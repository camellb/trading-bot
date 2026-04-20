"""
Polymarket executor — shadow mode today, live mode tomorrow.

Shadow mode:
    Simulates fills at the observed market mid-price. Writes a pm_positions
    row with mode='shadow'. No external calls, no wallet, no risk.

Live mode (stubbed until CLOB credentials are wired):
    Will submit a limit order via py-clob-client, wait for fill, then write
    a pm_positions row with mode='live', clob_order_id, and tx_hash. Until
    we wire credentials the live path raises explicitly.

All position bookkeeping and P&L flow through the same database, so the
dashboard, self-improvement loop, and Obsidian memory treat shadow and
live positions uniformly — only the `mode` column differs.

Bankroll model:
    bankroll = STARTING_CASH + Σ realized_pnl_usd(settled) - Σ cost_usd(open)
    Refreshed from DB before every sizing decision so concurrent fills don't
    over-stake.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

import calibration
import config
from db.engine import get_engine
from execution.pm_sizer import SizingDecision
from feeds.polymarket_feed import PolyMarket


# ── Public API ───────────────────────────────────────────────────────────────
class PMExecutor:
    """
    Handles the open/close lifecycle for Polymarket positions.

    The analyst calls `open_position` after sizing. The resolver (cron job)
    calls `settle_position` once the underlying market resolves.
    """

    def __init__(self, mode: Optional[str] = None):
        # Respect env first, then config — keeps "shadow" as the default
        # until live credentials are explicitly wired.
        self.mode = (mode
                     or os.environ.get("PM_MODE")
                     or getattr(config, "PM_MODE", "shadow")).lower()
        if self.mode not in ("shadow", "live"):
            raise ValueError(f"PM_MODE must be 'shadow' or 'live', got: {self.mode}")

    # ── Bankroll ─────────────────────────────────────────────────────────────
    def get_bankroll(self) -> float:
        """
        Current available bankroll in USD for the active mode.

            bankroll = starting_cash
                     + Σ realized_pnl_usd (settled positions)
                     - Σ cost_usd         (open positions)
        """
        starting = float(getattr(config, "PM_SHADOW_STARTING_CASH", 500.0))
        if self.mode == "live":
            starting = float(getattr(config, "PM_LIVE_STARTING_CASH", 200.0))
        try:
            with get_engine().begin() as conn:
                realized = conn.execute(text(
                    "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status IN ('settled', 'invalid')"
                ), {"m": self.mode}).scalar() or 0.0
                open_cost = conn.execute(text(
                    "SELECT COALESCE(SUM(cost_usd), 0) "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status = 'open'"
                ), {"m": self.mode}).scalar() or 0.0
            return float(starting) + float(realized) - float(open_cost)
        except Exception as exc:
            print(f"[pm_executor] get_bankroll failed: {exc}", file=sys.stderr)
            return float(starting)

    def get_portfolio_stats(self) -> dict:
        """
        Dashboard-friendly summary for the active mode.
        """
        starting = float(getattr(config, "PM_SHADOW_STARTING_CASH", 500.0))
        if self.mode == "live":
            starting = float(getattr(config, "PM_LIVE_STARTING_CASH", 200.0))
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE status = 'open') AS open_n, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled', 'invalid')) AS settled_n, "
                    "  COALESCE(SUM(cost_usd) FILTER (WHERE status = 'open'), 0) AS open_cost, "
                    "  COALESCE(SUM(realized_pnl_usd) FILTER (WHERE status IN ('settled', 'invalid')), 0) AS realized, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled', 'invalid') AND realized_pnl_usd > 0) AS wins "
                    "FROM pm_positions WHERE mode = :m"
                ), {"m": self.mode}).fetchone()
                open_n    = int(row[0] or 0)
                settled_n = int(row[1] or 0)
                open_cost = float(row[2] or 0)
                realized  = float(row[3] or 0)
                wins      = int(row[4] or 0)
        except Exception as exc:
            print(f"[pm_executor] get_portfolio_stats failed: {exc}", file=sys.stderr)
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
        research_quality: Optional[float] = None,
        model_disagreement: Optional[float] = None,
        n_models: Optional[int] = None,
    ) -> Optional[int]:
        """
        Record a new position. In shadow mode this is an immediate fill at
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
                                      market_archetype, research_quality,
                                      model_disagreement, n_models)
        else:
            pos_id = self._open_shadow(market, decision, claude_probability,
                                        prediction_id, reasoning, category,
                                        market_archetype, research_quality,
                                        model_disagreement, n_models)

        if pos_id and pos_id > 0:
            print(
                f"[pm_executor][{self.mode}] opened pm_position {pos_id}: "
                f"{market.question[:60]!r} {decision.side} "
                f"{decision.shares:.2f} shares @ {decision.entry_price:.3f} "
                f"(cost ${decision.stake_usd:.2f}, edge {decision.edge*10000:.0f}bps)",
                flush=True,
            )
        return pos_id

    def _open_shadow(self, market, decision, claude_p,
                     prediction_id, reasoning, category,
                     market_archetype=None, research_quality=None,
                     model_disagreement=None, n_models=None) -> Optional[int]:
        # Build execution metadata for audit trail.
        # Stored as a JSON appendix in the reasoning column so we don't need
        # a schema migration. Parseable downstream via the [exec_meta] marker.
        exec_meta = {
            "research_quality": research_quality,
            "model_disagreement": model_disagreement,
            "n_models": n_models,
            "eval_yes_price": getattr(market, "yes_price", None),
            "spread_estimate": float(getattr(config, "PM_SHADOW_SPREAD_ESTIMATE", 0.01)),
            "fee_rate": float(getattr(config, "PM_SHADOW_FEE_RATE", 0.002)),
        }
        # Truncate reasoning to leave room for metadata appendix
        reasoning_with_meta = (reasoning or "")[:3500]
        reasoning_with_meta += f"\n\n[exec_meta]{json.dumps(exec_meta)}"

        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "INSERT INTO pm_positions ("
                    "  prediction_id, market_id, condition_id, slug, question, category, "
                    "  side, shares, entry_price, cost_usd, "
                    "  claude_probability, edge_bps, confidence, "
                    "  mode, status, expected_resolution_at, reasoning, event_slug, "
                    "  market_archetype"
                    ") VALUES ("
                    "  :pid, :mid, :cid, :slug, :q, :cat, "
                    "  :side, :shares, :ep, :cost, "
                    "  :cp, :edge_bps, :conf, "
                    "  'shadow', 'open', :exp, :reason, :event_slug, "
                    "  :archetype"
                    ") RETURNING id"
                ), {
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
                    "edge_bps": decision.edge * 10_000.0,
                    "conf":  decision.confidence,
                    "exp":   market.end_date_iso,
                    "reason": reasoning_with_meta,
                    "event_slug": getattr(market, "event_slug", None),
                    "archetype": market_archetype,
                }).fetchone()
                return int(row[0]) if row else None
        except Exception as exc:
            print(f"[pm_executor] _open_shadow failed: {exc}", file=sys.stderr)
            return None

    def _open_live(self, market, decision, claude_p,
                   prediction_id, reasoning, category,
                   market_archetype=None, research_quality=None,
                   model_disagreement=None, n_models=None) -> Optional[int]:
        """
        Placeholder until Polymarket CLOB credentials are wired.
        """
        raise NotImplementedError(
            "Live execution requires Polymarket CLOB credentials. Set "
            "PM_MODE='shadow' in config.py, or provide POLYMARKET_API_KEY / "
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
                    "FROM pm_positions WHERE id = :pid AND status = 'open'"
                ), {"pid": position_id}).fetchone()
                if row is None:
                    print(f"[pm_executor] settle: position {position_id} not open",
                          file=sys.stderr)
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
            # For Polymarket rows we always score against the actual market
            # truth of the YES outcome, not whether our traded side won.
            if pred_id is not None and outcome != "INVALID":
                calibration.resolve_prediction_by_id(
                    prediction_id=pred_id,
                    outcome=1 if outcome == "YES" else 0,
                    pnl_usd=pnl,
                    note=f"pm_settlement outcome={outcome} side={side}",
                )

            print(
                f"[pm_executor][{self.mode}] settled pm_position {position_id}: "
                f"outcome={outcome}, side={side}, pnl=${pnl:+.2f}",
                flush=True,
            )
            return True
        except Exception as exc:
            print(f"[pm_executor] settle_position failed: {exc}", file=sys.stderr)
            return False

    # ── Open-position lookups ────────────────────────────────────────────────
    def get_open_positions(self) -> list[dict]:
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text(
                    "SELECT id, market_id, question, category, side, shares, "
                    "       entry_price, cost_usd, claude_probability, "
                    "       edge_bps, confidence, expected_resolution_at, "
                    "       created_at, prediction_id, reasoning, slug, "
                    "       event_slug "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status = 'open' "
                    "ORDER BY created_at DESC"
                ), {"m": self.mode}).fetchall()
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
                        "edge_bps":          float(r[9]) if r[9] is not None else None,
                        "confidence":        float(r[10]) if r[10] is not None else None,
                        "expected_resolution_at":
                            r[11].isoformat() if r[11] else None,
                        "created_at":        r[12].isoformat() if r[12] else None,
                        "prediction_id":     r[13],
                        "reasoning":         r[14],
                        "slug":              r[15],
                        "event_slug":        r[16],
                    }
                    for r in rows
                ]
        except Exception as exc:
            print(f"[pm_executor] get_open_positions failed: {exc}", file=sys.stderr)
            return []

    def has_open_position_on_market(self, market_id: str) -> bool:
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT 1 FROM pm_positions "
                    "WHERE mode = :m AND status = 'open' AND market_id = :mid "
                    "LIMIT 1"
                ), {"m": self.mode, "mid": str(market_id)}).fetchone()
                return row is not None
        except Exception as exc:
            print(f"[pm_executor] has_open_position failed: {exc}", file=sys.stderr)
            return True  # fail closed — assume position exists to prevent duplicates

    def open_position_count(self) -> int:
        try:
            with get_engine().begin() as conn:
                return int(conn.execute(text(
                    "SELECT COUNT(*) FROM pm_positions "
                    "WHERE mode = :m AND status = 'open'"
                ), {"m": self.mode}).scalar() or 0)
        except Exception as exc:
            print(f"[pm_executor] open_position_count failed: {exc}", file=sys.stderr)
            return 999  # fail closed — prevent opening new positions on DB error

    def count_positions_for_event(self, event_slug: str) -> int:
        """Count open positions belonging to the same event group."""
        try:
            with get_engine().begin() as conn:
                return int(conn.execute(text(
                    "SELECT COUNT(*) FROM pm_positions "
                    "WHERE mode = :m AND status = 'open' AND event_slug = :slug"
                ), {"m": self.mode, "slug": event_slug}).scalar() or 0)
        except Exception as exc:
            print(f"[pm_executor] count_positions_for_event failed: {exc}",
                  file=sys.stderr)
            return 0
