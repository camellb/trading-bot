"""
Polymarket executor — per-user (SaaS multi-tenancy).

Every executor instance is bound to a specific user_id and reads that
user's mode (simulation|live) and starting_cash from user_config. A user
with no mode or no starting_cash is `not ready` — the executor exposes
zero state and refuses all writes for that user. Brand-new accounts see
nothing until they complete onboarding.

Simulation mode:
    Simulates fills at the observed market mid-price. Writes a pm_positions
    row with mode='simulation' and user_id=<user>. No external calls.

Live mode (stubbed until CLOB credentials are wired):
    Will submit a limit order via py-clob-client using that user's
    Polymarket creds from user_config, then write a pm_positions row
    with mode='live', clob_order_id, tx_hash, and user_id=<user>. Until
    the CLOB client is wired the live path raises explicitly.

Bankroll model (per-user):
    bankroll = user_config.starting_cash
             + Σ realized_pnl_usd (WHERE user_id AND status IN settled/invalid)
             - Σ cost_usd         (WHERE user_id AND status = 'open')
    Refreshed from DB before every sizing decision so concurrent fills
    don't over-stake.
"""

from __future__ import annotations

import sys
from typing import Optional

from sqlalchemy import text

import calibration
from db.engine import get_engine
from execution.pm_sizer import SizingDecision
from feeds.polymarket_feed import PolyMarket
from engine.user_config import UserConfig, get_user_config


# ── Public API ───────────────────────────────────────────────────────────────
class PMExecutor:
    """
    Handles the open/close lifecycle for Polymarket positions, scoped to a
    single user. Construct one instance per user per scan/request.
    """

    def __init__(self, user_id: str, *, user_config: Optional[UserConfig] = None):
        if not user_id or not isinstance(user_id, str):
            raise ValueError(f"PMExecutor requires a user_id, got: {user_id!r}")
        self.user_id = user_id
        # Snapshot the user's config at construction time. Callers that need
        # fresh values after a dashboard edit should rebuild the executor.
        self._user_config: UserConfig = user_config or get_user_config(user_id)

    # ── Readiness ────────────────────────────────────────────────────────────
    @property
    def ready(self) -> bool:
        """True iff the bot may act for this user right now."""
        return self._user_config.ready_to_trade

    @property
    def mode(self) -> Optional[str]:
        """The user's configured mode, or None if not yet set."""
        return self._user_config.mode

    # ── Bankroll ─────────────────────────────────────────────────────────────
    def get_starting_cash(self) -> float:
        """
        This user's starting bankroll in USD. Returns 0.0 if the user hasn't
        finished onboarding — callers treat that as "no bankroll, don't trade".
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
        """
        starting = self.get_starting_cash()
        if not self.ready:
            return 0.0
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
            print(f"[pm_executor] get_bankroll({self.user_id}) failed: {exc}",
                  file=sys.stderr)
            return float(starting)

    def get_portfolio_stats(self) -> dict:
        """
        Dashboard-friendly summary for this user in their current mode.

        Not-ready users (no onboarding) see all zeros — never data that
        leaked from another tenant.
        """
        if not self.ready:
            return {
                "mode":            self._user_config.mode,     # may be None
                "starting_cash":   0.0,
                "bankroll":        0.0,
                "equity":          0.0,
                "open_positions":  0,
                "open_cost":       0.0,
                "settled_total":   0,
                "settled_wins":    0,
                "win_rate":        None,
                "realized_pnl":    0.0,
                "ready":           False,
            }
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

        if self.mode == "live":
            pos_id = self._open_live(market, decision, claude_probability,
                                      prediction_id, reasoning, category,
                                      market_archetype)
        else:
            pos_id = self._open_simulation(market, decision, claude_probability,
                                        prediction_id, reasoning, category,
                                        market_archetype)

        if pos_id and pos_id > 0:
            print(
                f"[pm_executor][{self.mode}] opened pm_position {pos_id}: "
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
                    "  market_archetype"
                    ") VALUES ("
                    "  :user_id, :pid, :mid, :cid, :slug, :q, :cat, "
                    "  :side, :shares, :ep, :cost, "
                    "  :cp, :ev_bps, :conf, "
                    "  :mode, 'open', :exp, :reason, :event_slug, "
                    "  :arch"
                    ") RETURNING id"
                ), {
                    "user_id": self.user_id,
                    "mode":  self.mode,
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
                    "exp":   market.end_date_iso,
                    "reason": (reasoning or "")[:4000] or None,
                    "event_slug": getattr(market, "event_slug", None),
                    "arch":  market_archetype,
                }).fetchone()
                return int(row[0]) if row else None
        except Exception as exc:
            print(f"[pm_executor] _open_simulation failed: {exc}", file=sys.stderr)
            return None

    def _open_live(self, market, decision, claude_p,
                   prediction_id, reasoning, category,
                   market_archetype=None) -> Optional[int]:
        """
        Placeholder until Polymarket CLOB credentials are wired.
        """
        raise NotImplementedError(
            "Live execution requires Polymarket CLOB credentials. Set "
            "PM_MODE='simulation' in config.py, or provide POLYMARKET_API_KEY / "
            "PROXY_ADDRESS / PRIVATE_KEY and implement _open_live via "
            "py-clob-client."
        )

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
                    "  settled_at          = NOW(), "
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
                f"[pm_executor][{self.mode}] settled pm_position {position_id}: "
                f"outcome={outcome}, side={side}, pnl=${pnl:+.2f}",
                flush=True,
            )

            # Trade-volume learning cadence — cheap no-op until the
            # 50-settled-trade gate is crossed for this user.
            try:
                from engine.learning_cadence import maybe_run_learning_cycle
                maybe_run_learning_cycle(user_id=self.user_id, mode=self.mode)
            except Exception as exc:
                print(f"[pm_executor] learning_cadence hook failed: {exc}",
                      file=sys.stderr)
            return True
        except Exception as exc:
            print(f"[pm_executor] settle_position failed: {exc}", file=sys.stderr)
            return False

    # ── Open-position lookups ────────────────────────────────────────────────
    def get_open_positions(self) -> list[dict]:
        if not self.ready:
            return []
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
            return True  # fail closed — assume position exists to prevent duplicates

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
            return 999  # fail closed — prevent opening new positions on DB error

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
