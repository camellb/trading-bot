"""
Diagnostic slices that feed the V1.5 learning proposers.

Each function returns a single dict / list of dicts that one proposer
in `learning_cadence.py` reads as its source of truth. Kept in a
separate module from `engine/diagnostics.py` because that file is
forecaster-quality + sizer attribution, and conflating it with these
risk-knob slices made the module sprawling and slow to compile.

Convention:
  - Every function is `user_id`-scoped (per-user data; multi-tenant
    safe even though local-first only has user_id='local').
  - Every function is `mode`-scoped to "live" or "simulation" so the
    proposers never mix data across modes (CLAUDE.md "WE CAN'T MIX
    ANY SIMULATION AND LIVE DATA TOGETHER" rule).
  - All numeric outputs are plain floats / ints. Bootstrap CIs come
    back as (lo, hi) tuples in the same units as the underlying mean.
  - No caching here. The proposers run on the cycle cadence (every
    25-50 settled trades), so paying the cost on the rare run is
    cheaper than maintaining cache invalidation.
"""

from __future__ import annotations

import random
import sys
from typing import Optional

from sqlalchemy import text

from db.engine import get_engine


# ── Constants ────────────────────────────────────────────────────────────────

# Bootstrap parameters. 1000 resamples is the standard cheap default;
# 95% CI is the standard reporting band.
_BOOTSTRAP_N = 1000
_CI_LO_PCT = 2.5
_CI_HI_PCT = 97.5

# Threshold sweep grid for exit-threshold backtests. Take-profit
# thresholds are tested as "fraction of cost basis" (entry_price *
# (1 + threshold)). Stop-loss thresholds as the same negative.
_TP_THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50,
                  0.75, 1.00, 1.50, 2.00, 3.00]
_SL_THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50,
                  0.60, 0.70, 0.80]

