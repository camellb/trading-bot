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
             + Σ realized_pnl_usd (WHERE user_id AND status IN settled/invalid/closed_early)
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
from db.engine import get_engine, iso_utc
from execution.pm_sizer import SizingDecision, _MIN_ABSOLUTE_STAKE_USD
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

# Tracks (cache_key) for which we've already synced CLOB balance/allowance
# under POLY_1271. Per Polymarket V2 docs
# (https://docs.polymarket.com/trading/deposit-wallets), the CLOB caches a
# user's balance + allowance state per signature_type. Before placing the
# first POLY_1271 order from this process, we MUST call
# update_balance_allowance(signature_type=3) — otherwise the CLOB has no
# record of the deposit wallet's funds and rejects orders. Idempotent +
# one-time per cache key keeps it cheap on every subsequent order.
_BALANCE_ALLOWANCE_SYNCED: set = set()

# Per-process flag: True once we've detected the V2 "signer mismatch"
# rejection from Polymarket. Pre-2026-05-17 we kept seeing this rejection
# because our py-clob-client-v2 was pinned to 1.0.0 (released 2026-04-17)
# which predates the SDK's deposit-wallet (POLY_1271 / ERC-7739 wrapped
# signature) support. 1.0.1 (2026-05-09) lands "feat: add deposit wallet
# order support" (#39) — orders now correctly set signer=DepositWallet
# and build the ERC-7739 wrapper. The mismatch state should never trip
# anymore, but the gate stays as a safety belt: if the rejection comes
# back (e.g. SDK regression or another V3-style migration), we fall back
# to simulation and tell the user instead of hammering the CLOB.
_V2_SIGNER_MISMATCH_DETECTED: bool = False
_V2_SIGNER_MISMATCH_NOTIFIED: bool = False

# Last-known live wallet bankroll per user_id. Populated on every
# successful get_cached_total_funder_balance() call in get_bankroll;
# read as a fallback when the wallet probe misses (cold cache + lock
# contention). Without this, get_bankroll fell through to the SIM
# formula (`starting + realized - open_cost`), which uses the
# configured starting_cash ($1000 onboarding default) and produced
# fake "Balance: $989" Dashboard tiles. User-reported 2026-05-20.
# In-process only; the next successful probe (within ~5s of boot)
# overwrites it with the true value.
_LIVE_BANKROLL_FALLBACK: dict[str, float] = {}


def _is_v2_signer_mismatch(exc_str: str) -> bool:
    """Detect Polymarket's V2 'signer != api-key address' rejection so we
    can shortcut subsequent orders + surface a clear user action."""
    s = (exc_str or "").lower()
    return (
        "the order signer address has to be the address of the api key" in s
        or "signer address has to be the address" in s
    )


