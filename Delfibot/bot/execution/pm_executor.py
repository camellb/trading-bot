"""
Polymarket executor - per-user (SaaS multi-tenancy).

Every executor instance is bound to a specific user_id and reads that
user's mode (simulation|live) and starting_cash from user_config. A user
with no mode or no starting_cash is `not ready` - the executor exposes
zero state and refuses all writes for that user. Brand-new accounts see
nothing until they complete onboarding.

Simulation mode:
    Simulates fills at the observed market mid-price. Writes a pm_positions
    row with mode='simulation' and user_id=<user>. No external calls.

Live mode (Polymarket V2 CLOB):
    Submits a CLOB limit order at the chosen side's market price via
    py-clob-client-v2 using the wallet's private key from the OS
    keychain, polls the order status until filled or timed out, and
    persists a pm_positions row with mode='live', clob_order_id, and
    tx_hash. Behind a kill-switch env var so flipping the dashboard to
    Live alone doesn't open the floodgates: the operator must also set
    DELFI_LIVE_KILLSWITCH_OFF=1 in the sidecar process environment to
    authorise real orders. With the kill-switch on (default), live
    orders fall back to simulation fills and the position row gets a
    "[killswitch on]" reasoning prefix so it's clear in the dashboard
    which trades were paper.

Bankroll model (per-user):
    bankroll = user_config.starting_cash
             + Σ realized_pnl_usd (WHERE user_id AND status IN settled/invalid)
             - Σ cost_usd         (WHERE user_id AND status = 'open')
    Refreshed from DB before every sizing decision so concurrent fills
    don't over-stake.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

from sqlalchemy import text

import calibration
from db.engine import get_engine
from execution.pm_sizer import SizingDecision
from feeds.polymarket_feed import PolyMarket
from engine.user_config import (
    UserConfig,
    get_active_polymarket_creds,
    get_user_config,
)


# ── Live trading config ─────────────────────────────────────────────────────
# Polymarket V2 CLOB host. Same hostname as V1 post-cutover; V2 routing
# happens server-side based on the request shape.
CLOB_HOST = "https://clob.polymarket.com"
# Polygon mainnet chain id.
POLYGON_CHAIN_ID = 137
# Default CLOB tick size when the market doesn't specify one. Most
# Polymarket markets quote at penny ticks.
DEFAULT_TICK_SIZE = "0.01"
# Order-status poll interval + timeout when waiting for a fill. Most
# market-priced limit orders fill in under 5 seconds; budget 30s before
# giving up and recording the order as "live but not yet filled".
FILL_POLL_SECONDS = 1.0
FILL_POLL_TIMEOUT_SECONDS = 30.0
# Process-level cache so we don't reauth on every order. Keyed by the
# wallet's lowercase 0x address since each user_config has exactly one.
_CLOB_CLIENT_CACHE: dict = {}


def _live_killswitch_off() -> bool:
    """
    True iff the operator has explicitly opted into real-money order
    placement. Even with mode=live and bot_enabled=True, the executor
    falls back to simulation fills unless this is set. Read fresh on
    every order so flipping the env var doesn't require a restart.
    """
    return os.environ.get("DELFI_LIVE_KILLSWITCH_OFF", "").strip() in ("1", "true", "True")


def _get_clob_client(wallet_address: str, private_key: str):
    """
    Build (or reuse) a py-clob-client-v2 ClobClient bound to the user's
    Polygon wallet. Two-step construction per the SDK README: first an
    unauthed client to derive API creds, then a fully-authed client.
    Cached in-process so we only do the round-trip once.

    Imports are deferred so a sidecar that's never gone live doesn't
    have to load the SDK on startup.
    """
    cache_key = wallet_address.lower()
    cached = _CLOB_CLIENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    from py_clob_client_v2.client import ClobClient   # type: ignore
    seed = ClobClient(host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID, key=private_key)
    creds = seed.create_or_derive_api_key()
    client = ClobClient(
        host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID,
        key=private_key, creds=creds,
    )
    _CLOB_CLIENT_CACHE[cache_key] = client
    return client


def _poll_order_filled(client, order_id: str) -> dict:
    """
    Poll the CLOB until the order is filled, cancelled, rejected, or we
    time out. Returns the last status dict observed; caller decides
    what to do with each terminal state (fill = persist, cancel = drop,
    timeout = persist as "still open").
    """
    deadline = time.monotonic() + FILL_POLL_TIMEOUT_SECONDS
    last: dict = {"status": "unknown"}
    while time.monotonic() < deadline:
        try:
            last = client.get_order(order_id) or {"status": "unknown"}
        except Exception as exc:
            print(f"[pm_executor] get_order({order_id}) failed: {exc}",
                  file=sys.stderr)
            time.sleep(FILL_POLL_SECONDS)
            continue
        status = (last.get("status") or "").upper()
        if status in ("FILLED", "MATCHED"):
            return last
        if status in ("CANCELED", "CANCELLED", "REJECTED"):
            return last
        time.sleep(FILL_POLL_SECONDS)
    return last


# ── Public API ───────────────────────────────────────────────────────────────
class PMExecutor:
    """
    Handles the open/close lifecycle for Polymarket positions, scoped to a
    single user. Construct one instance per user per scan/request.
    """

    def __init__(
        self,
        user_id: str,
        *,
        user_config: Optional[UserConfig] = None,
        view_mode_override: Optional[str] = None,
    ):
        """
        `view_mode_override` is a read-only lens from the dashboard's
        SIM/LIVE toggle. When set, the `mode` property returns it so
        portfolio stats, open/settled queries, and bankroll calculations
        scope by the requested mode. Writes (opening positions, settling)
        always use the user's configured trading mode via `trading_mode` -
        the override is never used to place a real order.
        """
        if not user_id or not isinstance(user_id, str):
            raise ValueError(f"PMExecutor requires a user_id, got: {user_id!r}")
        self.user_id = user_id
        # Snapshot the user's config at construction time. Callers that need
        # fresh values after a dashboard edit should rebuild the executor.
        self._user_config: UserConfig = user_config or get_user_config(user_id)
        # Accept "simulation" / "live" only; silently drop anything else
        # so a bad header never blanks the dashboard.
        if view_mode_override in ("simulation", "live"):
            self._view_mode_override: Optional[str] = view_mode_override
        else:
            self._view_mode_override = None

    # ── Readiness ────────────────────────────────────────────────────────────
    @property
    def ready(self) -> bool:
        """True iff the bot may act for this user right now."""
        return self._user_config.ready_to_trade

    @property
    def mode(self) -> Optional[str]:
        """
        Mode used for read queries: view_mode_override when set, otherwise
        the user's configured trading mode. Use `trading_mode` for writes.
        """
        return self._view_mode_override or self._user_config.mode

    @property
    def trading_mode(self) -> Optional[str]:
        """
        The user's configured trading mode. Always from user_config,
        never from the view-mode override. Writes use this.
        """
        return self._user_config.mode

    # ── Bankroll ─────────────────────────────────────────────────────────────
    def get_starting_cash(self) -> float:
        """
        This user's starting bankroll in USD. Returns 0.0 if the user hasn't
        finished onboarding - callers treat that as "no bankroll, don't trade".
        """
        if self._user_config.starting_cash is None:
            return 0.0
        return float(self._user_config.starting_cash)

    def get_bankroll(self) -> float:
        """
        Current available bankroll in USD for this user in their current mode:

            bankroll = starting_cash
                     + Σ realized_pnl_usd   (user, settled/invalid)
                     - Σ cost_usd           (user, open)

        Not gated on `self.ready` - reads must always surface the user's own
        history. A user whose trading-mode config is incomplete (e.g. picked
        'live' but hasn't wired Polymarket creds yet) should still see their
        historical bankroll in either view. The `ready` flag only gates
        writes (opening new positions); write-path callers in pm_analyst
        already short-circuit upstream before calling this.
        """
        starting = self.get_starting_cash()
        try:
            with get_engine().begin() as conn:
                realized = conn.execute(text(
                    "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m "
                    "  AND status IN ('settled', 'invalid')"
                ), {"uid": self.user_id, "m": self.mode}).scalar() or 0.0
                open_cost = conn.execute(text(
                    "SELECT COALESCE(SUM(cost_usd), 0) "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m AND status = 'open'"
                ), {"uid": self.user_id, "m": self.mode}).scalar() or 0.0
            return float(starting) + float(realized) - float(open_cost)
        except Exception as exc:
            print(
                f"[pm_executor] get_bankroll({self.user_id}) db query failed: "
                f"{exc} - falling back to starting_cash=${starting:.2f} "
                f"(realized PnL and open positions NOT included)",
                file=sys.stderr,
            )
            return float(starting)

    def get_portfolio_stats(self) -> dict:
        """
        Dashboard-friendly summary for this user in the current view mode.

        Never gated on `self.ready`. A user who picked 'live' at onboarding
        but hasn't wired Polymarket creds yet is not ready to TRADE live,
        but they must still see their historical simulation stats when the
        view toggle is on Simulation. Cross-tenant isolation is preserved
        by the `user_id = :uid` filter on every underlying query, not by
        the readiness flag.
        """
        starting = self.get_starting_cash()
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE status = 'open') AS open_n, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled', 'invalid')) AS settled_n, "
                    "  COALESCE(SUM(cost_usd) FILTER (WHERE status = 'open'), 0) AS open_cost, "
                    "  COALESCE(SUM(realized_pnl_usd) FILTER (WHERE status IN ('settled', 'invalid')), 0) AS realized, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled', 'invalid') AND realized_pnl_usd > 0) AS wins "
                    "FROM pm_positions WHERE user_id = :uid AND mode = :m"
                ), {"uid": self.user_id, "m": self.mode}).fetchone()
                open_n    = int(row[0] or 0)
                settled_n = int(row[1] or 0)
                open_cost = float(row[2] or 0)
                realized  = float(row[3] or 0)
                wins      = int(row[4] or 0)
        except Exception as exc:
            print(f"[pm_executor] get_portfolio_stats({self.user_id}) failed: {exc}",
                  file=sys.stderr)
            open_n = settled_n = wins = 0
            open_cost = realized = 0.0
        bankroll = float(starting) + realized - open_cost
        return {
            "mode":            self.mode,
            "starting_cash":   starting,
            "bankroll":        bankroll,
            "equity":          float(starting) + realized,  # excludes open exposure
            "open_positions":  open_n,
            "open_cost":       open_cost,
            "settled_total":   settled_n,
            "settled_wins":    wins,
            "win_rate":        (wins / settled_n) if settled_n else None,
            "realized_pnl":    realized,
            "ready":           True,
        }

    # ── Open a position ──────────────────────────────────────────────────────
    def open_position(
        self,
        market:        PolyMarket,
        decision:      SizingDecision,
        claude_probability: float,
        prediction_id: Optional[int] = None,
        reasoning:     Optional[str] = None,
        category:      Optional[str] = None,
        market_archetype: Optional[str] = None,
    ) -> Optional[int]:
        """
        Record a new position. In simulation mode this is an immediate fill at
        the observed price. In live mode this will submit a CLOB order and
        only persist after fill confirmation.

        Returns the pm_positions.id, or None on failure.
        """
        if not decision.should_trade:
            print(f"[pm_executor] refusing to open position: {decision.skip_reason}",
                  file=sys.stderr)
            return None

        # Writes always use the configured trading mode. The view-mode
        # override only affects read queries - it must never redirect a
        # real order to a different mode.
        if self.trading_mode == "live":
            pos_id = self._open_live(market, decision, claude_probability,
                                      prediction_id, reasoning, category,
                                      market_archetype)
        else:
            pos_id = self._open_simulation(market, decision, claude_probability,
                                        prediction_id, reasoning, category,
                                        market_archetype)

        if pos_id and pos_id > 0:
            print(
                f"[pm_executor][{self.trading_mode}] opened pm_position {pos_id}: "
                f"{market.question[:60]!r} {decision.side} "
                f"{decision.shares:.2f} shares @ {decision.entry_price:.3f} "
                f"(cost ${decision.stake_usd:.2f}, ev {decision.ev*100:+.2f}%)",
                flush=True,
            )
        return pos_id

    def _open_simulation(self, market, decision, claude_p,
                     prediction_id, reasoning, category,
                     market_archetype=None) -> Optional[int]:
        if not self.ready:
            print(f"[pm_executor] _open_simulation refused: user {self.user_id} not ready",
                  file=sys.stderr)
            return None
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "INSERT INTO pm_positions ("
                    "  user_id, prediction_id, market_id, condition_id, slug, question, category, "
                    "  side, shares, entry_price, cost_usd, "
                    "  claude_probability, ev_bps, confidence, "
                    "  mode, status, expected_resolution_at, reasoning, event_slug, "
                    "  market_archetype, venue"
                    ") VALUES ("
                    "  :user_id, :pid, :mid, :cid, :slug, :q, :cat, "
                    "  :side, :shares, :ep, :cost, "
                    "  :cp, :ev_bps, :conf, "
                    "  :mode, 'open', :exp, :reason, :event_slug, "
                    "  :arch, :venue"
                    ") RETURNING id"
                ), {
                    "user_id": self.user_id,
                    # Persist the trading mode, never the view-mode override.
                    "mode":  self.trading_mode,
                    "pid":   prediction_id,
                    "mid":   market.id,
                    "cid":   market.condition_id,
                    "slug":  market.slug,
                    "q":     market.question,
                    "cat":   category,
                    "side":  decision.side,
                    "shares": decision.shares,
                    "ep":    decision.entry_price,
                    "cost":  decision.stake_usd,
                    "cp":    claude_p,
                    "ev_bps": decision.ev * 10_000.0,
                    "conf":  decision.confidence,
                    # Persist the best-guess resolution time, not the
                    # raw `endDate`. `endDate` is Polymarket's trading
                    # window close - on sports it equals tip time and
                    # on event markets it can be days off the actual
                    # deadline. `resolution_at_estimate` blends
                    # gameStartTime, events[0].endDate and endDate to
                    # produce the value the dashboard should countdown
                    # against. The settler refreshes this on every
                    # sweep so a deadline shift or early resolution
                    # does not leave a stale countdown on the UI.
                    "exp":   market.resolution_at_estimate,
                    "reason": (reasoning or "")[:4000] or None,
                    "event_slug": getattr(market, "event_slug", None),
                    "arch":  market_archetype,
                    # Venue is stamped onto every row so per-venue ROI,
                    # calibration, and dashboard filters work. Pulled from
                    # the user_config snapshot taken in __init__; a venue
                    # change mid-session would be picked up by the NEXT
                    # executor instance, not this one.
                    "venue": getattr(self._user_config, "venue", "polymarket"),
                }).fetchone()
                return int(row[0]) if row else None
        except Exception as exc:
            print(f"[pm_executor] _open_simulation failed: {exc}", file=sys.stderr)
            return None

    def _open_live(self, market: PolyMarket, decision: SizingDecision, claude_p: float,
                   prediction_id: Optional[int], reasoning: Optional[str],
                   category: Optional[str], market_archetype: Optional[str] = None
                   ) -> Optional[int]:
        """
        Submit a real Polymarket V2 CLOB order for the chosen side and
        persist the position with `clob_order_id` + `tx_hash` once the
        order is filled.

        Kill-switch (operator safety belt):
            Even with mode=live, bot_enabled=True, and full creds, this
            method falls back to a simulation fill unless the env var
            `DELFI_LIVE_KILLSWITCH_OFF=1` is set in the sidecar process.
            That way the dashboard's mode toggle and the bot-pill Start
            button cannot, by themselves, place a real order. The ops
            trail in the position row makes paper vs. real fills clear
            in the Performance / Positions views.

        US venue (`polymarket_us`):
            Different signing scheme (QCEX API-key, not EIP-712 wallet).
            Not part of this implementation; raises explicitly. Keep
            `polymarket_us` users on simulation until that client lands.
        """
        if not self.ready:
            print(f"[pm_executor] _open_live refused: user {self.user_id} not ready",
                  file=sys.stderr)
            return None

        venue = getattr(self._user_config, "venue", "polymarket")
        if venue == "polymarket_us":
            raise NotImplementedError(
                "Live execution on Polymarket US (CFTC-regulated DCM) is not "
                "yet wired. The US venue uses QCEX API-key signing and USD "
                "settlement (no Polygon wallet). Keep the user on "
                "mode='simulation' until the US execution client is implemented."
            )
        if venue not in ("polymarket", None):
            raise NotImplementedError(
                f"Live execution not implemented for venue={venue!r}. "
                f"Keep the user on mode='simulation'."
            )

        # ── Kill-switch fallback ────────────────────────────────────────
        if not _live_killswitch_off():
            print(
                f"[pm_executor] live order GATED by DELFI_LIVE_KILLSWITCH_OFF "
                f"for user {self.user_id} on '{market.question[:60]}'. "
                f"Falling back to simulation fill so the trade still shows up "
                f"in the dashboard. Set DELFI_LIVE_KILLSWITCH_OFF=1 in the "
                f"sidecar env to authorise real-money orders.",
                flush=True,
            )
            paper_reasoning = (
                f"[killswitch on] {(reasoning or '')}".strip()
                or "[killswitch on]"
            )
            return self._open_simulation(
                market, decision, claude_p, prediction_id,
                paper_reasoning, category, market_archetype,
            )

        # ── Pre-flight: creds present, market has clob token IDs ────────
        creds = get_active_polymarket_creds(self._user_config)
        wallet = (creds.get("wallet_address") or "").strip()
        private_key = (creds.get("private_key") or "").strip()
        if not wallet or not private_key:
            print(
                f"[pm_executor] _open_live refused: missing wallet/private_key "
                f"for user {self.user_id}. Mode says 'live' but creds are "
                f"incomplete - keep user on simulation until they save creds.",
                file=sys.stderr,
            )
            return None
        if not market.clob_token_ids:
            print(
                f"[pm_executor] _open_live refused: market {market.id!r} has "
                f"no clob_token_ids. Polymarket gamma didn't surface them; "
                f"likely a stale or pending market.",
                file=sys.stderr,
            )
            return None

        # ── Map the chosen side to the right ERC-1155 token ────────────
        # `market.clob_token_ids` is (yes_token, no_token); decision.side is
        # always "YES" or "NO" per the V1 sizer doctrine.
        token_id = market.clob_token_ids[0] if decision.side == "YES" else market.clob_token_ids[1]
        if not token_id:
            print(
                f"[pm_executor] _open_live refused: empty token_id for side "
                f"{decision.side} on market {market.id}",
                file=sys.stderr,
            )
            return None

        # ── Build the CLOB client + place order ─────────────────────────
        try:
            client = _get_clob_client(wallet, private_key)
        except Exception as exc:
            print(
                f"[pm_executor] _open_live: failed to build CLOB client: {exc}",
                file=sys.stderr,
            )
            return None

        try:
            from py_clob_client_v2.clob_types import (   # type: ignore
                OrderArgs, OrderType, PartialCreateOrderOptions,
            )
            try:
                from py_clob_client_v2.order_builder.constants import BUY  # type: ignore
                buy_side = BUY
            except Exception:
                # Some SDK versions expose the side enum elsewhere; fall
                # back to the string literal if so. The wire format is
                # "BUY" / "SELL" anyway.
                buy_side = "BUY"
        except Exception as exc:
            print(
                f"[pm_executor] _open_live: SDK import failed: {exc}. "
                f"Is py-clob-client-v2 installed in the sidecar?",
                file=sys.stderr,
            )
            return None

        # Limit price = the wallet's intended entry price on the chosen
        # side. The sizer already clamped to a sensible range; we just
        # round to the tick size to avoid the SDK rejecting the order.
        # `decision.entry_price` is the side-specific ask; for shares
        # the size is stake / price.
        entry_price = round(float(decision.entry_price), 2)
        size_shares = round(float(decision.shares), 4)
        order_args = OrderArgs(
            token_id=token_id,
            price=entry_price,
            side=buy_side,
            size=size_shares,
        )

        try:
            resp = client.create_and_post_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size=DEFAULT_TICK_SIZE),
                order_type=OrderType.GTC,
            )
        except Exception as exc:
            print(
                f"[pm_executor] _open_live: create_and_post_order failed: {exc}",
                file=sys.stderr,
            )
            return None
        if not isinstance(resp, dict):
            print(f"[pm_executor] _open_live: unexpected order response shape: {resp!r}",
                  file=sys.stderr)
            return None

        order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id")
        if not order_id:
            print(
                f"[pm_executor] _open_live: order response missing order id: {resp!r}",
                file=sys.stderr,
            )
            return None

        # ── Wait for fill, then persist ─────────────────────────────────
        final = _poll_order_filled(client, str(order_id))
        final_status = (final.get("status") or "").upper()
        # `transactionHash` lands here once the on-chain match is mined.
        # Some V2 responses split per-fill into a `transactionsHashes`
        # list; flatten both shapes.
        tx_hash = (
            final.get("transactionHash")
            or (final.get("transactionsHashes") or [None])[0]
            or resp.get("transactionHash")
        )

        if final_status not in ("FILLED", "MATCHED"):
            # Order placed but not (fully) filled within our timeout, OR
            # rejected outright. Persist anyway with a status note so
            # the operator can see it in the dashboard. The settler
            # will pick up partial fills on its next sweep.
            print(
                f"[pm_executor][live] order {order_id} ended in status "
                f"{final_status!r} on '{market.question[:60]}'. Persisting "
                f"the live attempt with the order id; manual reconciliation "
                f"may be required.",
                flush=True,
            )

        return self._persist_live_position(
            market=market, decision=decision, claude_p=claude_p,
            prediction_id=prediction_id,
            reasoning=reasoning, category=category,
            market_archetype=market_archetype,
            clob_order_id=str(order_id),
            tx_hash=str(tx_hash) if tx_hash else None,
        )

    def _persist_live_position(
        self, *, market: PolyMarket, decision: SizingDecision, claude_p: float,
        prediction_id: Optional[int], reasoning: Optional[str],
        category: Optional[str], market_archetype: Optional[str],
        clob_order_id: str, tx_hash: Optional[str],
    ) -> Optional[int]:
        """
        Insert the live pm_positions row. Identical schema to
        `_open_simulation` but `mode='live'`, `clob_order_id`, and
        `tx_hash` are populated. Kept separate so the sim path stays
        free of any live-only fields.
        """
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "INSERT INTO pm_positions ("
                    "  user_id, prediction_id, market_id, condition_id, slug, question, category, "
                    "  side, shares, entry_price, cost_usd, "
                    "  claude_probability, ev_bps, confidence, "
                    "  mode, status, expected_resolution_at, reasoning, event_slug, "
                    "  market_archetype, venue, clob_order_id, tx_hash"
                    ") VALUES ("
                    "  :user_id, :pid, :mid, :cid, :slug, :q, :cat, "
                    "  :side, :shares, :ep, :cost, "
                    "  :cp, :ev_bps, :conf, "
                    "  'live', 'open', :exp, :reason, :event_slug, "
                    "  :arch, :venue, :order_id, :tx_hash"
                    ") RETURNING id"
                ), {
                    "user_id": self.user_id,
                    "pid":     prediction_id,
                    "mid":     market.id,
                    "cid":     market.condition_id,
                    "slug":    market.slug,
                    "q":       market.question,
                    "cat":     category,
                    "side":    decision.side,
                    "shares":  decision.shares,
                    "ep":      decision.entry_price,
                    "cost":    decision.stake_usd,
                    "cp":      claude_p,
                    "ev_bps":  decision.ev * 10_000.0,
                    "conf":    decision.confidence,
                    "exp":     market.resolution_at_estimate,
                    "reason":  (reasoning or "")[:4000] or None,
                    "event_slug": getattr(market, "event_slug", None),
                    "arch":    market_archetype,
                    "venue":   getattr(self._user_config, "venue", "polymarket"),
                    "order_id": clob_order_id,
                    "tx_hash": tx_hash,
                }).fetchone()
                return int(row[0]) if row else None
        except Exception as exc:
            print(f"[pm_executor] _persist_live_position failed: {exc}",
                  file=sys.stderr)
            return None

    # ── Settle a position ────────────────────────────────────────────────────
    def settle_position(
        self,
        position_id:      int,
        winning_outcome:  str,                # 'YES' | 'NO' | 'INVALID'
        settlement_price: Optional[float] = None,
    ) -> bool:
        """
        Resolve a position. `winning_outcome` is the outcome that won on
        Polymarket. `settlement_price` defaults to $1.00 for winners /
        $0.00 for losers / $0.50 for invalid markets.
        """
        outcome = (winning_outcome or "").upper()
        if outcome not in ("YES", "NO", "INVALID"):
            print(f"[pm_executor] settle: invalid outcome {outcome!r}",
                  file=sys.stderr)
            return False

        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT side, shares, cost_usd, prediction_id "
                    "FROM pm_positions "
                    "WHERE id = :pid AND user_id = :uid AND status = 'open'"
                ), {"pid": position_id, "uid": self.user_id}).fetchone()
                if row is None:
                    print(f"[pm_executor] settle: position {position_id} not open "
                          f"for user {self.user_id}", file=sys.stderr)
                    return False
                side      = str(row[0])
                shares    = float(row[1])
                cost_usd  = float(row[2])
                pred_id   = int(row[3]) if row[3] is not None else None

                if settlement_price is None:
                    if outcome == "INVALID":
                        settlement_price = 0.5
                    else:
                        settlement_price = 1.0 if side == outcome else 0.0

                proceeds = shares * float(settlement_price)
                pnl      = proceeds - cost_usd
                status   = "invalid" if outcome == "INVALID" else "settled"

                conn.execute(text(
                    "UPDATE pm_positions SET "
                    "  status              = :st, "
                    "  settled_at          = CURRENT_TIMESTAMP, "
                    "  settlement_outcome  = :out, "
                    "  settlement_price    = :sp, "
                    "  realized_pnl_usd    = :pnl "
                    "WHERE id = :pid"
                ), {
                    "st": status, "out": outcome,
                    "sp": float(settlement_price), "pnl": float(pnl),
                    "pid": position_id,
                })

            # Feed the calibration ledger so Brier and reliability update.
            # 'correct' means we took the winning side. INVALID markets
            # aren't scored (outcome=None bucket).
            if pred_id is not None and outcome != "INVALID":
                claude_correct = 1 if side == outcome else 0
                calibration.resolve_prediction_by_id(
                    prediction_id=pred_id,
                    outcome=claude_correct,
                    pnl_usd=pnl,
                    note=f"pm_settlement outcome={outcome} side={side}",
                )

            print(
                f"[pm_executor][{self.trading_mode}] settled pm_position {position_id}: "
                f"outcome={outcome}, side={side}, pnl=${pnl:+.2f}",
                flush=True,
            )

            # Trade-volume learning cadence - cheap no-op until the
            # 50-settled-trade gate is crossed for this user. Always uses
            # the trading mode so the learning cycle evaluates the actual
            # trades the bot made, not a view-mode snapshot.
            try:
                from engine.learning_cadence import maybe_run_learning_cycle
                maybe_run_learning_cycle(user_id=self.user_id, mode=self.trading_mode)
            except Exception as exc:
                print(f"[pm_executor] learning_cadence hook failed: {exc}",
                      file=sys.stderr)
            return True
        except Exception as exc:
            print(f"[pm_executor] settle_position failed: {exc}", file=sys.stderr)
            return False

    # ── Open-position lookups ────────────────────────────────────────────────
    def get_open_positions(self) -> list[dict]:
        # Read-only lookup: never gated on `self.ready`. A user who isn't
        # currently trade-ready (e.g. live mode without creds) must still
        # see their own history. Isolation is enforced by `user_id = :uid`.
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text(
                    "SELECT id, market_id, question, category, side, shares, "
                    "       entry_price, cost_usd, claude_probability, "
                    "       ev_bps, confidence, expected_resolution_at, "
                    "       created_at, prediction_id, reasoning, slug "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m AND status = 'open' "
                    "ORDER BY created_at DESC"
                ), {"uid": self.user_id, "m": self.mode}).fetchall()
                return [
                    {
                        "id":                r[0],
                        "market_id":         r[1],
                        "question":          r[2],
                        "category":          r[3],
                        "side":              r[4],
                        "shares":            float(r[5]),
                        "entry_price":       float(r[6]),
                        "cost_usd":          float(r[7]),
                        "claude_probability": float(r[8]) if r[8] is not None else None,
                        "ev_bps":            float(r[9]) if r[9] is not None else None,
                        "confidence":        float(r[10]) if r[10] is not None else None,
                        "expected_resolution_at":
                            r[11].isoformat() if r[11] else None,
                        "created_at":        r[12].isoformat() if r[12] else None,
                        "prediction_id":     r[13],
                        "reasoning":         r[14],
                        "slug":              r[15],
                    }
                    for r in rows
                ]
        except Exception as exc:
            print(f"[pm_executor] get_open_positions failed: {exc}", file=sys.stderr)
            return []

    def has_open_position_on_market(self, market_id: str) -> bool:
        if not self.ready:
            return False
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT 1 FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m "
                    "  AND status = 'open' AND market_id = :mid "
                    "LIMIT 1"
                ), {"uid": self.user_id, "m": self.mode, "mid": str(market_id)}).fetchone()
                return row is not None
        except Exception as exc:
            print(f"[pm_executor] has_open_position failed: {exc}", file=sys.stderr)
            return True  # fail closed - assume position exists to prevent duplicates

    def open_position_count(self) -> int:
        if not self.ready:
            return 0
        try:
            with get_engine().begin() as conn:
                return int(conn.execute(text(
                    "SELECT COUNT(*) FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m AND status = 'open'"
                ), {"uid": self.user_id, "m": self.mode}).scalar() or 0)
        except Exception as exc:
            print(f"[pm_executor] open_position_count failed: {exc}", file=sys.stderr)
            return 999  # fail closed - prevent opening new positions on DB error

    def count_positions_for_event(self, event_slug: str) -> int:
        """Count this user's open positions belonging to the same event group."""
        if not self.ready:
            return 0
        try:
            with get_engine().begin() as conn:
                return int(conn.execute(text(
                    "SELECT COUNT(*) FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m "
                    "  AND status = 'open' AND event_slug = :slug"
                ), {"uid": self.user_id, "m": self.mode, "slug": event_slug}).scalar() or 0)
        except Exception as exc:
            print(f"[pm_executor] count_positions_for_event failed: {exc}",
                  file=sys.stderr)
            return 0