# Horizon buckets in days-to-end. Matches the days-based timeframe
# knob on Risk page (`min_days_to_resolution` / `max_days_to_resolution`)
# so the horizon proposer can reason about user-actionable buckets
# rather than the hour-based brier_by_horizon buckets.
_HORIZON_BUCKETS_DAYS: list[tuple[str, float, Optional[float]]] = [
    ("< 1d",  0.0,  1.0),
    ("1-3d",  1.0,  3.0),
    ("3-7d",  3.0,  7.0),
    ("7-14d", 7.0,  14.0),
    ("14d+",  14.0, None),
]


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _bootstrap_mean_ci(values: list[float],
                       n_resamples: int = _BOOTSTRAP_N,
                       seed: int = 42,
                       ) -> tuple[float, float, float]:
    """Return (mean, ci_lo, ci_hi) on the sample's mean.

    Deterministic via fixed seed so the same cycle produces the same
    proposal twice in a row - prevents the dashboard's "applied" badge
    from flickering when the same suggestion comes back with a slightly
    different CI on the next cycle.
    """
    if not values:
        return 0.0, 0.0, 0.0
    if len(values) == 1:
        v = float(values[0])
        return v, v, v
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(n_resamples):
        s = 0.0
        for _ in range(n):
            s += values[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    mean = sum(values) / n
    return (mean,
            _percentile(means, _CI_LO_PCT),
            _percentile(means, _CI_HI_PCT))


# ── Exit policy attribution ─────────────────────────────────────────────────

def exit_policy_attribution(
    user_id: str = "local",
    mode: str = "live",
) -> dict:
    """Per-close-reason aggregates that drive the exit-policy toggle
    proposer.

    For each early exit (take_profit / stop_loss / time_decay), we
    have:
      - realized_pnl_usd       (what the early exit ACTUALLY made)
      - counterfactual_pnl_usd (what holding to settlement WOULD HAVE
                                made; backfilled by polymarket_runner
                                Phase C once the market settles)

    "Saved vs hold" = realized - counterfactual. POSITIVE means the
    early exit beat holding (the policy worked); NEGATIVE means we
    left money on the table.

    Output:
      {
        "by_reason": [
          {
            "reason":              "take_profit",
            "n":                   8,           # backfilled rows
            "n_pending":           2,           # exited but market still open
            "mean_realized":       1.23,
            "mean_counterfactual": 2.85,
            "mean_saved_vs_hold": -1.62,
            "ci_lo":              -2.81,
            "ci_hi":              -0.43,
            "policy_is_helpful":  False,        # CI excludes 0 and is positive
            "policy_is_harmful":  True,         # CI excludes 0 and is negative
          },
          ...
        ],
        "total_early_exits":  20,
        "total_backfilled":   18,
      }
    """
    reasons = ("take_profit", "stop_loss", "time_decay")
    out_by_reason: list[dict] = []
    total_exits = 0
    total_backfilled = 0
    try:
        with get_engine().begin() as conn:
            # Total early exits (any close_reason) for the badge.
            row_total = conn.execute(text(
                "SELECT COUNT(*) FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND status = 'closed_early'"
            ), {"uid": user_id, "m": mode}).fetchone()
            total_exits = int((row_total[0] if row_total else 0) or 0)

            for r in reasons:
                rows = conn.execute(text(
                    "SELECT realized_pnl_usd, counterfactual_pnl_usd "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = :m "
                    "  AND status = 'closed_early' "
                    "  AND close_reason = :reason"
                ), {"uid": user_id, "m": mode, "reason": r}).fetchall()
                realized_vals: list[float] = []
                counterfactual_vals: list[float] = []
                saved_vals: list[float] = []
                n_pending = 0
                for rp, cf in rows:
                    if rp is None:
                        continue
                    realized_vals.append(float(rp))
                    if cf is None:
                        n_pending += 1
                        continue
                    counterfactual_vals.append(float(cf))
                    saved_vals.append(float(rp) - float(cf))
                n = len(saved_vals)
                total_backfilled += n
                mean_saved, ci_lo, ci_hi = _bootstrap_mean_ci(saved_vals)
                mean_realized = (sum(realized_vals) / len(realized_vals)
                                 if realized_vals else 0.0)
                mean_cf = (sum(counterfactual_vals) / len(counterfactual_vals)
                           if counterfactual_vals else 0.0)
                policy_is_helpful = (n >= 8 and ci_lo > 0.0)
                policy_is_harmful = (n >= 8 and ci_hi < 0.0)
                out_by_reason.append({
                    "reason":              r,
                    "n":                   n,
                    "n_pending":           n_pending,
                    "mean_realized":       round(mean_realized, 4),
                    "mean_counterfactual": round(mean_cf, 4),
                    "mean_saved_vs_hold":  round(mean_saved, 4),
                    "ci_lo":               round(ci_lo, 4),
                    "ci_hi":               round(ci_hi, 4),
                    "policy_is_helpful":   policy_is_helpful,
                    "policy_is_harmful":   policy_is_harmful,
                })
    except Exception as exc:
        print(f"[learning_diagnostics] exit_policy_attribution failed: {exc}",
              file=sys.stderr)
    return {
        "by_reason":         out_by_reason,
        "total_early_exits": total_exits,
        "total_backfilled":  total_backfilled,
    }


# ── Exit threshold backtest ─────────────────────────────────────────────────

def exit_threshold_backtest(
    user_id: str = "local",
    mode: str = "live",
) -> dict:
    """Threshold sweep on the price-path data recorded by the 60s
    exit-policy job (`max_price_seen` / `min_price_seen`).

    For every settled position (no early exit required) we know:
      - entry_price (price paid for bought side, fraction 0..1)
      - max_price_seen (highest mid the position reached)
      - min_price_seen (lowest mid the position reached)
      - cost_usd
      - shares
      - realized_pnl_usd

    For a candidate take-profit threshold `tp_pct`, we simulate:
      - target_price = entry_price * (1 + tp_pct), clipped to <= 0.99.
      - If max_price_seen >= target_price, the position WOULD have
        exited at target_price. Counterfactual realized = shares *
        target_price - cost_usd.
      - Otherwise the position would have run to natural settlement
        and earned its actual realized_pnl_usd.

    Same logic in reverse for stop-loss thresholds (entry * (1 -
    sl_pct), clipped to >= 0.01).

    Output:
      {
        "take_profit": [
          {"threshold_pct": 0.20, "n_would_trigger": 6,
           "mean_pnl_per_position": 0.42, "total_pnl": 5.04, ...},
          ...
        ],
        "stop_loss": [...],
        "baseline_total_pnl": 12.50,
        "n_positions": 30,
      }

    Each row's `mean_pnl_per_position` is directly comparable: the
    threshold whose mean beats the baseline by a CI-significant
    margin is what the proposer recommends.
    """
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT entry_price, side, max_price_seen, min_price_seen, "
                "       cost_usd, shares, realized_pnl_usd "
                "FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND status IN ('settled','invalid','closed_early') "
                "  AND entry_price IS NOT NULL "
                "  AND cost_usd IS NOT NULL "
                "  AND shares IS NOT NULL "
                "  AND realized_pnl_usd IS NOT NULL"
            ), {"uid": user_id, "m": mode}).fetchall()
    except Exception as exc:
        print(f"[learning_diagnostics] exit_threshold_backtest failed: {exc}",
              file=sys.stderr)
        return {
            "take_profit": [], "stop_loss": [],
            "baseline_total_pnl": 0.0, "n_positions": 0,
        }

    # Project each row into bought-side price space. For YES bets the
    # bought-side price equals the YES side mid; for NO bets we flip.
    # max_price_seen / min_price_seen are already on the bought side
    # (see polymarket_runner.evaluate_open_positions).
    samples: list[dict] = []
    for entry, side, max_p, min_p, cost, shares, realized in rows:
        if entry is None or cost is None or shares is None or realized is None:
            continue
        samples.append({
            "entry":    float(entry),
            "side":     side or "YES",
            "max_p":    float(max_p) if max_p is not None else None,
            "min_p":    float(min_p) if min_p is not None else None,
            "cost":     float(cost),
            "shares":   float(shares),
            "realized": float(realized),
        })
    n_pos = len(samples)
    baseline_total = sum(s["realized"] for s in samples)

    def _simulate(threshold: float, kind: str) -> dict:
        """Returns aggregate stats if every position were closed at
        the candidate threshold when reachable."""
        n_trigger = 0
        per_position_pnls: list[float] = []
        for s in samples:
            entry = s["entry"]
            max_p = s["max_p"]
            min_p = s["min_p"]
            shares = s["shares"]
            cost = s["cost"]
            if kind == "tp":
                target = min(0.99, entry * (1.0 + threshold))
                reached = (max_p is not None and max_p >= target
                           and target > entry)
            else:  # sl
                target = max(0.01, entry * (1.0 - threshold))
                reached = (min_p is not None and min_p <= target
                           and target < entry)
            if reached:
                cf_pnl = shares * target - cost
                per_position_pnls.append(cf_pnl)
                n_trigger += 1
            else:
                per_position_pnls.append(s["realized"])
        mean, ci_lo, ci_hi = _bootstrap_mean_ci(per_position_pnls)
        return {
            "threshold_pct":           round(threshold, 4),
            "n_would_trigger":         n_trigger,
            "mean_pnl_per_position":   round(mean, 4),
            "total_pnl":               round(sum(per_position_pnls), 2),
            "ci_lo":                   round(ci_lo, 4),
            "ci_hi":                   round(ci_hi, 4),
        }

    tp_results = [_simulate(t, "tp") for t in _TP_THRESHOLDS]
    sl_results = [_simulate(t, "sl") for t in _SL_THRESHOLDS]

    return {
        "take_profit":         tp_results,
        "stop_loss":           sl_results,
        "baseline_total_pnl":  round(baseline_total, 2),
        "n_positions":         n_pos,
    }