def reset_v2_signer_mismatch_state() -> None:
    """Called from local_api when credentials change. Lets the next live
    order retry from a clean slate instead of staying gated forever.
    Also clears the balance-allowance sync memo so the freshly-saved key
    rebuilds CLOB state from scratch."""
    global _V2_SIGNER_MISMATCH_DETECTED, _V2_SIGNER_MISMATCH_NOTIFIED
    _V2_SIGNER_MISMATCH_DETECTED = False
    _V2_SIGNER_MISMATCH_NOTIFIED = False
    _BALANCE_ALLOWANCE_SYNCED.clear()


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

    Polymarket account shapes:
        signature_type=0  EOA               (MetaMask users)
        signature_type=1  POLY_PROXY        (the default Polymarket
                                             Magic-account proxy, most
                                             users)
        signature_type=2  POLY_GNOSIS_SAFE  (newer Safe-magic accounts)
    We don't know up-front which one applies. polymarket_wallet.py
    probes /balance-allowance for each sig_type and caches the answer
    for 5 minutes; we read it here so orders are signed against the
    same on-chain account that actually holds the user's collateral.
    Without this, a Magic-account user with funds in their proxy
    would get every order rejected for "insufficient collateral"
    because the EOA-default client would query a $0 EOA wallet.

    Cache key includes a SHA-256 of the private key AND the resolved
    signature_type so a key rotation or shape change does NOT return
    a stale client. The full key is never logged or exposed; only its
    hash sits in memory as a dict-key prefix.

    Imports are deferred so a sidecar that's never gone live doesn't
    have to load the SDK on startup.
    """
    import hashlib
    # Detect signature_type + funder once; cached upstream for 5 min.
    sig_type = 0
    funder: Optional[str] = None
    try:
        from feeds.polymarket_wallet import get_poly_signer_info
        info = get_poly_signer_info(private_key)
        if info:
            sig_type = int(info.get("signature_type", 0) or 0)
            funder = info.get("funder") or None
    except Exception as exc:
        print(
            f"[pm_executor] signer probe failed, defaulting to EOA: {exc}",
            file=sys.stderr,
        )

    # MANUAL api-key path: if the user has pasted Polymarket api
    # creds via Settings, use them directly and skip the SDK's
    # create_or_derive_api_key flow entirely. This is the unblock
    # for accounts where auto-derive returns a stale post-migration
    # key bound to the wrong (signer, funder) context.
    manual_creds = None
    try:
        from engine.user_config import get_polymarket_api_creds
        manual_creds = get_polymarket_api_creds()
    except Exception:
        manual_creds = None

    key_hash = hashlib.sha256(private_key.encode("utf-8")).hexdigest()[:16]
    cache_tag = "m" if manual_creds else "a"   # m=manual, a=auto-derive
    cache_key = f"{wallet_address.lower()}:{key_hash}:sig{sig_type}:{cache_tag}"
    cached = _CLOB_CLIENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    from py_clob_client_v2.client import ClobClient   # type: ignore
    from py_clob_client_v2.clob_types import ApiCreds  # type: ignore
    seed_kwargs = dict(host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID, key=private_key)
    if sig_type != 0:
        seed_kwargs["signature_type"] = sig_type
        if funder:
            seed_kwargs["funder"] = funder

    if manual_creds:
        creds = ApiCreds(
            api_key=manual_creds["api_key"],
            api_secret=manual_creds["api_secret"],
            api_passphrase=manual_creds["api_passphrase"],
        )
        print(
            f"[pm_executor] using MANUAL Polymarket api-key (from "
            f"Settings) for sig_type={sig_type} funder={funder}",
            file=sys.stderr,
        )
    else:
        seed = ClobClient(**seed_kwargs)
        creds = seed.create_or_derive_api_key()
    client_kwargs = dict(seed_kwargs)
    client_kwargs["creds"] = creds
    client = ClobClient(**client_kwargs)
    _CLOB_CLIENT_CACHE[cache_key] = client

    # Polymarket V2 docs (https://docs.polymarket.com/trading/deposit-wallets):
    # "After funding the deposit wallet or approving contracts from it,
    # update the CLOB balance cache using signature_type = 3."
    # Without this call, the CLOB has no record of the deposit wallet's
    # collateral and rejects POLY_1271 orders. One-time per cache_key
    # per process — cheap on every subsequent order.
    if sig_type == 3 and cache_key not in _BALANCE_ALLOWANCE_SYNCED:
        try:
            from py_clob_client_v2.clob_types import (  # type: ignore
                BalanceAllowanceParams, AssetType,
            )
            from py_clob_client_v2.order_utils import SignatureTypeV2  # type: ignore
            client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=SignatureTypeV2.POLY_1271,
                )
            )
            _BALANCE_ALLOWANCE_SYNCED.add(cache_key)
            print(
                f"[pm_executor] synced CLOB balance-allowance under "
                f"POLY_1271 for funder={funder}",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"[pm_executor] update_balance_allowance failed (continuing): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    return client


def _extract_filled_size(final: dict, resp: dict) -> float:
    """Pull the ACTUAL filled size in shares from a CLOB order response.

    Polymarket V2 reports fills under several keys depending on the
    code path that returned them. We check, in order:
      * `size_matched`        — V2 standard (most common)
      * `size_filled`         — older alias still emitted by some SDK builds
      * `filled_size`         — alt-cased variant
      * `made_amount`         — taker amount in collateral wei; converted
                                later to shares via the entry price
    The order is `(filled response) || (post-order response)` so a
    partial-fill discovered post-poll wins over the initial accept.
    """
    for src in (final, resp):
        if not isinstance(src, dict):
            continue
        for k in ("size_matched", "size_filled", "filled_size",
                  "matched_size", "match_size"):
            v = src.get(k)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                return fv
    # Fall back: scan a `fills` or `events` list if present.
    for src in (final, resp):
        if not isinstance(src, dict):
            continue
        fills = src.get("fills") or src.get("events") or []
        if not isinstance(fills, list):
            continue
        total = 0.0
        for f in fills:
            if not isinstance(f, dict):
                continue
            for k in ("size", "matched_size", "fill_size"):
                v = f.get(k)
                if v is None:
                    continue
                try:
                    total += float(v)
                except (TypeError, ValueError):
                    pass
                break
        if total > 0:
            return total
    return 0.0


def _lookup_on_chain_position(
    *, funder_address: Optional[str], condition_id: Optional[str],
    side: str,
) -> Optional[dict]:
    """Probe Polymarket's data-api for a current position on (condition_id,
    side) and return the matching row, or None if no such position
    exists.

    Used by _open_live to close the gap between order placement and
    fill confirmation: if the CLOB poll says "no fill" but the
    matcher already landed the trade on-chain, the position is in
    data-api before the order endpoint catches up. Without this
    fast-path, the bot would cancel the (already-filled) order, lose
    the row, and rely on the 2-minute reconciler tick to backfill.
    """
    if not funder_address or not condition_id:
        return None
    target_cond = condition_id.lower()
    # Map Delfi's binary YES/NO onto data-api's outcomeIndex 0/1 -
    # same convention as the reconciler.
    target_idx  = 0 if side.upper() == "YES" else 1
    try:
        import requests
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={
                "user": funder_address,
                "sizeThreshold": "0.01",
                "limit": "200",
            },
            headers={"User-Agent": "delfibot/1.0 _open_live"},
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as exc:
        print(f"[pm_executor] data-api position probe failed: "
              f"{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        cid = (row.get("conditionId") or "").lower()
        idx = row.get("outcomeIndex")
        size = float(row.get("size") or 0.0)
        if cid == target_cond and idx == target_idx and size > 0:
            return row
    return None


def _extract_filled_cost(
    final: dict, resp: dict, filled_shares: float,
    *, fallback_price: float, client=None, order_id: Optional[str] = None,
) -> float:
    """Pull the ACTUAL collateral spent for the filled shares.

    Three sources of truth, tried in order:

    1. The CLOB's per-trade records via `client.get_trades(id=order_id)`.
       This is the on-chain truth - every match emits a Trade row with
       its own (price, size). Sum price*size across all trades for the
       order to get the volume-weighted USDC cost, which exactly
       matches what hit the wallet. Marketable BUYs frequently fill
       BELOW the limit price (price improvement), and the bot was
       previously recording the limit as cost which produced ghost
       P&L drift in pm_positions (Solana 1AM ET case: DB $3.85,
       on-chain $2.31).

    2. Inline `fills` array on the order response (some SDK builds
       echo trades back inline). Same math.

    3. Single-amount fields if the CLOB returned one. Less reliable -
       some are maker shares, not USDC - but salvages any positive
       value over the limit-price fallback.

    4. Final fallback: `filled_shares * fallback_price`. Last resort
       only. Logs a warning so we can spot the case in production;
       the reconciler's drift detector will alert on the next tick
       if this row's recorded cost diverges from on-chain.
    """
    # Source 1: client.get_trades() for the order. Most reliable.
    # Prefer `usdcSize` (the actual USDC sent on-chain, INCLUDING
    # taker fees) over `price * size` (which strips the fee). The
    # difference is small per trade (~1-2%) but compounds across
    # the position log and produces realized-P&L drift against
    # Polymarket's own number - the source of the user-visible
    # "Polymarket says X, Delfi says Y" complaint.
    if client is not None and order_id:
        try:
            from py_clob_client_v2.clob_types import TradeParams  # type: ignore
            trades = client.get_trades(TradeParams(id=str(order_id)))
        except Exception as exc:
            print(f"[pm_executor] get_trades({order_id}) failed: {exc}",
                  file=sys.stderr)
            trades = None
        if isinstance(trades, list) and trades:
            total = 0.0
            for t in trades:
                if not isinstance(t, dict):
                    continue
                # Prefer the actual USDC sent (with fees) if present.
                usdc = t.get("usdcSize") or t.get("usdc_size")
                if usdc is not None:
                    try:
                        u = float(usdc)
                        if u > 0:
                            total += u
                            continue
                    except (TypeError, ValueError):
                        pass
                # Fallback: price * size if the trade row only has
                # the per-share data (older SDK builds, or maker
                # fills that don't carry a usdcSize).
                try:
                    p = float(t.get("price") or 0)
                    s = float(t.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                if p > 0 and s > 0:
                    total += p * s
            if total > 0:
                return total

    # Source 2: inline fills array on the order response. Same
    # usdcSize-preference logic.
    for src in (final, resp):
        if not isinstance(src, dict):
            continue
        fills = src.get("fills") or src.get("trades") or src.get("events")
        if not isinstance(fills, list):
            continue
        total = 0.0
        for f in fills:
            if not isinstance(f, dict):
                continue
            usdc = f.get("usdcSize") or f.get("usdc_size")
            if usdc is not None:
                try:
                    u = float(usdc)
                    if u > 0:
                        total += u
                        continue
                except (TypeError, ValueError):
                    pass
            try:
                p = float(f.get("price") or 0)
                s = float(f.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if p > 0 and s > 0:
                total += p * s
        if total > 0:
            return total

    # Source 3: single-amount fields on the order response.
    for src in (final, resp):
        if not isinstance(src, dict):
            continue
        for k in ("matched_amount", "made_amount", "filled_amount",
                  "cost", "cost_usd", "taking_amount"):
            v = src.get(k)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                return fv

    # Source 4: limit-price fallback. This is the bug path - it's the
    # original limit, not the actual fill. Log so we can spot the case
    # in production. The reconciler's drift detector will surface a
    # warning if the limit/fill mismatch persists.
    if filled_shares > 0:
        print(
            f"[pm_executor] WARN _extract_filled_cost falling back to "
            f"limit price for order={order_id!r}; pm_positions.cost_usd "
            f"may differ from on-chain truth. Drift detector will alert "
            f"on the next reconciler tick.",
            file=sys.stderr, flush=True,
        )
        return filled_shares * float(fallback_price)
    return 0.0


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

        Live override: when the user's CONFIGURED trading mode is 'live'
        (not the view-mode override) AND a Polymarket private key is on
        file, return the actual on-chain DepositWallet balance instead of
        the static configured value. Without this the sizer was using
        the simulation-default $1000 starting_cash on live mode and
        building orders 100x larger than the wallet could fund —
        Polymarket rejected with "not enough balance" because the
        configured number didn't match real funds.

        The wallet probe is cached for 5 min in polymarket_wallet, so this
        is cheap even when called from every order. Falls back to the
        configured starting_cash on any probe failure so the bot doesn't
        block trading just because the RPC blipped.
        """
        if self._user_config.starting_cash is None:
            return 0.0
        configured = float(self._user_config.starting_cash)
        # LIVE mode: never return the configured starting_cash. That
        # number is the SIM-mode default ($1000 typically), and
        # treating it as real bankroll causes the sizer to build
        # orders 100x bigger than the wallet can fund (Polymarket
        # rejects). Use cached signer info (or the last-known live
        # bankroll, or 0 as a safe floor) - never the SIM constant.
        if (
            self._user_config.mode == "live"
            and self._view_mode_override is None
        ):
            # Live mode: starting_cash is the user's TOTAL committed
            # capital on Polymarket, i.e. cash + cost basis of all
            # open positions = equity at cost basis. This is the
            # right reference for every risk gate:
            #
            #   - exposure_cap = starting_cash * (1 - reserve_pct).
            #     If we used current cash instead, the cap would
            #     shrink every time we opened a position (cash goes
            #     down → cap goes down → previously-fine exposure
            #     suddenly exceeds the cap → bot refuses to open
            #     anything new → user sees "exposure $19 >= cap $9"
            #     even though they configured a much higher tolerance.
            #     Real bug seen with bankroll $10, open_cost $19,
            #     90% cap that was supposed to allow $27 deployment.
            #   - drawdown / daily-loss / weekly-loss limits also
            #     use this as the denominator so a trade-open
            #     doesn't artificially inflate the drawdown reading.
            #
            # Wallet probe (cache-only) + DB open_cost. Both read
            # from in-process caches populated by background jobs
            # so this is essentially free.
            wallet_balance = 0.0
            try:
                from engine.user_config import get_user_polymarket_creds
                from feeds.polymarket_wallet import get_cached_poly_signer_info
                creds = get_user_polymarket_creds(self.user_id)
                pk = (creds or {}).get("private_key") if creds else None
                if pk:
                    info = get_cached_poly_signer_info(pk)
                    if info and isinstance(info.get("balance"), (int, float)):
                        wallet_balance = float(info["balance"])
                if wallet_balance <= 0.0:
                    wallet_balance = float(
                        _LIVE_BANKROLL_FALLBACK.get(self.user_id, 0.0)
                    )
            except Exception as exc:
                print(
                    f"[pm_executor] live starting_cash wallet probe failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                wallet_balance = float(
                    _LIVE_BANKROLL_FALLBACK.get(self.user_id, 0.0)
                )

            open_cost_basis = 0.0
            try:
                with get_engine().begin() as conn:
                    row = conn.execute(text(
                        "SELECT COALESCE(SUM(cost_usd), 0) "
                        "FROM pm_positions "
                        "WHERE user_id = :uid "
                        "  AND mode = 'live' "
                        "  AND status = 'open'"
                    ), {"uid": self.user_id}).fetchone()
                    if row and row[0] is not None:
                        open_cost_basis = float(row[0])
            except Exception as exc:
                print(
                    f"[pm_executor] live starting_cash open_cost probe "
                    f"failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

            return wallet_balance + open_cost_basis
        return configured

    def get_equity(self) -> float:
        """
        Current total wealth in USD = cash + current MTM value of open
        positions. Used by the risk manager's drawdown calc so the
        formula `1 - (equity / starting)` reflects ACTUAL loss
        (realised + unrealised), not deployment.

        LIVE mode: pulls from Polymarket's data-api positions cache
        (every position the wallet holds at currentValue). Cache is
        warmed by the pm_balance_refresh scheduler job; if cold, falls
        back to bankroll + cost basis of bot-tracked opens.

        SIM mode: returns bankroll + SUM(cost_usd) of open bot
        positions. There's no per-position MTM in sim (we don't
        compute synthetic mid-prices), so cost-basis equity is the
        right approximation. Drawdown still rises correctly on
        settled losses because they flow through bankroll via the
        sim-mode formula.
        """
        bankroll = self.get_bankroll()
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT COALESCE(SUM(cost_usd), 0) "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m "
                    "  AND status = 'open'"
                ), {"uid": self.user_id, "m": self.mode}).fetchone()
                open_cost_basis = float(row[0] or 0)
        except Exception:
            open_cost_basis = 0.0

        if self.mode == "live":
            try:
                from engine.user_config import get_user_polymarket_creds
                from feeds.polymarket_wallet import (
                    get_total_open_positions_value, get_cached_poly_signer_info,
                )
                creds = get_user_polymarket_creds(self.user_id)
                pk = (creds or {}).get("private_key") if creds else None
                if pk:
                    info = get_cached_poly_signer_info(pk)
                    funder = (info or {}).get("funder") if info else None
                    if funder:
                        mtm = get_total_open_positions_value(funder)
                        if mtm is not None:
                            return float(bankroll) + float(mtm)
            except Exception as exc:
                print(
                    f"[pm_executor] get_equity MTM probe failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        # Fallback (always for sim, on data-api miss for live): cost
        # basis. Strictly worse but never wrong.
        return float(bankroll) + open_cost_basis

    def get_bankroll(self) -> float:
        """
        Current bankroll in USD for this user in their current mode.

        LIVE mode: the actual on-chain wallet total at the funder
        (pUSD + USDC.e). Both are spendable — pUSD trades immediately
        on V2 markets; USDC.e is auto-wrapped to pUSD within ~10
        minutes by pm_activate_legacy. NO additional DB adjustment:
        the wallet balance already reflects every settled bet and
        every open position (open positions are CTF tokens paid for
        with pUSD that has already left the wallet). Adding
        realized_pnl on top would double-count.

        SIM mode: the bookkeeping formula
            bankroll = starting_cash + Σ realized_pnl − Σ open_cost
        because there's no real wallet to read from.

        Background: the older "live + realized − open" formula caused
        the WIN Telegram message to report ``Balance: $3.87`` after a
        $4.60 bet returned $5.00. The live wallet was $3.47 (pre-
        activation pUSD), the DB added $0.40 realized P&L on top, and
        the math didn't add up (2026-05-18).

        Not gated on `self.ready` — reads must always surface the
        user's own history.
        """
        # LIVE mode: read the wallet directly. No DB adjustment.
        if (
            self._user_config.mode == "live"
            and self._view_mode_override is None
        ):
            try:
                from engine.user_config import get_user_polymarket_creds
                from feeds.polymarket_wallet import (
                    get_cached_total_funder_balance,
                )
                creds = get_user_polymarket_creds(self.user_id)
                pk = (creds or {}).get("private_key") if creds else None
                if pk:
                    total = get_cached_total_funder_balance(pk)
                    if total is not None:
                        # Remember the last good probe so the next
                        # cache-cold call can serve a real number.
                        _LIVE_BANKROLL_FALLBACK[self.user_id] = float(total)
                        return float(total)
                    # Probe missed: fall back to the LAST observed live
                    # balance instead of dropping through to the SIM
                    # formula. The SIM formula uses the configured
                    # starting_cash (typically $1000 from onboarding)
                    # and produces a fake "Balance: $989" the second
                    # the cache is cold - the bug user-reported
                    # 2026-05-20 ("what the actual fuck happened here?").
                    # 0.0 floor is intentional for first-boot: better
                    # to show $0 than a $989 fabrication.
                    last = _LIVE_BANKROLL_FALLBACK.get(self.user_id, 0.0)
                    return float(last)
            except Exception as exc:
                print(
                    f"[pm_executor] live get_bankroll probe failed, "
                    f"falling back to last-known live balance: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                last = _LIVE_BANKROLL_FALLBACK.get(self.user_id, 0.0)
                return float(last)

        # SIM mode only: DB-derived formula. Never reached in live
        # mode - the live branch above always returns something
        # (either a fresh probe, the last-known live balance, or 0).
        starting = self.get_starting_cash()
        try:
            with get_engine().begin() as conn:
                realized = conn.execute(text(
                    "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m "
                    "  AND status IN ('settled', 'invalid', 'closed_early')"
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
                # Everything mode-scoped. User's hard rule (2026-05-16):
                # "WE CAN'T MIX ANY SIMULATION AND LIVE DATA TOGETHER.
                # NEVER!" The earlier lifetime-counts experiment is
                # reverted — counts, P&L, win rate, and skipped
                # evaluations all live under the current mode only.
                # Switching modes shows a clean per-mode ledger; the
                # other mode's data still exists in the DB, just not
                # in the displayed view.
                # `closed_early` rows count toward the ledger because
                # the exit-policy SELL realized P&L, even though the
                # underlying market hasn't reached natural resolution
                # yet. They behave identically to a `settled` row for
                # bankroll, equity, and win-rate purposes; the
                # distinguishing field is `close_reason`, used by the
                # review report to score exit quality separately.
                # `settled_n` counts only real trades that resolved
                # with a YES/NO outcome (status='settled' or
                # 'closed_early'). `invalid` markets are
                # auto-refunded by Polymarket and aren't trades the
                # user "took" - including them in the denominator
                # drags win-rate down for no reason (15W/22T=68%
                # vs 15W/21T=71% for the same trades). The
                # Performance page already excludes invalids; this
                # aligns Overview to match.
                row = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE status = 'open') AS open_n, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled', 'closed_early')) AS settled_n, "
                    # open_cost_mtm = mark-to-market value of open
                    # positions when the refresher has populated
                    # current_value_usd, otherwise cost basis. Drives
                    # the user-facing "Locked Capital" tile +
                    # WIN/LOSS / new_position Telegram blocks so the
                    # surfaces all agree.
                    # polymarket_runner.evaluate_open_positions writes
                    # current_value_usd = shares * outcomePrices for
                    # the held side every 60s.
                    "  COALESCE("
                    "    SUM(COALESCE(current_value_usd, cost_usd)) "
                    "      FILTER (WHERE status = 'open'),"
                    "    0"
                    "  ) AS open_cost_mtm, "
                    # open_cost_basis = pure cost basis (purchase
                    # price). Subtracted from MTM to compute unrealized
                    # P&L. MUST be a separate aggregate from
                    # open_cost_mtm above; if both are the COALESCE
                    # form, unrealized_pnl computes to 0 by
                    # construction and the Dashboard P&L tile + Total
                    # equity delta never move with the market.
                    "  COALESCE("
                    "    SUM(cost_usd) FILTER (WHERE status = 'open'),"
                    "    0"
                    "  ) AS open_cost_basis, "
                    "  COALESCE(SUM(realized_pnl_usd) FILTER (WHERE status IN ('settled', 'closed_early')), 0) AS realized, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled', 'closed_early') AND realized_pnl_usd > 0) AS wins "
                    "FROM pm_positions WHERE user_id = :uid AND mode = :m"
                ), {"uid": self.user_id, "m": self.mode}).fetchone()
                open_n         = int(row[0] or 0)
                settled_n      = int(row[1] or 0)
                open_cost      = float(row[2] or 0)   # MTM (legacy name)
                open_cost_basis = float(row[3] or 0)  # cost basis
                realized       = float(row[4] or 0)
                wins           = int(row[5] or 0)

                # Skipped evaluations live in market_evaluations, not
                # pm_positions (a skip never opens a position). Mode
                # is set at evaluation time (analyst writes the
                # current user_config.mode) so a "skip in live mode"
                # is distinct from "skip in simulation mode" — the
                # column was added in the same commit that ships
                # this query.
                skipped_row = conn.execute(text(
                    "SELECT COUNT(*) "
                    "  FROM market_evaluations "
                    " WHERE user_id = :uid "
                    "   AND mode = :m "
                    "   AND COALESCE(UPPER(recommendation), '') NOT IN "
                    "       ('BUY_YES', 'YES', 'BUY_NO', 'NO')"
                ), {"uid": self.user_id, "m": self.mode}).fetchone()
                skipped_n = int(skipped_row[0] or 0)
        except Exception as exc:
            print(f"[pm_executor] get_portfolio_stats({self.user_id}) failed: {exc}",
                  file=sys.stderr)
            open_n = settled_n = wins = skipped_n = 0
            open_cost = open_cost_basis = realized = 0.0
        # SINGLE source of truth for bankroll + equity, used by every
        # downstream surface (Dashboard /api/summary, settlement
        # Telegram messages via polymarket_runner, learning-cycle
        # bookkeeping).
        #
        # bankroll = wallet (real on-chain or DB formula) + pending
        #            redemption payouts for live winners that have
        #            settled but whose redeem+wrap chain hasn't yet
        #            credited the wallet.
        # equity   = bankroll + open_cost (cost basis of open positions).
        #
        # ═══════════════════════════════════════════════════════════
        # DO NOT WIDEN THIS QUERY. Read the regression test in
        # Delfibot/bot/tools/test_pending_payout_guards.py before
        # touching either of the two guards below. The bug fixed
        # here (2026-05-23, commit a718bc3) cost the user trust by
        # showing $10+ phantom money in Telegram messages and on the
        # dashboard for ~3 days because a stale invalid-market row
        # stayed in this projection forever. We've fixed the same
        # class of bug FOUR times. The user said: "engrave it
        # somewhere so it doesn't get fucked up anymore."
        # ═══════════════════════════════════════════════════════════
        #
        # Why the pending-payout projection:
        # The relayer redeem chain typically completes within 30-60s
        # of settle_position firing. During that window the real
        # wallet probe still shows the pre-redeem balance, so a WIN
        # notification would render "Balance: $X" where X doesn't
        # yet include the just-won money. Users read this as "the
        # win wasn't credited" even though it's already on its way.
        # The projection closes that gap: we add the expected payout
        # (cost_usd + realized_pnl_usd) for every settled winner whose
        # `redeem_tx_hash` is still NULL AND which settled in the
        # last 10 minutes.
        #
        # Two narrowings — DO NOT REMOVE EITHER:
        #
        # 1. status='invalid' rows EXCLUDED. Polymarket auto-refunds
        #    voided markets directly to the wallet — they don't go
        #    through the relayer redeem chain that pending_payout
        #    was built for. Including them caused the bug where an
        #    invalid market from 3 days earlier (#332, $10.88 cost)
        #    permanently inflated Balance by $10.88 across every
        #    dashboard refresh and every Telegram WIN/LOSS message
        #    — the wallet had ALREADY been refunded by Polymarket;
        #    the bot was double-counting.
        #
        # 2. 10-minute settled_at floor. The relayer redeem completes
        #    in 30-90s in steady state; anything older than 10 min
        #    means either the redeem completed and the tx-hash
        #    capture missed (so the wallet probe ALREADY reflects
        #    the payout) or the redeem never fired (a tracking bug
        #    we can't unblock by projecting forever). Either way the
        #    right answer is "stop projecting, trust the wallet."
        #
        # Sim mode never needs this: get_bankroll() already counts
        # realized P&L for settled rows via the DB formula.
        bankroll_wallet  = float(self.get_bankroll())
        pending_payout   = 0.0
        if self.mode == "live":
            try:
                with get_engine().begin() as conn:
                    pending = conn.execute(text(
                        "SELECT COALESCE(SUM("
                        "  cost_usd + COALESCE(realized_pnl_usd, 0)"
                        "), 0) "
                        "FROM pm_positions "
                        "WHERE user_id = :uid "
                        "  AND mode = 'live' "
                        "  AND status = 'settled' "
                        "  AND side = settlement_outcome "
                        "  AND redeem_tx_hash IS NULL "
                        "  AND settled_at IS NOT NULL "
                        "  AND settled_at > datetime('now', '-10 minutes')"
                    ), {"uid": self.user_id}).scalar() or 0.0
                    pending_payout = float(pending)
            except Exception as exc:
                print(
                    f"[pm_executor] pending-payout probe failed for "
                    f"{self.user_id}: {exc}",
                    file=sys.stderr,
                )

        # Locked Capital (= equity contribution from open positions).
        #
        # In LIVE mode we ask Polymarket's own data-api for the sum of
        # currentValue across EVERY position the wallet holds, including
        # ones the user opened manually outside the bot. That's the same
        # source Polymarket's "Portfolio" UI reads, so the Dashboard +
        # Telegram numbers reconcile to the cent. The bot's P&L,
        # win-rate, and position-count fields below stay scoped to
        # pm_positions (bot-tracked rows) so manual trades don't pollute
        # the bot's track record.
        #
        # On any failure (network, parse, non-2xx) we fall through to
        # `open_cost` from the SQL above, which is already
        # SUM(COALESCE(current_value_usd, cost_usd)) for bot-tracked
        # opens. That's a strictly worse but never-wrong floor.
        locked_capital = open_cost
        # When the data-api returns Polymarket's authoritative
        # cashPnl, we prefer it over local "MTM - cost basis" math
        # because (a) Polymarket uses mid-price for currentValue and
        # the bot's cost was recorded at execution price, (b)
        # Polymarket folds trading fees into initialValue. Local math
        # overstates unrealized by both deltas (saw +$1 vs Polymarket's
        # own portfolio UI). Falls back to local when the data-api is
        # unreachable.
        # Two data-api numbers we pull from Polymarket and surface
        # alongside our local computations:
        #   data_api_unrealized: sum of cashPnl across currently-
        #     held positions. Tracks the live mark-to-market gain on
        #     positions the user still owns. Useful for the unrealized
        #     tile but NOT what Polymarket's portfolio UI shows as
        #     "All-Time P&L".
        #   data_api_total_pnl: Polymarket's bookkeeping P&L across
        #     the whole wallet lifetime (deposits, withdraws, fills,
        #     redemptions). This IS the "All-Time P&L" number from
        #     their portfolio UI. /api/summary prefers this for
        #     `total_pnl` so the Dashboard headline matches the
        #     Polymarket page to the cent.
        data_api_unrealized: Optional[float] = None
        data_api_total_pnl: Optional[float] = None
        polymarket_closed_realized: Optional[float] = None
        polymarket_redeemable_cashPnl: Optional[float] = None
        if self.mode == "live":
            try:
                from engine.user_config import get_user_polymarket_creds
                from feeds.polymarket_wallet import (
                    get_total_open_positions_value,
                    cached_total_open_positions_cash_pnl,
                    cached_user_total_pnl,
                    cached_closed_realized_pnl,
                    cached_redeemable_cashPnl,
                    get_poly_signer_info,
                )
                creds = get_user_polymarket_creds(self.user_id)
                pk = (creds or {}).get("private_key") if creds else None
                if pk:
                    info = get_poly_signer_info(pk)
                    funder = (info or {}).get("funder")
                    if funder:
                        # currentValue is the cheap sub-second probe;
                        # leaving it on the request path because it
                        # rarely hangs and has its own non-blocking
                        # lock + stale-cache fallback. The slow
                        # endpoints (cashPnl, user-pnl, closed-pnl)
                        # are cache-only here so they can never hold
                        # an api_executor worker; the pm_pnl_refresh
                        # scheduler job keeps the caches warm.
                        total_open_value = get_total_open_positions_value(funder)
                        if total_open_value is not None:
                            locked_capital = float(total_open_value)
                        cash_pnl = cached_total_open_positions_cash_pnl(funder)
                        if cash_pnl is not None:
                            data_api_unrealized = float(cash_pnl)
                        user_pnl = cached_user_total_pnl(funder)
                        if user_pnl is not None:
                            data_api_total_pnl = float(user_pnl)
                        # Polymarket UI's "All-Time P&L" = sum of
                        # realizedPnl from /closed-positions PLUS
                        # cashPnl from /positions (redeemable +
                        # open). Surface both pieces so /api/summary
                        # can compose them.
                        closed_r = cached_closed_realized_pnl(funder)
                        if closed_r is not None:
                            polymarket_closed_realized = float(closed_r)
                        redeem_pnl = cached_redeemable_cashPnl(funder)
                        if redeem_pnl is not None:
                            polymarket_redeemable_cashPnl = float(redeem_pnl)
            except Exception as exc:
                print(
                    f"[pm_executor] locked_capital probe failed for "
                    f"{self.user_id}: {exc}",
                    file=sys.stderr,
                )

        bankroll = bankroll_wallet + pending_payout
        equity   = bankroll + locked_capital
        return {
            "mode":            self.mode,
            "starting_cash":   starting,
            "bankroll":        bankroll,
            "equity":          equity,
            "locked_capital":  locked_capital,  # data-api sum in live, DB sum in sim
            "open_positions":  open_n,
            "open_cost":       locked_capital,  # alias; kept for legacy callers
            # bot_open_cost = pure cost basis of bot-tracked opens.
            # /api/summary subtracts it from open_cost (MTM) to get
            # unrealized_pnl, so this must be cost, not MTM. Was
            # silently MTM before because both fields used the same
            # COALESCE(current_value_usd, cost_usd) aggregate.
            "bot_open_cost":   open_cost_basis,
            "settled_total":   settled_n,
            "settled_wins":    wins,
            "skipped_total":   skipped_n,
            "win_rate":        (wins / settled_n) if settled_n else None,
            "realized_pnl":    realized,
            # data_api_unrealized = sum of cashPnl from data-api
            # /positions, currently-open rows only (non-redeemable).
            # Per-position live MTM gain on what the user still
            # owns.
            "data_api_unrealized": data_api_unrealized,
            # data_api_total_pnl = Polymarket's "All-Time P&L" from
            # user-pnl-api/user-pnl. Legacy reading kept for any
            # surface that hasn't been migrated; /api/summary now
            # prefers polymarket_closed_realized +
            # polymarket_redeemable_cashPnl + data_api_unrealized
            # which matches the polymarket.com portfolio tile.
            "data_api_total_pnl": data_api_total_pnl,
            # Polymarket UI components. Sum of these three equals
            # the "All-Time P&L" number Polymarket displays:
            #   polymarket_closed_realized      (closed winners)
            #   polymarket_redeemable_cashPnl   (settled losers)
            #   data_api_unrealized             (open positions MTM)
            "polymarket_closed_realized":   polymarket_closed_realized,
            "polymarket_redeemable_cashPnl": polymarket_redeemable_cashPnl,
            "ready":           True,
        }

    # ── Open a position ──────────────────────────────────────────────────────
    def open_position(
        self,
        market:        PolyMarket,
        decision:      SizingDecision,
        delfi_probability: float,
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
            pos_id = self._open_live(market, decision, delfi_probability,
                                      prediction_id, reasoning, category,
                                      market_archetype)
        else:
            pos_id = self._open_simulation(market, decision, delfi_probability,
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

    def _open_simulation(self, market, decision, delfi_p,
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
                    "  delfi_probability, ev_bps, confidence, "
                    "  mode, status, expected_resolution_at, reasoning, event_slug, "
                    "  market_archetype, venue, "
                    "  volume_24h_at_entry, liquidity_at_entry"
                    ") VALUES ("
                    "  :user_id, :pid, :mid, :cid, :slug, :q, :cat, "
                    "  :side, :shares, :ep, :cost, "
                    "  :cp, :ev_bps, :conf, "
                    "  :mode, 'open', :exp, :reason, :event_slug, "
                    "  :arch, :venue, "
                    "  :vol24h, :liq"
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
                    "cp":    delfi_p,
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
                    # Per-trade richness: market thinness signals at
                    # entry time. Lets future ROI analysis slice by
                    # volume / liquidity bands. NULL-safe: a missing
                    # value on a malformed gamma row stays NULL, not 0.
                    "vol24h": getattr(market, "volume_24h_clob", None),
                    "liq":    getattr(market, "liquidity_num", None),
                }).fetchone()
                return int(row[0]) if row else None
        except Exception as exc:
            print(f"[pm_executor] _open_simulation failed: {exc}", file=sys.stderr)
            return None

    def _open_live(self, market: PolyMarket, decision: SizingDecision, delfi_p: float,
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

        Venue:
            v1 is offshore Polymarket only (EIP-712 against the V2 CTF
            Exchange on Polygon). The CFTC-regulated US venue (QCEX,
            API-key signing) is not supported.
        """
        if not self.ready:
            print(f"[pm_executor] _open_live refused: user {self.user_id} not ready",
                  file=sys.stderr)
            return None

        # ── V2 signer-mismatch short-circuit ────────────────────────────
        # If we've already detected the Polymarket V2 "signer != api-key"
        # rejection in this process, every subsequent order will fail the
        # same way. Skip the API call + the Telegram noise; fall back to a
        # simulation fill with a clear marker so the user sees what would
        # have happened. The flag clears on credential change or restart.
        global _V2_SIGNER_MISMATCH_DETECTED, _V2_SIGNER_MISMATCH_NOTIFIED
        if _V2_SIGNER_MISMATCH_DETECTED:
            print(
                f"[pm_executor] _open_live: V2 signer mismatch was detected "
                f"earlier this session; routing to simulation fill instead of "
                f"posting. User must paste Trading API Keys from polymarket.com "
                f"to clear this state.",
                file=sys.stderr,
            )
            return self._open_simulation(
                market, decision, delfi_p, prediction_id,
                f"[v2 signer mismatch] {(reasoning or '')}".strip(),
                category, market_archetype,
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
                market, decision, delfi_p, prediction_id,
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
        # Derive the funder address once up front. POSITIONS live on the
        # funder (the proxy contract for sig_type=1/2 accounts, the EOA
        # for sig_type=0); we need this address to probe data-api below
        # when the CLOB fill poll times out. Cached in
        # get_poly_signer_info so repeated calls are cheap.
        try:
            from feeds.polymarket_wallet import get_poly_signer_info
            _info = get_poly_signer_info(private_key)
            funder_address: Optional[str] = (_info or {}).get("funder")
        except Exception:
            funder_address = None
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

        # ── Wallet balance pre-check ────────────────────────────────────
        # The sizer computes stake from the internal DB bankroll, which
        # may exceed the wallet's actual pUSD balance (e.g. user set
        # starting_cash=$1000 but only deposited $25). Cap the stake at
        # the real on-chain balance so we don't hammer the CLOB with
        # orders it will reject for "not enough balance".
        try:
            from feeds.polymarket_wallet import get_poly_signer_info as _signer_info
            _info = _signer_info(private_key)
            if _info is not None:
                wallet_bal = float(_info.get("balance", 0.0) or 0.0)
                if wallet_bal < _MIN_ABSOLUTE_STAKE_USD:
                    print(
                        f"[pm_executor] _open_live: wallet balance "
                        f"${wallet_bal:.2f} < minimum ${_MIN_ABSOLUTE_STAKE_USD:.2f} "
                        f"- skipping order on '{market.question[:60]}'",
                        flush=True,
                    )
                    return None
                if decision.stake_usd > wallet_bal:
                    old_stake = decision.stake_usd
                    decision.stake_usd = round(wallet_bal, 4)
                    decision.shares = round(
                        decision.stake_usd / float(decision.entry_price), 4
                    )
                    print(
                        f"[pm_executor] _open_live: capped stake "
                        f"${old_stake:.2f} -> ${decision.stake_usd:.2f} "
                        f"(wallet balance ${wallet_bal:.2f}) "
                        f"on '{market.question[:60]}'",
                        flush=True,
                    )
        except Exception as _bex:
            print(f"[pm_executor] _open_live: balance pre-check failed: {_bex}",
                  file=sys.stderr)

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

        # ── DEEP-DIVE INSTRUMENTATION (2026-05-17) ────────────────────
        # User asked for hard data + proof. Split the SDK's
        # create_and_post_order into its two parts so we can log the
        # FULL order struct AND the api-keys Polymarket has on file
        # for this account, BEFORE the post that gets rejected.
        # When orders start succeeding we can dial this back to
        # debug=False or remove entirely.
        try:
            builder = getattr(client, "builder", None)
            print(
                f"[pm_executor][live] PRE-BUILD maker_seed={getattr(builder,'funder',None)!r} "
                f"sig_type_seed={getattr(builder,'signature_type',None)!r} "
                f"signer_seed={getattr(getattr(builder,'signer',None),'address',lambda:None)()!r} "
                f"market={market.id} side={decision.side} "
                f"price={entry_price} size={size_shares}",
                flush=True,
            )
        except Exception:
            pass

        # Inspect Polymarket's registered api-keys for this account.
        # Each entry tells us the address Polymarket has bound the
        # key to. If order.signer doesn't match any of these, we
        # get "signer address has to be the address of the API KEY".
        try:
            keys_info = client.get_api_keys()
            print(f"[pm_executor][live] api-keys on file: {keys_info!r}",
                  flush=True)
        except Exception as ek:
            print(f"[pm_executor][live] get_api_keys() failed: {ek}",
                  flush=True)

        # Build the order WITHOUT posting it yet.
        try:
            order = client.create_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size=DEFAULT_TICK_SIZE),
            )
            print(
                f"[pm_executor][live] BUILT ORDER "
                f"maker={getattr(order, 'maker', None)!r} "
                f"signer={getattr(order, 'signer', None)!r} "
                f"signatureType={getattr(order, 'signatureType', None)!r} "
                f"tokenId={getattr(order, 'tokenId', None)!r} "
                f"signature_len={len(getattr(order, 'signature', '') or '')}",
                flush=True,
            )
        except Exception as ec:
            print(f"[pm_executor][live] create_order failed: {ec}", flush=True)
            order = None

        try:
            if order is not None:
                resp = client.post_order(order, OrderType.GTC)
            else:
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
            # Surface the failure to the user. Until now these would
            # disappear into stderr and the user only noticed because
            # the dashboard wasn't growing. log_event writes a row to
            # event_log AND fires Telegram (when configured) via the
            # "order_error" notification category. The description
            # captures the market, side, size, and exact Polymarket
            # error so it's actionable from the Errors tab.
            #
            # Detect Polymarket's V2 "signer != api-key address" rejection.
            # If we see it once we'll see it on EVERY order this session,
            # so flip the process-level gate and emit a SINGLE actionable
            # event for the user. Subsequent orders short-circuit to
            # simulation fills at the top of _open_live, so we don't spam
            # the Errors tab or Telegram.
            err_msg = str(exc)[:600]
            is_signer_mismatch = _is_v2_signer_mismatch(err_msg)
            try:
                from db.logger import log_event
                from feeds import telegram_messages as _tm
                # Full question text. The earlier 80-char truncation made
                # the Errors tab's "category" lookup fail for any market
                # whose question exceeded 80 chars (Errors tab matches
                # the parsed question against pm_positions / market_
                # evaluations by full text). Reported 2026-05-24: all
                # Elon-tweet and Spurs/Thunder errors showed Category="-"
                # because their full questions ran past 80 chars.
                # event_log.description is TEXT, no length cap on the
                # column; the truncation was vestigial.
                question_short = market.question or ""
                if is_signer_mismatch:
                    _V2_SIGNER_MISMATCH_DETECTED = True
                    description = (
                        "Polymarket rejected the order because its CLOB has a "
                        "different address authorised as the trading signer "
                        "for this wallet. This usually happens when the "
                        "account was created via the Polymarket web UI first "
                        "(Magic.link session key) and the MetaMask key "
                        "pasted into Delfi was never registered as a "
                        "trading signer. "
                        "FIX: go to polymarket.com -> Settings -> API Keys, "
                        "generate Trading API Keys, and paste the api-key, "
                        "secret, and passphrase into Delfi -> Settings -> "
                        "Polymarket API Key. Until then live orders fall "
                        "back to simulation fills so trading data keeps "
                        "flowing."
                    )
                    telegram_html = None
                    try:
                        telegram_html = _tm.order_rejected(
                            question="Polymarket signer mismatch",
                            side=decision.side,
                            stake_usd=decision.stake_usd,
                            price=entry_price,
                            error_text=description,
                            mode=self.trading_mode,
                        )
                    except Exception:
                        telegram_html = None
                    # Only one row + one Telegram per process - subsequent
                    # short-circuited orders never hit this branch.
                    if not _V2_SIGNER_MISMATCH_NOTIFIED:
                        log_event(
                            event_type="order_error",
                            severity=3,  # higher severity, action required
                            description=description,
                            source="pm_executor._open_live",
                            telegram_html=telegram_html,
                        )
                        _V2_SIGNER_MISMATCH_NOTIFIED = True
                else:
                    description = (
                        f"Order rejected on '{question_short}': "
                        f"{decision.side} {size_shares:.2f}@${entry_price:.3f}. "
                        f"{err_msg}"
                    )
                    # Rich Telegram-HTML matches the rest of the message
                    # spec (new_position / settled_win / settled_loss).
                    try:
                        telegram_html = _tm.order_rejected(
                            question=market.question or "(unknown market)",
                            side=decision.side,
                            stake_usd=decision.stake_usd,
                            price=entry_price,
                            error_text=err_msg,
                            mode=self.trading_mode,
                        )
                    except Exception:
                        telegram_html = None
                    log_event(
                        event_type="order_error",
                        severity=2,  # warning, not fatal
                        description=description,
                        source="pm_executor._open_live",
                        telegram_html=telegram_html,
                    )
            except Exception as log_exc:
                print(
                    f"[pm_executor] could not log order_error event: {log_exc}",
                    file=sys.stderr,
                )
            # If this was the signer mismatch, fall back to simulation so
            # the trade still lands in the dashboard with a clear marker.
            if is_signer_mismatch:
                return self._open_simulation(
                    market, decision, delfi_p, prediction_id,
                    f"[v2 signer mismatch] {(reasoning or '')}".strip(),
                    category, market_archetype,
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

        # Invalidate the signer-info cache so the NEXT order attempt in
        # this scan re-probes the on-chain balance. Without this, the
        # wallet balance pre-check above still sees the pre-order (higher)
        # balance for up to 5 minutes and lets a second order through even
        # though the wallet is now empty.
        try:
            from feeds.polymarket_wallet import invalidate_signer_cache as _inval
            _inval(private_key)
        except Exception:
            pass

        # ── Wait for fill, then persist actual fill (NOT intent) ────────
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

        # Extract ACTUAL filled size from the CLOB response. The original
        # `decision` carries the LIMIT-order intent (5 shares × ask),
        # but the order may have partially filled or not filled at all
        # (e.g. micro-window market closed before the order matched, or
        # the limit price was below the live ask). Persisting the
        # intent values produced "ghost positions" on the dashboard
        # that don't exist on-chain (user-reported 2026-05-18:
        # Delfi showed 3 open positions while Polymarket showed only 1).
        filled_shares = _extract_filled_size(final, resp)
        filled_cost   = _extract_filled_cost(
            final, resp, filled_shares,
            fallback_price=entry_price,
            client=client, order_id=str(order_id),
        )

        if filled_shares <= 0:
            # Zero fill PER THE CLOB POLL. But the CLOB matcher
            # frequently lands the fill on-chain BEFORE its public
            # poll endpoint reflects the match. The 2-minute
            # reconciler safety net catches this eventually, but
            # the analyst could re-open into the same market in the
            # meantime. Synchronously check data-api/positions
            # against this market's conditionId BEFORE cancelling -
            # if the position is already on-chain, persist it from
            # the on-chain truth and skip the cancel. Closes the
            # 2-min reconciler gap to ~zero on this hot path.
            on_chain = _lookup_on_chain_position(
                funder_address=funder_address,
                condition_id=getattr(market, "condition_id", None),
                side=decision.side,
            )
            if on_chain is not None:
                print(
                    f"[pm_executor][live] zero-fill per poll but data-api "
                    f"shows fill landed on-chain "
                    f"(size={on_chain.get('size')}, "
                    f"avg=${on_chain.get('avgPrice')}). Persisting from "
                    f"on-chain truth instead of cancelling.",
                    flush=True,
                )
                from dataclasses import replace as _replace
                onchain_shares = float(on_chain.get("size") or 0.0)
                onchain_avg    = float(on_chain.get("avgPrice") or 0.0)
                try:
                    decision = _replace(
                        decision,
                        shares=onchain_shares,
                        stake_usd=onchain_shares * onchain_avg,
                        entry_price=onchain_avg,
                    )
                except TypeError:
                    try:
                        decision.shares     = onchain_shares  # type: ignore[misc]
                        decision.stake_usd  = onchain_shares * onchain_avg  # type: ignore[misc]
                        decision.entry_price = onchain_avg  # type: ignore[misc]
                    except Exception:
                        pass
                return self._persist_live_position(
                    market=market, decision=decision, delfi_p=delfi_p,
                    prediction_id=prediction_id,
                    reasoning=reasoning, category=category,
                    market_archetype=market_archetype,
                    clob_order_id=str(order_id),
                    tx_hash=None,
                )

            # Genuine zero-fill (or the CLOB really didn't match).
            # Cancel the order if it's still on the book and do NOT
            # persist a position row. The dashboard would otherwise
            # show a fake "open position" with no on-chain counterpart.
            try:
                if final_status not in (
                    "CANCELED", "CANCELLED", "REJECTED",
                ):
                    # py-clob-client-v2 has two cancel APIs:
                    #   cancel_order(payload: OrderPayload) - takes an
                    #     object with .orderID; raises
                    #     "'str' object has no attribute 'orderID'"
                    #     when passed a bare hash string.
                    #   cancel_orders(order_hashes: list[str]) - takes
                    #     a list of order-hash strings, which is what
                    #     we have here.
                    # Use the plural form. The earlier single-form call
                    # was throwing on every zero-fill order, leaving
                    # them live on Polymarket's order book forever.
                    client.cancel_orders([str(order_id)])
            except Exception as exc:
                print(
                    f"[pm_executor] _open_live: cancel of order "
                    f"{order_id} failed: {exc}",
                    file=sys.stderr,
                )
            try:
                from db.logger import log_event
                from feeds import telegram_messages as _tm
                description = (
                    f"Order placed but never filled on "
                    f"'{(market.question or '')[:80]}': "
                    f"{decision.side} {decision.shares:.2f}@"
                    f"${entry_price:.3f}. Status: {final_status!r}. "
                    f"Skipping the trade — no on-chain position created."
                )
                try:
                    telegram_html = _tm.order_rejected(
                        question=market.question or "(unknown market)",
                        side=decision.side,
                        stake_usd=decision.stake_usd,
                        price=entry_price,
                        error_text=(
                            f"Order placed on Polymarket but no fill "
                            f"within {FILL_POLL_TIMEOUT_SECONDS:.0f}s. "
                            f"Likely no liquidity at the chosen price, "
                            f"or the market closed before matching. "
                            f"No position opened."
                        ),
                        mode=self.trading_mode,
                    )
                except Exception:
                    telegram_html = None
                log_event(
                    event_type="order_rejected",
                    severity=2,
                    description=description,
                    source="pm_executor._open_live",
                    telegram_html=telegram_html,
                )
            except Exception as log_exc:
                print(
                    f"[pm_executor] could not log no-fill event: {log_exc}",
                    file=sys.stderr,
                )
            return None

        # Partial fill: scale the position row to reflect what actually
        # landed on-chain. `decision` is the SizingDecision dataclass
        # which is frozen — we can't mutate it — so swap to a copy with
        # the actual numbers. Keep the original entry_price (it's the
        # limit price, which equals the fill price for marketable BUYs
        # on Polymarket V2 unless price-improved).
        if filled_shares < decision.shares - 1e-9:
            print(
                f"[pm_executor][live] partial fill on order {order_id}: "
                f"intent {decision.shares:.2f} sh @ "
                f"${entry_price:.3f} = ${decision.stake_usd:.2f}, "
                f"actual {filled_shares:.2f} sh @ "
                f"${filled_cost/filled_shares:.3f} = ${filled_cost:.2f}",
                flush=True,
            )
            try:
                from dataclasses import replace as _replace
                decision = _replace(
                    decision,
                    shares=filled_shares,
                    stake_usd=filled_cost,
                    entry_price=filled_cost / filled_shares,
                )
            except TypeError:
                # `decision` isn't a dataclass or doesn't support replace;
                # fall back to mutating attributes in-place.
                try:
                    decision.shares     = filled_shares  # type: ignore[misc]
                    decision.stake_usd  = filled_cost  # type: ignore[misc]
                    decision.entry_price = (
                        filled_cost / filled_shares
                    )  # type: ignore[misc]
                except Exception:
                    pass

        return self._persist_live_position(
            market=market, decision=decision, delfi_p=delfi_p,
            prediction_id=prediction_id,
            reasoning=reasoning, category=category,
            market_archetype=market_archetype,
            clob_order_id=str(order_id),
            tx_hash=str(tx_hash) if tx_hash else None,
        )

    def _persist_live_position(
        self, *, market: PolyMarket, decision: SizingDecision, delfi_p: float,
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
                    "  delfi_probability, ev_bps, confidence, "
                    "  mode, status, expected_resolution_at, reasoning, event_slug, "
                    "  market_archetype, venue, clob_order_id, tx_hash, "
                    "  volume_24h_at_entry, liquidity_at_entry"
                    ") VALUES ("
                    "  :user_id, :pid, :mid, :cid, :slug, :q, :cat, "
                    "  :side, :shares, :ep, :cost, "
                    "  :cp, :ev_bps, :conf, "
                    "  'live', 'open', :exp, :reason, :event_slug, "
                    "  :arch, :venue, :order_id, :tx_hash, "
                    "  :vol24h, :liq"
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
                    "cp":      delfi_p,
                    "ev_bps":  decision.ev * 10_000.0,
                    "conf":    decision.confidence,
                    "exp":     market.resolution_at_estimate,
                    "reason":  (reasoning or "")[:4000] or None,
                    "event_slug": getattr(market, "event_slug", None),
                    "arch":    market_archetype,
                    "venue":   getattr(self._user_config, "venue", "polymarket"),
                    "order_id": clob_order_id,
                    "tx_hash": tx_hash,
                    # Per-trade richness mirrors _open_simulation. See
                    # the comment there.
                    "vol24h":  getattr(market, "volume_24h_clob", None),
                    "liq":     getattr(market, "liquidity_num", None),
                }).fetchone()
                return int(row[0]) if row else None
        except Exception as exc:
            print(f"[pm_executor] _persist_live_position failed: {exc}",
                  file=sys.stderr)
            return None

    # ── Close a position EARLY (exit policy) ────────────────────────────────
    def close_position_early(
        self,
        position_id:  int,
        reason:       str,                  # 'take_profit' | 'stop_loss' | 'time_decay'
        details:      str,
        current_bid:  float,                # the bid we expect to sell at
        clob_token_id: Optional[str] = None,
    ) -> bool:
        """
        Close an open position before its natural Polymarket settlement,
        because the user's exit policy (take-profit, stop-loss, or
        time-decay) tripped. Mirrors settle_position's contract but
        records the row with `status='closed_early'`, a non-null
        `closed_at`, and a `close_reason` so dashboards and review
        reports can distinguish a discretionary exit from a natural
        resolution.

        Simulation mode:
            No order placed. The exit is recorded with realized P&L
            computed against `current_bid` (`pnl = shares*bid - cost`).
            This is the "what would have happened if we'd exited" path
            and it must match the live path's accounting exactly so a
            user comparing the two modes is comparing apples to apples.

        Live mode:
            Places a SELL CLOB order at `current_bid` for the held
            outcome's clob_token_id, polls for fill, then UPDATEs the
            row. Subject to the same kill-switch as _open_live - with
            DELFI_LIVE_KILLSWITCH_OFF unset we record a paper close
            instead of hitting the CLOB.

        Returns True iff the row was updated (live: order placed AND
        DB committed; simulation: DB committed). The natural-resolution
        backfill in polymarket_runner is responsible for stamping
        `counterfactual_pnl_usd` once the market itself settles - that
        is NOT this method's job and never blocks the exit.
        """
        if reason not in ("take_profit", "stop_loss", "time_decay"):
            print(f"[pm_executor] close_position_early: bad reason {reason!r}",
                  file=sys.stderr)
            return False
        if current_bid is None or current_bid <= 0.0:
            print(f"[pm_executor] close_position_early: invalid bid "
                  f"{current_bid!r} for pos {position_id}", file=sys.stderr)
            return False

        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT side, shares, cost_usd, prediction_id, "
                    "       mode, condition_id, question "
                    "FROM pm_positions "
                    "WHERE id = :pid AND user_id = :uid AND status = 'open'"
                ), {"pid": position_id, "uid": self.user_id}).fetchone()
                if row is None:
                    print(f"[pm_executor] close_position_early: position "
                          f"{position_id} not open for user {self.user_id}",
                          file=sys.stderr)
                    return False
                side         = str(row[0])
                shares       = float(row[1])
                cost_usd     = float(row[2])
                pred_id      = int(row[3]) if row[3] is not None else None
                row_mode     = str(row[4]) if row[4] is not None else ""
                condition_id = str(row[5]) if row[5] is not None else ""
                question     = str(row[6] or "")
        except Exception as exc:
            print(f"[pm_executor] close_position_early read failed: {exc}",
                  file=sys.stderr)
            return False

        # Live-CLOB SELL leg. Only runs for live-mode rows with the
        # kill-switch off AND a non-empty token_id; otherwise we record
        # a paper close so the dashboard accounting still moves.
        clob_order_id: Optional[str] = None
        tx_hash: Optional[str] = None
        if (
            row_mode == "live"
            and _live_killswitch_off()
            and clob_token_id
        ):
            try:
                clob_order_id, tx_hash = self._place_close_order(
                    token_id=clob_token_id,
                    shares=shares,
                    sell_price=float(current_bid),
                )
            except Exception as exc:
                # Order placement failure is NOT silent - we surface it
                # but we DO NOT update the row. The next exit-policy
                # tick can retry. Returning False means the caller skips
                # the Telegram notification.
                print(f"[pm_executor] close_position_early SELL failed for "
                      f"pos {position_id}: {exc}", file=sys.stderr)
                try:
                    from db.logger import log_event
                    log_event(
                        event_type="order_error",
                        severity=2,
                        description=(
                            f"Early-exit SELL rejected on "
                            f"'{question[:120]}': {str(exc)[:300]}"
                        ),
                        source="pm_executor.close_position_early",
                    )
                except Exception:
                    pass
                return False

        proceeds = shares * float(current_bid)
        pnl = proceeds - cost_usd

        try:
            with get_engine().begin() as conn:
                conn.execute(text(
                    "UPDATE pm_positions SET "
                    "  status                = 'closed_early', "
                    "  closed_at             = CURRENT_TIMESTAMP, "
                    "  settled_at            = CURRENT_TIMESTAMP, "
                    "  close_reason          = :reason, "
                    "  settlement_price      = :sp, "
                    "  realized_pnl_usd      = :pnl, "
                    "  close_clob_order_id   = :oid, "
                    "  close_tx_hash         = :tx "
                    "WHERE id = :pid"
                ), {
                    "reason": reason,
                    "sp":     float(current_bid),
                    "pnl":    float(pnl),
                    "oid":    clob_order_id,
                    "tx":     tx_hash,
                    "pid":    position_id,
                })
        except Exception as exc:
            print(f"[pm_executor] close_position_early UPDATE failed for "
                  f"pos {position_id}: {exc}", file=sys.stderr)
            return False

        # Calibration ledger: an early exit is informative for
        # learning. We resolve the prediction tentatively with the
        # exit P&L. The natural-resolution backfill will later compute
        # `counterfactual_pnl_usd` so the review report can ask "was
        # this exit premature?". We don't try to mark the prediction
        # right/wrong here because the market hasn't actually resolved
        # yet - that's what `counterfactual_pnl_usd` is for.
        # NOTE: we deliberately DO NOT feed calibration.resolve_prediction
        # because that would lock the binary outcome at the exit
        # moment and the eventual-natural-resolution outcome (which
        # we may take a counterfactual loss against) would be ignored
        # by Brier scoring. The prediction stays unresolved in
        # calibration until polymarket_runner's natural-settlement
        # path fires `counterfactual_pnl_usd` and we close the loop.
        _ = pred_id  # explicitly unused for now

        print(
            f"[pm_executor][{row_mode}] closed-early pm_position {position_id}: "
            f"{reason} @ ${current_bid:.3f} ({details}), pnl ${pnl:+.2f}",
            flush=True,
        )

        # Emit a position_settled-style event so the dashboard and
        # Telegram both pick it up. Win/loss styling decided by pnl
        # sign rather than market-vs-side, because the market hasn't
        # actually resolved - this is the EXIT P&L only.
        try:
            from db.logger import log_event
            from feeds import telegram_messages as _tm
            try:
                # Mode-scoped stats — same pattern as the natural-settle
                # path in polymarket_runner.
                if row_mode == self.mode:
                    stats = self.get_portfolio_stats()
                else:
                    other = PMExecutor(
                        self.user_id, view_mode_override=row_mode,
                    )
                    stats = other.get_portfolio_stats()
                bankroll_after = float(stats.get("bankroll", 0.0))
                equity_after   = float(stats.get("equity",   bankroll_after))
            except Exception:
                bankroll_after = self.get_bankroll()
                equity_after   = bankroll_after
            roi = (pnl / cost_usd) if cost_usd > 0 else 0.0
            common = dict(
                question=question,
                side=side,
                reason=reason,
                pnl=pnl,
                roi=roi,
                bankroll=bankroll_after,
                equity=equity_after,
                mode=row_mode or "simulation",
                details=details,
            )
            if pnl >= 0:
                telegram_html = _tm.early_exit_win(**common)
            else:
                telegram_html = _tm.early_exit_loss(**common)
            log_event(
                event_type="position_closed_early",
                severity=20,
                description=(
                    f"Closed early ({reason}) on '{question[:120]}': "
                    f"{details}, P&L ${pnl:+.2f}, "
                    f"mode {row_mode or 'simulation'}, position={position_id}"
                ),
                source="pm_executor.close_position_early",
                telegram_html=telegram_html,
            )
        except Exception as exc:
            print(f"[pm_executor] close_position_early event log failed: "
                  f"{exc}", file=sys.stderr)

        return True

    def _place_close_order(
        self,
        *,
        token_id:   str,
        shares:     float,
        sell_price: float,
    ) -> tuple[str, Optional[str]]:
        """
        Submit a CLOB SELL order for `shares` of the held outcome at
        `sell_price`. Returns (order_id, tx_hash). Raises on any
        rejection so the caller's UPDATE is skipped.

        Mirrors `_open_live`'s order construction but with side=SELL
        and reuses the same client cache + tick-size rounding.
        """
        creds = get_active_polymarket_creds(self._user_config)
        wallet      = (creds.get("wallet_address") or "").strip()
        private_key = (creds.get("private_key")    or "").strip()
        if not wallet or not private_key:
            raise RuntimeError("missing wallet/private_key on user_config")

        client = _get_clob_client(wallet, private_key)
        from py_clob_client_v2.clob_types import (   # type: ignore
            OrderArgs, OrderType, PartialCreateOrderOptions,
        )
        try:
            from py_clob_client_v2.order_builder.constants import SELL  # type: ignore
            sell_side = SELL
        except Exception:
            sell_side = "SELL"
        price = round(float(sell_price), 2)
        size  = round(float(shares), 4)
        args = OrderArgs(token_id=token_id, price=price, side=sell_side, size=size)
        resp = client.create_and_post_order(
            order_args=args,
            options=PartialCreateOrderOptions(tick_size=DEFAULT_TICK_SIZE),
            order_type=OrderType.GTC,
        )
        if not isinstance(resp, dict):
            raise RuntimeError(f"unexpected SELL response shape: {resp!r}")
        order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id")
        if not order_id:
            raise RuntimeError(f"SELL response missing order id: {resp!r}")
        final = _poll_order_filled(client, str(order_id))
        tx_hash = (
            final.get("transactionHash")
            or (final.get("transactionsHashes") or [None])[0]
            or resp.get("transactionHash")
        )
        return str(order_id), (str(tx_hash) if tx_hash else None)

    # ── Backfill counterfactual P&L on an already-closed-early row ──────────
    def backfill_counterfactual_pnl(
        self,
        position_id:      int,
        winning_outcome:  str,                # 'YES' | 'NO' | 'INVALID'
        settlement_price: Optional[float] = None,
    ) -> bool:
        """
        Once a market we'd already exited early reaches its natural
        Polymarket resolution, write `counterfactual_pnl_usd` onto the
        row so the review report can score whether the exit was wise.

        `counterfactual_pnl_usd` = (the P&L we'd have realized if we'd
        held to resolution) - (the P&L we actually got from the early
        exit). Positive means the exit was premature (we left money on
        the table); negative means the exit was wise (we dodged a loss).
        Used by `engine/review_report.py` to summarise exit quality.

        Idempotent: a second call on the same row updates the same
        column with the same number. Safe to call from the resolve
        sweep on every tick.
        """
        outcome = (winning_outcome or "").upper()
        if outcome not in ("YES", "NO", "INVALID"):
            return False
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT side, shares, cost_usd, realized_pnl_usd "
                    "  FROM pm_positions "
                    " WHERE id = :pid AND user_id = :uid "
                    "   AND status = 'closed_early'"
                ), {"pid": position_id, "uid": self.user_id}).fetchone()
                if row is None:
                    return False
                side       = str(row[0])
                shares     = float(row[1])
                cost_usd   = float(row[2])
                exit_pnl   = float(row[3] or 0.0)
                if settlement_price is None:
                    if outcome == "INVALID":
                        settlement_price = 0.5
                    else:
                        settlement_price = 1.0 if side == outcome else 0.0
                # What we would have made by holding to resolution.
                hold_pnl = shares * float(settlement_price) - cost_usd
                counterfactual = hold_pnl - exit_pnl
                conn.execute(text(
                    "UPDATE pm_positions "
                    "   SET counterfactual_pnl_usd = :cf, "
                    "       settlement_outcome    = :out, "
                    "       settlement_price      = COALESCE(settlement_price, :sp) "
                    " WHERE id = :pid"
                ), {
                    "cf":  float(counterfactual),
                    "out": outcome,
                    "sp":  float(settlement_price),
                    "pid": position_id,
                })
            return True
        except Exception as exc:
            print(f"[pm_executor] backfill_counterfactual_pnl failed for "
                  f"pos {position_id}: {exc}", file=sys.stderr)
            return False

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

        Live mode (post-2026-05-03): on settlement of a live position
        with held outcome tokens, this method also calls
        CTF.redeemPositions on Polygon to convert the ERC-1155 tokens
        into pUSD. The redemption is gated by DELFI_LIVE_KILLSWITCH_OFF
        - same env var as _open_live - and short-circuits silently
        when off. The redeem tx hash is persisted in
        `pm_positions.redeem_tx_hash` for the operator's audit trail.
        Losers and simulation rows skip the redeem path entirely.
        """
        outcome = (winning_outcome or "").upper()
        if outcome not in ("YES", "NO", "INVALID"):
            print(f"[pm_executor] settle: invalid outcome {outcome!r}",
                  file=sys.stderr)
            return False

        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT side, shares, cost_usd, prediction_id, "
                    "       mode, condition_id "
                    "FROM pm_positions "
                    "WHERE id = :pid AND user_id = :uid AND status = 'open'"
                ), {"pid": position_id, "uid": self.user_id}).fetchone()
                if row is None:
                    print(f"[pm_executor] settle: position {position_id} not open "
                          f"for user {self.user_id}", file=sys.stderr)
                    return False
                side          = str(row[0])
                shares        = float(row[1])
                cost_usd      = float(row[2])
                pred_id       = int(row[3]) if row[3] is not None else None
                row_mode      = str(row[4]) if row[4] is not None else ""
                condition_id  = str(row[5]) if row[5] is not None else ""

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

            # ── On-chain redemption (live winners only) ────────────────
            # Runs OUTSIDE the begin() block above so a slow Polygon
            # round-trip can't hold a write lock on the SQLite DB. The
            # redeemer self-gates on DELFI_LIVE_KILLSWITCH_OFF; with
            # the switch on (default) it returns immediately without
            # touching the network.
            if row_mode == "live":
                try:
                    from execution.pm_redeemer import (
                        redeem_winning_position, index_sets_for_outcome,
                    )
                    needs_redeem = index_sets_for_outcome(side, outcome) is not None
                    if needs_redeem:
                        creds = get_active_polymarket_creds(self._user_config)
                        wallet      = (creds.get("wallet_address") or "").strip()
                        private_key = (creds.get("private_key")    or "").strip()
                        result = redeem_winning_position(
                            condition_id=condition_id,
                            side=side,
                            outcome=outcome,
                            wallet=wallet,
                            private_key=private_key,
                        )
                        if result.tx_hash:
                            try:
                                with get_engine().begin() as conn2:
                                    conn2.execute(text(
                                        "UPDATE pm_positions "
                                        "SET redeem_tx_hash = :tx "
                                        "WHERE id = :pid"
                                    ), {"tx": result.tx_hash, "pid": position_id})
                            except Exception as exc:
                                print(
                                    f"[pm_executor] redeem_tx_hash persist "
                                    f"failed for pos {position_id}: {exc}",
                                    file=sys.stderr,
                                )
                        if not result.redeemed:
                            print(
                                f"[pm_executor] redeem skipped or failed for "
                                f"pos {position_id}: {result.error}",
                                file=sys.stderr,
                            )
                        # Force-refresh the cached wallet probe so the
                        # WIN Telegram message (rendered right after
                        # settle_position returns) shows a Balance that
                        # already includes the just-credited payout.
                        # Without this, the probe stays cached up to 60s
                        # and the notification displays a pre-payout
                        # balance, making users think the win wasn't
                        # credited. Cheap: one extra Polygon RPC probe,
                        # only on actual winners.
                        if result.redeemed and private_key:
                            try:
                                from feeds.polymarket_wallet import (
                                    refresh_live_balance_cache,
                                )
                                refresh_live_balance_cache(private_key)
                            except Exception as exc:
                                print(
                                    f"[pm_executor] post-redeem cache "
                                    f"refresh failed for pos "
                                    f"{position_id}: {exc}",
                                    file=sys.stderr,
                                )
                except Exception as exc:
                    # Never let a redeem-side problem mark settlement as
                    # failed - the DB is already correct, the on-chain
                    # tokens can be redeemed manually if the hook misfires.
                    print(
                        f"[pm_executor] redeem hook threw for pos "
                        f"{position_id}: {exc}",
                        file=sys.stderr,
                    )

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
                    "       entry_price, cost_usd, delfi_probability, "
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
                        "delfi_probability": float(r[8]) if r[8] is not None else None,
                        "ev_bps":            float(r[9]) if r[9] is not None else None,
                        "confidence":        float(r[10]) if r[10] is not None else None,
                        # iso_utc anchors the SQLite-returned datetime
                        # string with an explicit UTC offset so the JS
                        # Date parser doesn't fall back to local-time
                        # interpretation. See db.engine.iso_utc.
                        "expected_resolution_at": iso_utc(r[11]),
                        "created_at":             iso_utc(r[12]),
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
