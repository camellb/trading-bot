"""
Trade-volume-based learning cadence.

Every 50 settled trades the bot runs a full analysis pass, proposes
user_config tweaks with backtester evidence, and stores them in the
pending_suggestions table for the user to Apply, Skip, or Snooze on the
dashboard.

Calendar-based tuning (weekly / monthly) proposes changes on whatever
sample happens to have accumulated in that window, which is too often
too small and noisy. Trade-volume gating guarantees every suggestion is
backed by a meaningful sample. An active bot might hit 50 trades in days;
a quiet one might take weeks — either way, suggestions arrive when the
data justifies them.

Proposers are deterministic heuristics for now. Each proposer follows the
same rule: never emit a suggestion for a bucket with n < 20.
"""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass
from typing import Optional

from engine.user_config import (
    DEFAULT_USER_ID,
    USER_CONFIG_BOUNDS,
    UserConfig,
    get_user_config,
    update_user_config,
)

# Minimum new settled trades required before a learning cycle runs.
LEARNING_CYCLE_TRADE_INTERVAL = 50

# Minimum sample size per bucket before a proposer may emit a suggestion.
MIN_BUCKET_N = 20


@dataclass
class Proposal:
    param_name:     str
    current_value:  float
    proposed_value: float
    evidence:       str
    backtest_delta: Optional[float] = None
    backtest_trades: Optional[int]   = None