# ── Horizon ROI attribution ─────────────────────────────────────────────────

def horizon_pnl_attribution(
    user_id: str = "local",
    mode: str = "live",
) -> list[dict]:
    """ROI bucketed by days-to-end at entry. Reads horizon_hours from
    the predictions row linked via pm_positions.prediction_id, so
    only positions with a prediction trail show up.

    Output (one row per bucket in `_HORIZON_BUCKETS_DAYS`):
      {"bucket": "1-3d", "n": 12, "pnl": 4.20, "cost": 24.00,
       "roi": 0.175, "win_rate": 0.58, "ci_lo": 0.04, "ci_hi": 0.31,
       "usable": True}
    """
    out: list[dict] = []
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT pr.horizon_hours, pp.cost_usd, pp.realized_pnl_usd "
                "FROM pm_positions pp "
                "JOIN predictions pr ON pr.id = pp.prediction_id "
                "WHERE pp.user_id = :uid AND pp.mode = :m "
                "  AND pp.status IN ('settled','invalid','closed_early') "
                "  AND pp.cost_usd IS NOT NULL "
                "  AND pp.realized_pnl_usd IS NOT NULL "
                "  AND pr.horizon_hours IS NOT NULL"
            ), {"uid": user_id, "m": mode}).fetchall()
    except Exception as exc:
        print(f"[learning_diagnostics] horizon_pnl_attribution failed: {exc}",
              file=sys.stderr)
        return out

    for label, lo_hours, hi_hours in _HORIZON_BUCKETS_DAYS:
        bucket_rows = []
        for hh, cost, pnl in rows:
            h_days = float(hh or 0.0) / 24.0
            if h_days < lo_hours:
                continue
            if hi_hours is not None and h_days >= hi_hours:
                continue
            bucket_rows.append((float(cost or 0.0), float(pnl or 0.0)))
        n = len(bucket_rows)
        cost_sum = sum(c for c, _ in bucket_rows)
        pnl_sum = sum(p for _, p in bucket_rows)
        wins = sum(1 for _, p in bucket_rows if p > 0)
        roi = (pnl_sum / cost_sum) if cost_sum > 0 else None
        # Per-position ROI for CI.
        per_pos_roi = [
            (p / c) for c, p in bucket_rows if c > 0
        ]
        _, ci_lo, ci_hi = _bootstrap_mean_ci(per_pos_roi)
        out.append({
            "bucket":   label,
            "n":        n,
            "pnl":      round(pnl_sum, 2),
            "cost":     round(cost_sum, 2),
            "roi":      round(roi, 4) if roi is not None else None,
            "win_rate": round(wins / n, 4) if n else None,
            "ci_lo":    round(ci_lo, 4),
            "ci_hi":    round(ci_hi, 4),
            "usable":   n >= 8,
        })
    return out


