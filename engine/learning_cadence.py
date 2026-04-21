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
import json
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

# Stricter per-diagnostic gates (in trade-count) for proposers where the
# downside of acting on a noisy bucket is higher.
STRICT_BUCKET_N = 30          # archetype- and EV-bucket-level decisions.
COST_CORRECTION_MIN_N = 50    # cost assumption correction.
SELECTION_LOOSEN_MIN_N = 50   # selection-gate loosening.

# UserConfig fields that do NOT exist yet — proposals on these are advisory
# (surfaced to the user, backtest_delta=None) until Commit 4 adds the fields
# and teaches the sizer/backtester to honour them.
ADVISORY_PARAMS = {
    "cost_assumption_override",
    "probability_cap",
    "archetype_skip_list",
    "ev_bucket_skip_list",
}

# 0.25 is the Brier score of uninformed forecasts on binary outcomes; an
# archetype scoring worse than that is demonstrably mis-forecast.
ARCHETYPE_BRIER_THRESHOLD = 0.25

# Predicted-vs-actual gap in a high-confidence bin that signals overconfidence.
CALIBRATION_GAP_THRESHOLD = 0.10

# Realised-vs-assumed cost gap meaningful enough to act on.
COST_DELTA_THRESHOLD = 0.005


@dataclass
class Proposal:
    param_name:     str
    current_value:  Optional[float]
    proposed_value: Optional[float]
    evidence:       str
    backtest_delta: Optional[float] = None
    backtest_trades: Optional[int]   = None
    # In-memory only: carries the specific item (archetype, EV bucket, etc.)
    # and a suggested new tuple/value that `_attach_backtest_delta` can use
    # to construct the modified UserConfig for the simulation.
    proposal_metadata: Optional[dict] = None


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