# ── Public entry points ──────────────────────────────────────────────────────
def maybe_run_learning_cycle(user_id: str = DEFAULT_USER_ID,
                             mode: str = "shadow") -> dict:
    """
    Called after every settlement. Runs the pipeline iff the trade-volume
    gate has been crossed since the last cycle. Returns a status dict.
    """
    try:
        settled_now = _count_settled_trades(mode)
        last_cycle = _last_cycle_settled_count(user_id)
        delta = settled_now - last_cycle
        if delta < LEARNING_CYCLE_TRADE_INTERVAL:
            return {
                "status":      "gate_not_crossed",
                "settled_now": settled_now,
                "since_last":  delta,
                "threshold":   LEARNING_CYCLE_TRADE_INTERVAL,
            }

        stats = _gather_stats(mode, limit=LEARNING_CYCLE_TRADE_INTERVAL)
        current_cfg = get_user_config(user_id)
        proposals = propose_suggestions(stats, current_cfg)

        # Enrich each with the backtester delta.
        for prop in proposals:
            _attach_backtest_delta(prop, current_cfg)

        stored = 0
        for prop in proposals:
            if _store_pending_suggestion(prop, user_id=user_id,
                                          settled_count=settled_now):
                stored += 1

        return {
            "status":       "ran",
            "settled_now":  settled_now,
            "since_last":   delta,
            "proposals":    len(proposals),
            "stored":       stored,
        }
    except Exception as exc:
        print(f"[learning_cadence] maybe_run failed: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc)}


def propose_suggestions(stats: dict, current: UserConfig) -> list[Proposal]:
    """
    Deterministic heuristic proposers. No single rule emits a suggestion
    unless its underlying bucket has n ≥ MIN_BUCKET_N.
    """
    out: list[Proposal] = []

    recent = stats.get("recent_window") or {}
    n = int(recent.get("n", 0))
    if n < MIN_BUCKET_N:
        return out

    roi = float(recent.get("roi", 0.0))
    win_rate = float(recent.get("win_rate", 0.0))

    # Sustained losing streak → raise threshold (fewer, higher-conviction bets).
    if roi < -0.05 and win_rate < 0.45:
        lo, hi = USER_CONFIG_BOUNDS["min_ev_threshold"]
        proposed = min(round(current.min_ev_threshold * 1.3, 4), hi)
        if proposed > current.min_ev_threshold + 0.005:
            out.append(Proposal(
                param_name="min_ev_threshold",
                current_value=current.min_ev_threshold,
                proposed_value=proposed,
                evidence=(
                    f"Last {n} settled trades: ROI {roi*100:+.1f}%, win rate "
                    f"{win_rate*100:.1f}%. Raising the EV threshold selects for "
                    f"higher-conviction bets and should reduce noise trades."
                ),
            ))

    # Sustained winning pattern → lower threshold (capture more +EV).
    if roi > 0.08 and win_rate > 0.55:
        lo, hi = USER_CONFIG_BOUNDS["min_ev_threshold"]
        proposed = max(round(current.min_ev_threshold * 0.8, 4), lo)
        if proposed < current.min_ev_threshold - 0.005:
            out.append(Proposal(
                param_name="min_ev_threshold",
                current_value=current.min_ev_threshold,
                proposed_value=proposed,
                evidence=(
                    f"Last {n} settled trades: ROI {roi*100:+.1f}%, win rate "
                    f"{win_rate*100:.1f}%. Lowering the EV threshold captures "
                    f"more positive-EV trades the book is handling well."
                ),
            ))

    # Drawdown pressure → cut max stake.
    peak_dd = float(recent.get("peak_drawdown_pct", 0.0))
    if peak_dd >= 0.25 and current.max_stake_pct > 0.02:
        lo, hi = USER_CONFIG_BOUNDS["max_stake_pct"]
        proposed = max(round(current.max_stake_pct * 0.7, 4), lo)
        if proposed < current.max_stake_pct - 0.005:
            out.append(Proposal(
                param_name="max_stake_pct",
                current_value=current.max_stake_pct,
                proposed_value=proposed,
                evidence=(
                    f"Peak drawdown over last {n} trades: {peak_dd*100:.1f}%. "
                    f"Reducing max_stake_pct from {current.max_stake_pct*100:.1f}% "
                    f"to {proposed*100:.1f}% limits single-trade risk."
                ),
            ))

    return out


# ── DB helpers ───────────────────────────────────────────────────────────────
def _count_settled_trades(mode: str) -> int:
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            return int(conn.execute(text(
                "SELECT COUNT(*) FROM pm_positions "
                "WHERE mode = :m AND status IN ('settled', 'invalid')"
            ), {"m": mode}).scalar() or 0)
    except Exception as exc:
        print(f"[learning_cadence] count_settled failed: {exc}", file=sys.stderr)
        return 0


def _last_cycle_settled_count(user_id: str) -> int:
    """The settled_count we stamped on the most recent suggestion."""
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            val = conn.execute(text(
                "SELECT MAX(settled_count_at_creation) "
                "FROM pending_suggestions "
                "WHERE user_id = :uid"
            ), {"uid": user_id}).scalar()
            return int(val or 0)
    except Exception as exc:
        print(f"[learning_cadence] last_cycle_count failed: {exc}", file=sys.stderr)
        return 0


def _gather_stats(mode: str, limit: int) -> dict:
    """Pull last `limit` settled trades and summarise for the proposers."""
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT cost_usd, realized_pnl_usd, category "
                "FROM pm_positions "
                "WHERE mode = :m AND status IN ('settled', 'invalid') "
                "ORDER BY settled_at DESC "
                "LIMIT :lim"
            ), {"m": mode, "lim": limit}).fetchall()
    except Exception as exc:
        print(f"[learning_cadence] gather_stats failed: {exc}", file=sys.stderr)
        return {"recent_window": {"n": 0}}

    costs  = [float(r[0] or 0.0) for r in rows]
    pnls   = [float(r[1] or 0.0) for r in rows]
    cats   = [r[2] for r in rows]

    n = len(rows)
    total_cost = sum(costs) or 1.0
    total_pnl  = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    roi = total_pnl / total_cost
    win_rate = wins / n if n else 0.0

    # Peak drawdown over the window — running max vs current equity.
    equity = 0.0
    peak = 0.0
    peak_dd = 0.0
    # Iterate in chronological order.
    for p in reversed(pnls):
        equity += p
        if equity > peak:
            peak = equity
        dd = (peak - equity) / (peak + 1e-9) if peak > 0 else 0.0
        if dd > peak_dd:
            peak_dd = dd

    by_category: dict[str, dict] = {}
    for cat, pnl, cost in zip(cats, pnls, costs):
        key = cat or "other"
        b = by_category.setdefault(key, {"n": 0, "pnl": 0.0, "cost": 0.0, "wins": 0})
        b["n"]    += 1
        b["pnl"]  += pnl
        b["cost"] += cost
        b["wins"] += 1 if pnl > 0 else 0

    for cat, b in by_category.items():
        b["roi"] = b["pnl"] / b["cost"] if b["cost"] else 0.0
        b["win_rate"] = b["wins"] / b["n"] if b["n"] else 0.0

    return {
        "recent_window": {
            "n":                  n,
            "roi":                roi,
            "win_rate":           win_rate,
            "total_pnl":          total_pnl,
            "total_cost":         total_cost,
            "peak_drawdown_pct":  peak_dd,
            "by_category":        by_category,
        },
    }


def _attach_backtest_delta(prop: Proposal, current: UserConfig) -> None:
    """Populate backtest_delta on a proposal using the EV backtester."""
    try:
        from backtester.ev_backtester import load_evaluations, simulate_with_config
        evals = load_evaluations(since_days=90)
        if not evals:
            return

        modified = dataclasses.replace(current, **{prop.param_name: prop.proposed_value})
        baseline = simulate_with_config(evals, current)
        candidate = simulate_with_config(evals, modified)

        base_roi = baseline.get("roi") or 0.0
        cand_roi = candidate.get("roi") or 0.0
        prop.backtest_delta = float(cand_roi - base_roi)
        prop.backtest_trades = int(candidate.get("trades_resolved") or 0)
    except Exception as exc:
        print(f"[learning_cadence] backtest delta failed for "
              f"{prop.param_name}: {exc}", file=sys.stderr)