# ── Loss-day recovery analysis ──────────────────────────────────────────────

def loss_day_recovery(
    user_id: str = "local",
    mode: str = "live",
) -> dict:
    """Group settled-trade P&L by calendar day. For each day where
    the daily P&L was net-negative, look at the NEXT trading day and
    track whether the bot recovered or continued losing.

    Output:
      {
        "n_loss_days":         12,
        "n_recovered":         7,    # net positive on the next day
        "n_continued":         5,    # net negative or zero on the next day
        "recovery_rate":       0.583,
        "mean_loss_day_pnl":  -3.20,
        "mean_next_day_pnl":   1.40,
      }

    Powers the daily_loss_limit_pct proposer: high recovery rate
    suggests the limit can be loosened; low rate suggests tightening
    so the bot stops trading after a loss streak begins.
    """
    out = {
        "n_loss_days":      0,
        "n_recovered":      0,
        "n_continued":      0,
        "recovery_rate":    None,
        "mean_loss_day_pnl": 0.0,
        "mean_next_day_pnl": 0.0,
    }
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT date(settled_at) AS d, "
                "       COALESCE(SUM(realized_pnl_usd), 0) AS pnl "
                "FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND settled_at IS NOT NULL "
                "  AND realized_pnl_usd IS NOT NULL "
                "  AND status IN ('settled','invalid','closed_early') "
                "GROUP BY d ORDER BY d ASC"
            ), {"uid": user_id, "m": mode}).fetchall()
    except Exception as exc:
        print(f"[learning_diagnostics] loss_day_recovery failed: {exc}",
              file=sys.stderr)
        return out

    daily = [(str(r[0]), float(r[1] or 0.0)) for r in rows]
    loss_pnls: list[float] = []
    next_pnls: list[float] = []
    for i, (_d, pnl) in enumerate(daily):
        if pnl >= 0:
            continue
        loss_pnls.append(pnl)
        if i + 1 < len(daily):
            next_pnl = daily[i + 1][1]
            next_pnls.append(next_pnl)
            if next_pnl > 0:
                out["n_recovered"] += 1
            else:
                out["n_continued"] += 1
    out["n_loss_days"] = len(loss_pnls)
    if loss_pnls:
        out["mean_loss_day_pnl"] = round(sum(loss_pnls) / len(loss_pnls), 4)
    if next_pnls:
        out["mean_next_day_pnl"] = round(sum(next_pnls) / len(next_pnls), 4)
        considered = out["n_recovered"] + out["n_continued"]
        if considered > 0:
            out["recovery_rate"] = round(out["n_recovered"] / considered, 4)
    return out