def propose_suggestions(stats: dict, current: UserConfig,
                        diag: Optional[dict] = None) -> list[Proposal]:
    """
    Deterministic heuristic proposers.

    `stats`  — recent-window aggregate produced by `_gather_stats`.
    `diag`   — optional dict of diagnostic slices (see `_collect_diagnostics`).
               Tests may pass synthetic slices; in production it is fetched
               lazily if omitted.

    No single rule emits a suggestion unless its underlying bucket meets its
    own sample-size gate.
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

    # ── Diagnostic-driven proposers ────────────────────────────────────────
    # Lazy-load on first access so unit tests can stub a synthetic diag.
    if diag is None:
        diag = _collect_diagnostics()

    out.extend(_propose_archetype_threshold(diag, current))
    out.extend(_propose_calibration_shrinkage(diag, current))
    out.extend(_propose_cost_correction(diag, current))
    out.extend(_propose_selection_loosening(diag, current))
    out.extend(_propose_ev_bucket_exclude(diag, current))

    return out


def _collect_diagnostics() -> dict:
    """Pull the diagnostic slices needed by the new proposers. Isolated
    behind a helper so tests can pass a synthetic dict directly."""
    try:
        from engine import diagnostics as D
        return {
            "brier_by_archetype": D.brier_by_archetype("all"),
            "calibration_curve":  D.calibration_curve("all"),
            "cost_validation":    D.cost_validation(),
            "selection_quality":  D.selection_quality(),
            "roi_by_ev_bucket":   D.roi_by_ev_bucket(),
        }
    except Exception as exc:
        print(f"[learning_cadence] diag collection failed: {exc}",
              file=sys.stderr)
        return {}


# ── Diagnostic proposers ─────────────────────────────────────────────────────
def _propose_archetype_threshold(diag: dict,
                                 current: UserConfig) -> list[Proposal]:
    """
    Flag archetypes whose Brier score exceeds the uninformed baseline with
    a reliable sample. Proposal is advisory — skip that archetype until
    the forecaster improves — pending the Commit 4 `archetype_skip_list`
    sizer field.
    """
    out: list[Proposal] = []
    rows = diag.get("brier_by_archetype") or []
    for r in rows:
        n = int(r.get("n", 0) or 0)
        brier = r.get("brier")
        archetype = r.get("archetype")
        if n < STRICT_BUCKET_N or brier is None or not archetype:
            continue
        if brier <= ARCHETYPE_BRIER_THRESHOLD:
            continue
        out.append(Proposal(
            param_name="archetype_skip_list",
            current_value=None,
            proposed_value=None,
            evidence=(
                f"Archetype '{archetype}' Brier {brier:.3f} over {n} resolved "
                f"predictions is worse than the 0.25 uninformed baseline. "
                f"Proposal: add '{archetype}' to the archetype skip list so "
                f"the forecaster stops betting markets it demonstrably "
                f"mis-calibrates."
            ),
            proposal_metadata={
                "field": "archetype_skip_list",
                "add":   archetype,
            },
        ))
    return out


def _propose_calibration_shrinkage(diag: dict,
                                   current: UserConfig) -> list[Proposal]:
    """
    Detect systematic overconfidence at the top of the reliability diagram:
    when the mean predicted probability in a high-p bin (>= 0.7) exceeds the
    mean actual resolution rate by more than CALIBRATION_GAP_THRESHOLD.
    Proposal: cap predictions above the observed ceiling. Advisory —
    `probability_cap` sizer field lands in Commit 4.
    """
    out: list[Proposal] = []
    cc = diag.get("calibration_curve") or {}
    bins = cc.get("bins") or []
    # Find the worst offender in the high-p half.
    worst = None
    worst_gap = 0.0
    for b in bins:
        lo = float(b.get("lo", 0.0) or 0.0)
        n  = int(b.get("n", 0) or 0)
        mp = b.get("mean_pred")
        ma = b.get("mean_actual")
        if lo < 0.7 or n < STRICT_BUCKET_N or mp is None or ma is None:
            continue
        gap = float(mp) - float(ma)
        if gap > worst_gap:
            worst_gap = gap
            worst = b
    if worst is None or worst_gap < CALIBRATION_GAP_THRESHOLD:
        return out
    cap = round(max(0.5, min(0.95, float(worst["mean_actual"]) + 0.02)), 3)
    out.append(Proposal(
        param_name="probability_cap",
        current_value=None,
        proposed_value=cap,
        evidence=(
            f"Reliability bin [{worst['lo']:.1f}, {worst['hi']:.1f}]: mean "
            f"predicted {worst['mean_pred']:.3f} vs mean actual "
            f"{worst['mean_actual']:.3f} over n={worst['n']} — the forecaster "
            f"is overconfident by {worst_gap*100:.1f}pp. Proposal: cap "
            f"emitted probabilities at {cap:.2f} so sizing stops amplifying "
            f"the overshoot."
        ),
        proposal_metadata={
            "field": "probability_cap",
            "value": cap,
        },
    ))
    return out


def _propose_cost_correction(diag: dict,
                             current: UserConfig) -> list[Proposal]:
    """
    If the realised implied cost exceeds the sizer's assumed cost by more
    than COST_DELTA_THRESHOLD over a meaningful sample, propose raising the
    assumed cost so EV math reflects reality. Advisory —
    `cost_assumption_override` sizer field lands in Commit 4.
    """
    out: list[Proposal] = []
    cv = diag.get("cost_validation") or {}
    n = int(cv.get("n", 0) or 0)
    implied = cv.get("implied_cost")
    assumed = cv.get("assumed_cost")
    if n < COST_CORRECTION_MIN_N or implied is None or assumed is None:
        return out
    delta = float(implied) - float(assumed)
    if delta < COST_DELTA_THRESHOLD:
        return out
    proposed = round(float(implied), 4)
    out.append(Proposal(
        param_name="cost_assumption_override",
        current_value=float(assumed),
        proposed_value=proposed,
        evidence=(
            f"Realised implied cost {implied*100:.2f}% exceeds assumed cost "
            f"{assumed*100:.2f}% by {delta*100:.2f}pp across n={n} settled "
            f"positions. EV math currently over-estimates by that amount. "
            f"Proposal: set cost_assumption_override={proposed}."
        ),
        proposal_metadata={
            "field": "cost_assumption_override",
            "value": proposed,
        },
    ))
    return out


def _propose_selection_loosening(diag: dict,
                                 current: UserConfig) -> list[Proposal]:
    """
    If the $10-flat-stake counterfactual ROI on *skipped* predictions is
    higher than the realised ROI on *traded* positions, the selection gate
    is rejecting too much. Propose lowering `min_ev_threshold`. Actionable —
    target field already exists on UserConfig.
    """
    out: list[Proposal] = []
    sq = diag.get("selection_quality") or {}
    traded = sq.get("traded") or {}
    skipped = sq.get("skipped_counterfactual") or {}

    t_n = int(traded.get("n", 0) or 0)
    s_n = int(skipped.get("n", 0) or 0)
    t_roi = traded.get("roi")
    s_roi = skipped.get("roi")

    if (t_n < SELECTION_LOOSEN_MIN_N or s_n < SELECTION_LOOSEN_MIN_N
            or t_roi is None or s_roi is None):
        return out
    if float(s_roi) <= float(t_roi):
        return out

    lo, hi = USER_CONFIG_BOUNDS["min_ev_threshold"]
    proposed = max(round(current.min_ev_threshold * 0.8, 4), lo)
    if proposed >= current.min_ev_threshold - 0.005:
        return out
    out.append(Proposal(
        param_name="min_ev_threshold",
        current_value=current.min_ev_threshold,
        proposed_value=proposed,
        evidence=(
            f"Skipped counterfactual ROI {float(s_roi)*100:+.1f}% (n={s_n}) "
            f"beats traded ROI {float(t_roi)*100:+.1f}% (n={t_n}). The "
            f"selection gate is discarding winners. Proposal: lower "
            f"min_ev_threshold from {current.min_ev_threshold*100:.1f}% to "
            f"{proposed*100:.1f}% so more +EV opportunities pass through."
        ),
    ))
    return out


def _propose_ev_bucket_exclude(diag: dict,
                               current: UserConfig) -> list[Proposal]:
    """
    For each EV bucket with persistent negative ROI over a reliable sample,
    propose adding it to the skip list. Advisory — `ev_bucket_skip_list`
    sizer field lands in Commit 4.
    """
    out: list[Proposal] = []
    rows = diag.get("roi_by_ev_bucket") or []
    for r in rows:
        n = int(r.get("n", 0) or 0)
        roi = r.get("roi")
        bucket = r.get("bucket")
        if n < MIN_BUCKET_N or roi is None or not bucket:
            continue
        if float(roi) >= 0.0:
            continue
        out.append(Proposal(
            param_name="ev_bucket_skip_list",
            current_value=None,
            proposed_value=None,
            evidence=(
                f"EV bucket '{bucket}' has ROI {float(roi)*100:+.1f}% over "
                f"n={n} settled positions — persistently negative. Proposal: "
                f"add '{bucket}' to ev_bucket_skip_list."
            ),
            proposal_metadata={
                "field": "ev_bucket_skip_list",
                "add":   bucket,
            },
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
    """Populate backtest_delta on a proposal using the EV backtester.

    Scalar proposals (min_ev_threshold, max_stake_pct, cost_assumption_override,
    probability_cap) are applied via dataclass replace. List proposals
    (archetype_skip_list, ev_bucket_skip_list) read proposal_metadata['add']
    and append to the existing tuple."""
    try:
        modified = _build_modified_config(prop, current)
        if modified is None:
            return

        from backtester.ev_backtester import load_evaluations, simulate_with_config
        evals = load_evaluations(since_days=90)
        if not evals:
            return

        baseline = simulate_with_config(evals, current)
        candidate = simulate_with_config(evals, modified)

        base_roi = baseline.get("roi") or 0.0
        cand_roi = candidate.get("roi") or 0.0
        prop.backtest_delta = float(cand_roi - base_roi)
        prop.backtest_trades = int(candidate.get("trades_resolved") or 0)
    except Exception as exc:
        print(f"[learning_cadence] backtest delta failed for "
              f"{prop.param_name}: {exc}", file=sys.stderr)


def _build_modified_config(prop: Proposal,
                           current: UserConfig) -> Optional[UserConfig]:
    """
    Derive the simulated UserConfig for this proposal. Returns None when the
    proposal lacks enough information to simulate.
    """
    name = prop.param_name
    meta = prop.proposal_metadata or {}

    # List-append proposals (skip lists): tuple-union the existing list with
    # the new item from metadata.
    if name in ("archetype_skip_list", "ev_bucket_skip_list"):
        item = meta.get("add")
        if not item:
            return None
        existing = tuple(getattr(current, name, ()) or ())
        if item in existing:
            return None
        return dataclasses.replace(current, **{name: existing + (str(item),)})

    # Scalar proposals: prefer proposed_value; fall back to metadata.
    value = prop.proposed_value
    if value is None:
        value = meta.get("value")
    if value is None:
        return None
    return dataclasses.replace(current, **{name: value})


def _store_pending_suggestion(prop: Proposal, user_id: str,
                              settled_count: int) -> bool:
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        meta_json = json.dumps(prop.proposal_metadata) \
            if prop.proposal_metadata else None
        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO pending_suggestions "
                "(user_id, param_name, current_value, proposed_value, "
                " evidence, backtest_delta, backtest_trades, "
                " settled_count_at_creation, metadata) "
                "VALUES (:uid, :k, :cur, :prop, :ev, :bd, :bt, :sc, "
                "        CAST(:meta AS JSONB))"
            ), {
                "uid":  user_id,
                "k":    prop.param_name,
                "cur":  prop.current_value,
                "prop": prop.proposed_value,
                "ev":   prop.evidence[:4000],
                "bd":   prop.backtest_delta,
                "bt":   prop.backtest_trades,
                "sc":   settled_count,
                "meta": meta_json,
            })
        return True
    except Exception as exc:
        print(f"[learning_cadence] store_pending failed: {exc}", file=sys.stderr)
        return False


def _decode_metadata(raw) -> Optional[dict]:
    """Coerce a JSONB/JSON/TEXT column value into a dict or None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else None
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


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
                "       backtest_trades, status, settled_count_at_creation, "
                "       metadata "
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
            "metadata":       _decode_metadata(r[10]),
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