def _store_pending_suggestion(prop: Proposal, user_id: str,
                              settled_count: int) -> bool:
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO pending_suggestions "
                "(user_id, param_name, current_value, proposed_value, "
                " evidence, backtest_delta, backtest_trades, "
                " settled_count_at_creation) "
                "VALUES (:uid, :k, :cur, :prop, :ev, :bd, :bt, :sc)"
            ), {
                "uid":  user_id,
                "k":    prop.param_name,
                "cur":  prop.current_value,
                "prop": prop.proposed_value,
                "ev":   prop.evidence[:4000],
                "bd":   prop.backtest_delta,
                "bt":   prop.backtest_trades,
                "sc":   settled_count,
            })
        return True
    except Exception as exc:
        print(f"[learning_cadence] store_pending failed: {exc}", file=sys.stderr)
        return False


# ── Suggestion lifecycle (called from the dashboard API) ─────────────────────
def list_pending_suggestions(user_id: str = DEFAULT_USER_ID,
                             include_snoozed: bool = True) -> list[dict]:
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        statuses = ("pending", "snoozed") if include_snoozed else ("pending",)
        placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
        params = {"uid": user_id}
        for i, s in enumerate(statuses):
            params[f"s{i}"] = s
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT id, created_at, param_name, current_value, "
                "       proposed_value, evidence, backtest_delta, "
                "       backtest_trades, status, settled_count_at_creation "
                "FROM pending_suggestions "
                f"WHERE user_id = :uid AND status IN ({placeholders}) "
                "ORDER BY created_at DESC"
            ), params).fetchall()
    except Exception as exc:
        print(f"[learning_cadence] list_pending failed: {exc}", file=sys.stderr)
        return []

    out = []
    for r in rows:
        out.append({
            "id":             int(r[0]),
            "created_at":     r[1].isoformat() if r[1] else None,
            "param_name":     r[2],
            "current_value":  float(r[3]) if r[3] is not None else None,
            "proposed_value": float(r[4]) if r[4] is not None else None,
            "evidence":       r[5],
            "backtest_delta": float(r[6]) if r[6] is not None else None,
            "backtest_trades": int(r[7]) if r[7] is not None else None,
            "status":         r[8],
            "settled_count":  int(r[9]) if r[9] is not None else None,
        })
    return out


def apply_suggestion(suggestion_id: int,
                     user_id: str = DEFAULT_USER_ID,
                     resolved_by: str = "user") -> dict:
    """
    Apply a pending suggestion: update user_config, mark the row applied.
    Validation of bounds happens inside update_user_config.
    """
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        row = conn.execute(text(
            "SELECT param_name, proposed_value, status "
            "FROM pending_suggestions WHERE id = :id AND user_id = :uid"
        ), {"id": suggestion_id, "uid": user_id}).fetchone()
        if row is None:
            return {"status": "not_found"}
        if row[2] not in ("pending", "snoozed"):
            return {"status": "already_resolved", "current_status": row[2]}

        param_name = str(row[0])
        proposed = float(row[1])

    # This validates against bounds and raises ValueError on any problem.
    update_user_config(user_id, **{param_name: proposed})

    with get_engine().begin() as conn:
        conn.execute(text(
            "UPDATE pending_suggestions SET status = 'applied', "
            "resolved_at = NOW(), resolved_by = :rb "
            "WHERE id = :id AND user_id = :uid"
        ), {"id": suggestion_id, "uid": user_id, "rb": resolved_by})

    return {"status": "applied",
            "param_name": param_name,
            "value": proposed}


def skip_suggestion(suggestion_id: int,
                    user_id: str = DEFAULT_USER_ID,
                    resolved_by: str = "user") -> dict:
    return _update_status(suggestion_id, user_id, "skipped", resolved_by)


def snooze_suggestion(suggestion_id: int,
                      user_id: str = DEFAULT_USER_ID,
                      resolved_by: str = "user") -> dict:
    return _update_status(suggestion_id, user_id, "snoozed", resolved_by)


def _update_status(suggestion_id: int, user_id: str,
                   status: str, resolved_by: str) -> dict:
    from sqlalchemy import text
    from db.engine import get_engine
    try:
        with get_engine().begin() as conn:
            result = conn.execute(text(
                "UPDATE pending_suggestions SET status = :st, "
                "resolved_at = NOW(), resolved_by = :rb "
                "WHERE id = :id AND user_id = :uid "
                "  AND status IN ('pending', 'snoozed')"
            ), {"id": suggestion_id, "uid": user_id, "st": status, "rb": resolved_by})
            if result.rowcount == 0:
                return {"status": "not_found_or_resolved"}
        return {"status": status, "id": suggestion_id}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