# ── Loss-WEEK recovery analysis ─────────────────────────────────────────────

def loss_week_recovery(
    user_id: str = "local",
    mode: str = "live",
) -> dict:
    """Same shape as `loss_day_recovery` but bucketed by ISO week.

    Drives the weekly_loss_limit_pct proposer: high recovery_rate
    after a losing week suggests the limit can be loosened; low
    rate suggests the bot should halt sooner so a bad week doesn't
    cascade.

    SQLite's `strftime('%Y-%W', ...)` returns "YYYY-WW" buckets
    (week numbers 00-53). Two settled positions on the same ISO
    week roll up to one bucket.
    """
    out = {
        "n_loss_weeks":      0,
        "n_recovered":       0,
        "n_continued":       0,
        "recovery_rate":     None,
        "mean_loss_week_pnl": 0.0,
        "mean_next_week_pnl": 0.0,
    }
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT strftime('%Y-%W', settled_at) AS w, "
                "       COALESCE(SUM(realized_pnl_usd), 0) AS pnl "
                "FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND settled_at IS NOT NULL "
                "  AND realized_pnl_usd IS NOT NULL "
                "  AND status IN ('settled','invalid','closed_early') "
                "GROUP BY w ORDER BY w ASC"
            ), {"uid": user_id, "m": mode}).fetchall()
    except Exception as exc:
        print(f"[learning_diagnostics] loss_week_recovery failed: {exc}",
              file=sys.stderr)
        return out

    weekly = [(str(r[0]), float(r[1] or 0.0)) for r in rows]
    loss_pnls: list[float] = []
    next_pnls: list[float] = []
    for i, (_w, pnl) in enumerate(weekly):
        if pnl >= 0:
            continue
        loss_pnls.append(pnl)
        if i + 1 < len(weekly):
            next_pnl = weekly[i + 1][1]
            next_pnls.append(next_pnl)
            if next_pnl > 0:
                out["n_recovered"] += 1
            else:
                out["n_continued"] += 1
    out["n_loss_weeks"] = len(loss_pnls)
    if loss_pnls:
        out["mean_loss_week_pnl"] = round(sum(loss_pnls) / len(loss_pnls), 4)
    if next_pnls:
        out["mean_next_week_pnl"] = round(sum(next_pnls) / len(next_pnls), 4)
        considered = out["n_recovered"] + out["n_continued"]
        if considered > 0:
            out["recovery_rate"] = round(out["n_recovered"] / considered, 4)
    return out


