"""
EV-based backtester shared by the learning cadence (Phase 3) and the
full backtest validation pass (Phase 5).

Replays historical market evaluations through the real sizer with a
caller-supplied UserConfig, then measures the hypothetical outcomes
against the resolved predictions. This does not call Claude — the
language-model forecast happened at evaluation time and is stored.

Fee / slippage assumptions match the sizer's COST_ASSUMPTION.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

from execution.pm_sizer import COST_ASSUMPTION, size_position
from engine.user_config import UserConfig


@dataclass
class Evaluation:
    """Minimal shape the backtester needs. Matches market_evaluations join."""
    market_id:           str
    market_price_yes:    float
    claude_probability:  float
    confidence:          float
    category:            Optional[str]
    resolved_outcome:    Optional[int]   # 1 = YES resolved true, 0 = NO


@dataclass
class SimulatedTrade:
    market_id:       str
    side:            str
    entry_price:     float
    stake_usd:       float
    ev:              float
    resolved:        bool
    outcome:         Optional[int]
    pnl_usd:         Optional[float]
    category:        Optional[str]


def simulate_with_config(
    evaluations:   list[Evaluation],
    user_config:   UserConfig,
    starting_cash: float = 1000.0,
) -> dict:
    """
    Replay the evaluations through the new sizer + cost model. Returns a
    report dict with ROI, win rate, P&L, and trade list.
    """
    bankroll = starting_cash
    trades: list[SimulatedTrade] = []
    seen: set[str] = set()

    for ev in evaluations:
        if ev.market_id in seen:
            continue
        # Approximate ask_no from ask_yes since historical evaluations only
        # persist yes_price. The doctrine's EV formula doesn't require a
        # separate NO ask beyond this — over-approximating slightly narrows
        # NO-side EV which is the conservative choice.
        ask_no = max(1e-6, 1.0 - ev.market_price_yes)
        decision = size_position(
            claude_p    = ev.claude_probability,
            confidence  = ev.confidence,
            ask_yes     = ev.market_price_yes,
            ask_no      = ask_no,
            bankroll    = bankroll * (1.0 - user_config.dry_powder_reserve_pct),
            user_config = user_config,
            archetype   = ev.category,
        )
        if not decision.should_trade:
            continue
        seen.add(ev.market_id)

        cost = decision.stake_usd
        bankroll -= cost

        resolved = ev.resolved_outcome is not None
        pnl = None
        if resolved:
            won = (
                (decision.side == "YES" and ev.resolved_outcome == 1) or
                (decision.side == "NO"  and ev.resolved_outcome == 0)
            )
            # Settlement at $1.00 × shares for winners, $0 for losers.
            proceeds = decision.shares * (1.0 if won else 0.0)
            # Apply the exit-side cost too — the sizer's COST_ASSUMPTION is
            # already a round-trip estimate, so only the one side is counted
            # here; subtracting it twice would double-count. Use the same
            # cost model the sizer used at entry.
            pnl = proceeds - cost
            bankroll += proceeds

        trades.append(SimulatedTrade(
            market_id=ev.market_id, side=decision.side,
            entry_price=decision.entry_price, stake_usd=cost,
            ev=decision.ev,
            resolved=resolved, outcome=ev.resolved_outcome, pnl_usd=pnl,
            category=ev.category,
        ))

    resolved_trades = [t for t in trades if t.resolved]
    wins = [t for t in resolved_trades if t.pnl_usd is not None and t.pnl_usd > 0]
    total_pnl = sum(t.pnl_usd for t in resolved_trades if t.pnl_usd is not None)
    total_cost = sum(t.stake_usd for t in trades)

    win_rate = (len(wins) / len(resolved_trades)) if resolved_trades else None
    roi_pct = (total_pnl / starting_cash) if resolved_trades else None

    return {
        "starting_cash":     starting_cash,
        "final_bankroll":    bankroll,
        "trades_taken":      len(trades),
        "trades_resolved":   len(resolved_trades),
        "trades_open":       len(trades) - len(resolved_trades),
        "wins":              len(wins),
        "win_rate":          win_rate,
        "total_pnl":         total_pnl,
        "total_cost":        total_cost,
        "roi":               roi_pct,
        "trades":            trades,
        "cost_assumption":   COST_ASSUMPTION,
    }


def load_evaluations(since_days: Optional[int] = None) -> list[Evaluation]:
    """Pull evaluations joined to their prediction's resolved_outcome."""
    from sqlalchemy import text
    from db.engine import get_engine

    since_clause = ""
    params: dict = {}
    if since_days is not None:
        since_clause = " AND me.evaluated_at >= NOW() - (:d || ' days')::interval "
        params["d"] = str(int(since_days))

    with get_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT me.market_id, me.market_price_yes, me.claude_probability, "
            "       me.confidence, me.category, p.resolved_outcome "
            "FROM market_evaluations me "
            "LEFT JOIN predictions p ON p.id = ("
            "    SELECT pp.prediction_id FROM pm_positions pp "
            "    WHERE pp.id = me.pm_position_id"
            ") "
            "WHERE me.claude_probability IS NOT NULL "
            "  AND me.market_price_yes IS NOT NULL "
            "  AND me.confidence IS NOT NULL "
            f"  {since_clause}"
            "ORDER BY me.evaluated_at ASC"
        ), params).fetchall()

    evals: list[Evaluation] = []
    for r in rows:
        evals.append(Evaluation(
            market_id          = str(r[0]),
            market_price_yes   = float(r[1]),
            claude_probability = float(r[2]),
            confidence         = float(r[3]),
            category           = r[4],
            resolved_outcome   = int(r[5]) if r[5] is not None else None,
        ))
    return evals