# ── Loss-streak analysis ────────────────────────────────────────────────────

def loss_streak_analysis(
    user_id: str = "local",
    mode: str = "live",
) -> dict:
    """For each chronologically-observed loss streak of length N >= 2,
    record the realized P&L of the trade IMMEDIATELY AFTER the streak
    broke (the next settled position).

    Drives the streak_cooldown_losses proposer: if trade-after-
    streak P&L is consistently worse than the baseline (bot is in a
    bad regime), the cooldown should kick in sooner; if better
    (regimes mean-revert quickly), the cooldown can loosen.

    Output:
      {
        "n_streaks": 8,           # total streaks of length >= 2
        "by_length": {            # how many trades-after at each streak length
          "2": {"n": 5, "mean_next_pnl":  0.40},
          "3": {"n": 2, "mean_next_pnl": -1.20},
          "4+": {"n": 1, "mean_next_pnl": -2.10},
        },
        "baseline_mean_pnl": 0.18, # avg P&L over ALL settled positions
        "worst_streak_len":  4,
      }
    """
    out = {
        "n_streaks":         0,
        "by_length":         {},
        "baseline_mean_pnl": 0.0,
        "worst_streak_len":  0,
    }
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT realized_pnl_usd "
                "FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND status IN ('settled','invalid','closed_early') "
                "  AND realized_pnl_usd IS NOT NULL "
                "  AND settled_at IS NOT NULL "
                "ORDER BY settled_at ASC"
            ), {"uid": user_id, "m": mode}).fetchall()
    except Exception as exc:
        print(f"[learning_diagnostics] loss_streak_analysis failed: {exc}",
              file=sys.stderr)
        return out

    pnls = [float(r[0] or 0.0) for r in rows]
    if not pnls:
        return out
    out["baseline_mean_pnl"] = round(sum(pnls) / len(pnls), 4)

    # Walk the series. Whenever a loss streak (>=2 consecutive
    # negatives) ENDS, capture the next trade's P&L.
    streak = 0
    longest = 0
    by_len: dict[str, list[float]] = {}
    for i, p in enumerate(pnls):
        if p < 0:
            streak += 1
            if streak > longest:
                longest = streak
        else:
            if streak >= 2:
                # The current trade `p` IS the trade-after-streak.
                key = f"{streak}" if streak < 4 else "4+"
                by_len.setdefault(key, []).append(p)
                out["n_streaks"] += 1
            streak = 0
    out["worst_streak_len"] = longest
    out["by_length"] = {
        k: {
            "n":             len(v),
            "mean_next_pnl": round(sum(v) / len(v), 4),
        }
        for k, v in by_len.items()
    }
    return out


# ── Archetype × price-band ROI ──────────────────────────────────────────────

def archetype_price_band_pnl(
    user_id: str = "local",
    mode: str = "live",
) -> list[dict]:
    """Slice settled positions by (archetype, 10pp band of entry
    market price). Drives the per-archetype price-band proposer:
    bands inside an archetype whose ROI CI is statistically negative
    get proposed as additions to that archetype's price-band skip
    list.

    Output: one row per (archetype, band) cell:
      {"archetype": "tennis", "band_lo": 0.50, "band_hi": 0.60,
       "n": 6, "pnl": -3.20, "cost": 12.00, "roi": -0.267,
       "ci_lo": -0.51, "ci_hi": -0.02, "usable": False}
    """
    out: list[dict] = []
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT market_archetype, side, entry_price, "
                "       cost_usd, realized_pnl_usd "
                "FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND status IN ('settled','invalid','closed_early') "
                "  AND market_archetype IS NOT NULL "
                "  AND entry_price IS NOT NULL "
                "  AND cost_usd IS NOT NULL "
                "  AND realized_pnl_usd IS NOT NULL"
            ), {"uid": user_id, "m": mode}).fetchall()
    except Exception as exc:
        print(f"[learning_diagnostics] archetype_price_band_pnl failed: {exc}",
              file=sys.stderr)
        return out

    # Bucket rows into (archetype, band) cells. Band is 10pp slice
    # of the MARKET YES probability at entry (entry_price for YES
    # bets; 1 - entry_price for NO bets).
    cells: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for arch, side, entry, cost, pnl in rows:
        if entry is None or cost is None or pnl is None:
            continue
        market_yes = float(entry) if (side or "YES") == "YES" else (1.0 - float(entry))
        band_idx = max(0, min(9, int(market_yes * 10)))
        cells.setdefault((arch, band_idx), []).append(
            (float(cost), float(pnl)),
        )
    for (arch, band_idx), rs in sorted(cells.items()):
        n = len(rs)
        cost_sum = sum(c for c, _ in rs)
        pnl_sum = sum(p for _, p in rs)
        roi = (pnl_sum / cost_sum) if cost_sum > 0 else None
        per_pos = [(p / c) for c, p in rs if c > 0]
        _, ci_lo, ci_hi = _bootstrap_mean_ci(per_pos)
        out.append({
            "archetype": arch,
            "band_lo":   round(band_idx / 10.0, 2),
            "band_hi":   round((band_idx + 1) / 10.0, 2),
            "n":         n,
            "pnl":       round(pnl_sum, 2),
            "cost":      round(cost_sum, 2),
            "roi":       round(roi, 4) if roi is not None else None,
            "ci_lo":     round(ci_lo, 4),
            "ci_hi":     round(ci_hi, 4),
            "usable":    n >= 8,
        })
    return out


# ── Aggregate ROI + drawdown ────────────────────────────────────────────────

def aggregate_roi_and_drawdown(
    user_id: str = "local",
    mode: str = "live",
) -> dict:
    """Top-line numbers the base-stake-pct and drawdown-halt proposers
    need: total ROI, current drawdown vs peak equity, and the
    realized peak drawdown over the last N settlements.

    Output:
      {
        "n_settled":         42,
        "total_pnl":         12.40,
        "total_cost":        165.00,
        "roi":               0.075,
        "current_drawdown":  0.08,   # 1 - equity/peak_equity over realized series
        "peak_drawdown":     0.21,
      }
    """
    out = {
        "n_settled":        0,
        "total_pnl":        0.0,
        "total_cost":       0.0,
        "roi":              None,
        "current_drawdown": 0.0,
        "peak_drawdown":    0.0,
    }
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT realized_pnl_usd, cost_usd, settled_at "
                "FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND status IN ('settled','invalid','closed_early') "
                "  AND realized_pnl_usd IS NOT NULL "
                "  AND cost_usd IS NOT NULL "
                "ORDER BY settled_at ASC"
            ), {"uid": user_id, "m": mode}).fetchall()
    except Exception as exc:
        print(f"[learning_diagnostics] aggregate_roi_and_drawdown failed: {exc}",
              file=sys.stderr)
        return out

    pnls = [float(r[0] or 0.0) for r in rows]
    costs = [float(r[1] or 0.0) for r in rows]
    n = len(pnls)
    total_pnl = sum(pnls)
    total_cost = sum(costs)
    out["n_settled"] = n
    out["total_pnl"] = round(total_pnl, 2)
    out["total_cost"] = round(total_cost, 2)
    if total_cost > 0:
        out["roi"] = round(total_pnl / total_cost, 4)

    # Drawdown: walk the cumulative realized P&L curve, track peak,
    # measure max drop from peak.
    cum = 0.0
    peak = 0.0
    peak_dd = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / max(peak, 1e-9)
            if dd > peak_dd:
                peak_dd = dd
    out["peak_drawdown"] = round(peak_dd, 4)
    if peak > 0:
        out["current_drawdown"] = round(
            max(0.0, (peak - cum) / max(peak, 1e-9)), 4,
        )
    return out
