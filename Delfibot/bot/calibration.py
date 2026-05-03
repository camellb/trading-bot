"""
Calibration instrument - the foundation for knowing whether the
forecaster is well-calibrated, per category and overall.

Design:
  * Every actionable prediction (a crypto TRADE, a Polymarket bet, a backtest
    simulated trade) gets a row in the `predictions` table when it's made.
  * When the outcome is known (crypto trade closed, prediction market
    resolved, backtest simulated through horizon), the same row is
    resolved with outcome (1 = correct, 0 = incorrect) and realized P&L.
  * From that history we compute Brier score + reliability diagrams per
    source / per category / per bucket.  That tells us whether the system's
    stated probabilities match reality - the single most important question.

No trading decisions flow through this module.  Its job is purely to watch
and score.  If the logger breaks the bot's trading path must still work -
every public function swallows exceptions and returns best-effort values.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from db.engine import get_engine

# Bucket edges for reliability diagrams - five equal-width bins in [0,1].
RELIABILITY_BINS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]

# Time-horizon buckets for Brier breakdowns - (label, lower_hours, upper_hours).
# upper=None means no upper bound.
HORIZON_BUCKETS = [
    ("< 1d",   0,    24),
    ("1-7d",   24,   168),
    ("7-30d",  168,  720),
    ("30d+",   720,  None),  # None = no upper bound
]


# ── Write path ───────────────────────────────────────────────────────────────
def log_prediction(
    source:        str,
    subject_key:   str,
    probability:   float,
    category:      Optional[str]  = None,
    confidence:    Optional[float] = None,
    horizon_hours: Optional[float] = None,
    reasoning:     Optional[str]  = None,
    metadata:      Optional[dict] = None,
    trade_id:      Optional[int]  = None,
) -> int:
    """
    Record a prediction at the moment it's made.  Returns the row id, or -1
    on error - never raises; trading paths must not be broken by logging.

    `probability` is the (claimed) calibrated probability the prediction
    is correct, in [0,1].  For a LONG crypto trade, "correct" = "closes
    with positive P&L".  For a Polymarket bet, "correct" = "market resolves
    YES if the bet was YES".  The scoring primitive is the same everywhere.
    """
    try:
        p = float(probability)
        # Clamp defensively - a sloppy source shouldn't corrupt the dataset.
        p = max(0.0, min(1.0, p))
        meta_json = json.dumps(metadata) if metadata is not None else None
        from engine.user_config import DEFAULT_USER_ID
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "INSERT INTO predictions "
                "(user_id, source, subject_key, category, probability, confidence, "
                " horizon_hours, reasoning, metadata, trade_id) "
                "VALUES "
                "(:user_id, :source, :subject_key, :category, :probability, :confidence, "
                " :horizon_hours, :reasoning, :metadata, :trade_id) "
                "RETURNING id"
            ), {
                "user_id":        DEFAULT_USER_ID,
                "source":         source,
                "subject_key":    subject_key,
                "category":       category,
                "probability":    p,
                "confidence":     confidence,
                "horizon_hours":  horizon_hours,
                "reasoning":      (reasoning or "")[:8000] or None,
                "metadata":       meta_json,
                "trade_id":       trade_id,
            }).fetchone()
            return int(row[0]) if row else -1
    except Exception as exc:
        print(f"[calibration] log_prediction failed: {exc}", file=sys.stderr)
        return -1


def resolve_prediction_by_trade(
    trade_id:  int,
    outcome:   int,
    pnl_usd:   Optional[float] = None,
    note:      Optional[str]   = None,
) -> bool:
    """
    Resolve every unresolved prediction tied to `trade_id`.  Called from the
    close path. `outcome` = 1 if the prediction was correct (e.g. trade
    profitable) else 0.  Never raises.
    """
    try:
        if outcome not in (0, 1):
            outcome = 1 if outcome else 0
        with get_engine().begin() as conn:
            conn.execute(text(
                "UPDATE predictions SET "
                "  resolved_at      = CURRENT_TIMESTAMP, "
                "  resolved_outcome = :outcome, "
                "  resolved_pnl_usd = :pnl, "
                "  resolved_note    = :note "
                "WHERE trade_id = :trade_id AND resolved_at IS NULL"
            ), {
                "outcome":  int(outcome),
                "pnl":      pnl_usd,
                "note":     note,
                "trade_id": trade_id,
            })
        return True
    except Exception as exc:
        print(f"[calibration] resolve_prediction_by_trade failed: {exc}",
              file=sys.stderr)
        return False


def resolve_prediction_by_id(
    prediction_id: int,
    outcome:       int,
    pnl_usd:       Optional[float] = None,
    note:          Optional[str]   = None,
) -> bool:
    """Direct-id resolution - used by prediction-market + backtest paths."""
    try:
        if outcome not in (0, 1):
            outcome = 1 if outcome else 0
        with get_engine().begin() as conn:
            conn.execute(text(
                "UPDATE predictions SET "
                "  resolved_at      = CURRENT_TIMESTAMP, "
                "  resolved_outcome = :outcome, "
                "  resolved_pnl_usd = :pnl, "
                "  resolved_note    = :note "
                "WHERE id = :id AND resolved_at IS NULL"
            ), {"outcome": int(outcome), "pnl": pnl_usd, "note": note, "id": prediction_id})
        return True
    except Exception as exc:
        print(f"[calibration] resolve_prediction_by_id failed: {exc}",
              file=sys.stderr)
        return False


# ── Read path: scoring + reliability ─────────────────────────────────────────
def _empty_report(source: Optional[str], since_days: Optional[int]) -> dict:
    return {
        "source":       source or "all",
        "since_days":   since_days,
        "total":        0,
        "resolved":     0,
        "unresolved":   0,
        "brier":        None,
        "mean_prob":    None,
        "mean_outcome": None,
        "realized_pnl_usd": None,
        "bins":         [{"lo": lo, "hi": hi, "n": 0,
                          "mean_pred": None, "mean_actual": None}
                         for lo, hi in RELIABILITY_BINS],
        "by_category":  [],
        "by_archetype": [],
        "by_horizon":   [{"bucket": label, "n": 0, "brier": None,
                          "mean_pred": None, "mean_actual": None}
                         for label, _, _ in HORIZON_BUCKETS],
    }


def get_report(
    source: Optional[str] = None,
    since_days: Optional[int] = None,
    user_id: Optional[str] = None,
) -> dict:
    """
    Return the calibration card for a single user.

    Source of truth is `pm_positions` (filtered by user_id, entered-only) so
    every metric reflects the trades that user actually took. Skipped
    evaluations and other users' positions are never counted.

    Shape:
      {
        "source":       "polymarket" | "all",
        "since_days":   <int|null>,
        "total":        <entered positions for this user>,
        "resolved":     <settled positions for this user>,
        "unresolved":   <open positions for this user>,
        "brier":        <float|null>,   # on chosen side
        "mean_prob":    <float|null>,   # avg claude_probability at entry
        "mean_outcome": <float|null>,   # empirical win rate on entered
        "realized_pnl_usd": <float|null>,
        "bins":         [ {lo, hi, n, mean_pred, mean_actual}, ... ],
        "by_category":  [ {category, n, brier, mean_pred, mean_actual}, ... ],
        "by_horizon":   [ {bucket, n, brier, mean_pred, mean_actual}, ... ],
      }

    All keys always present. No user_id → empty report (SaaS rule: users only
    see their own performance; admin dashboards have separate endpoints).
    """
    if not user_id:
        return _empty_report(source, since_days)
    # The only prediction source that resolves through pm_positions today.
    # Other sources (e.g. crypto trades) simply return empty until they're
    # migrated onto a user-scoped resolution path.
    if source and source not in ("polymarket", "all"):
        return _empty_report(source, since_days)

    try:
        # outcome = 1 when the winning outcome matches the side we took.
        # INVALID resolutions (rare, Polymarket-side) fall through as 0 here.
        outcome_expr = (
            "CASE WHEN settlement_outcome = side THEN 1.0 ELSE 0.0 END"
        )
        base_filters = ["user_id = :uid"]
        params: dict = {"uid": user_id}
        if since_days and since_days > 0:
            # Anchor on settled_at for resolved aggregates; keep created_at
            # for totals so the bucket reflects "entered in the last N days".
            pass

        res_filters = list(base_filters) + [
            "settled_at IS NOT NULL",
            "claude_probability IS NOT NULL",
            "settlement_outcome IN ('YES', 'NO')",
        ]
        if since_days and since_days > 0:
            res_filters.append(
                f"settled_at >= datetime('now', '-{int(since_days)} days')"
            )
        res_where = "WHERE " + " AND ".join(res_filters)

        total_filters = list(base_filters) + [
            "claude_probability IS NOT NULL",
        ]
        if since_days and since_days > 0:
            total_filters.append(
                f"created_at >= datetime('now', '-{int(since_days)} days')"
            )
        total_where = "WHERE " + " AND ".join(total_filters)

        with get_engine().begin() as conn:
            totals = conn.execute(text(
                f"SELECT "
                f"  COUNT(*) AS total, "
                f"  COUNT(settled_at) AS resolved "
                f"FROM pm_positions {total_where}"
            ), params).fetchone()
            total    = int(totals[0] or 0)
            resolved = int(totals[1] or 0)

            agg = conn.execute(text(
                f"SELECT "
                f"  AVG(claude_probability)                        AS mean_prob, "
                f"  AVG({outcome_expr})                            AS mean_outcome, "
                f"  AVG(POWER(claude_probability - ({outcome_expr}), 2)) AS brier, "
                f"  SUM(realized_pnl_usd)                          AS pnl "
                f"FROM pm_positions {res_where}"
            ), params).fetchone()
            mean_prob    = float(agg[0]) if agg[0] is not None else None
            mean_outcome = float(agg[1]) if agg[1] is not None else None
            brier        = float(agg[2]) if agg[2] is not None else None
            realized_pnl = float(agg[3]) if agg[3] is not None else None

            bins: list[dict] = []
            for lo, hi in RELIABILITY_BINS:
                cmp_hi = "<=" if hi == 1.0 else "<"
                bin_sql = (
                    f"SELECT COUNT(*) AS n, "
                    f"       AVG(claude_probability) AS mp, "
                    f"       AVG({outcome_expr}) AS ma "
                    f"FROM pm_positions {res_where} "
                    f"  AND claude_probability >= :lo "
                    f"  AND claude_probability {cmp_hi} :hi"
                )
                row = conn.execute(text(bin_sql),
                                    {**params, "lo": lo, "hi": hi}).fetchone()
                bins.append({
                    "lo": lo,
                    "hi": hi,
                    "n":  int(row[0] or 0),
                    "mean_pred":   float(row[1]) if row[1] is not None else None,
                    "mean_actual": float(row[2]) if row[2] is not None else None,
                })

            # Per-category breakdown.
            # Returns: n, brier, mean_pred, mean_actual, pnl_usd, cost_usd, wins.
            # Frontend can derive ROI = pnl_usd/cost_usd and win_rate = wins/n.
            cat_rows = conn.execute(text(
                f"SELECT category, "
                f"       COUNT(*) AS n, "
                f"       AVG(POWER(claude_probability - ({outcome_expr}), 2)) AS brier, "
                f"       AVG(claude_probability) AS mp, "
                f"       AVG({outcome_expr}) AS ma, "
                f"       COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
                f"       COALESCE(SUM(cost_usd), 0) AS cost, "
                f"       SUM(CASE WHEN settlement_outcome = side THEN 1 ELSE 0 END) AS wins "
                f"FROM pm_positions {res_where} "
                f"  AND category IS NOT NULL "
                f"GROUP BY category "
                f"ORDER BY n DESC"
            ), params).fetchall()
            by_category = [{
                "category":    r[0],
                "n":           int(r[1] or 0),
                "brier":       float(r[2]) if r[2] is not None else None,
                "mean_pred":   float(r[3]) if r[3] is not None else None,
                "mean_actual": float(r[4]) if r[4] is not None else None,
                "pnl_usd":     float(r[5]) if r[5] is not None else 0.0,
                "cost_usd":    float(r[6]) if r[6] is not None else 0.0,
                "wins":        int(r[7] or 0),
            } for r in cat_rows]

            # Per-archetype breakdown. Same shape as by_category but grouped
            # on the canonical flat-taxonomy `market_archetype` column. Lets
            # the dashboard surface ROI and win-rate per archetype so the
            # operator can see exactly which archetypes carry P&L and which
            # are dragging.
            arch_rows = conn.execute(text(
                f"SELECT market_archetype, "
                f"       COUNT(*) AS n, "
                f"       AVG(POWER(claude_probability - ({outcome_expr}), 2)) AS brier, "
                f"       AVG(claude_probability) AS mp, "
                f"       AVG({outcome_expr}) AS ma, "
                f"       COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
                f"       COALESCE(SUM(cost_usd), 0) AS cost, "
                f"       SUM(CASE WHEN settlement_outcome = side THEN 1 ELSE 0 END) AS wins "
                f"FROM pm_positions {res_where} "
                f"  AND market_archetype IS NOT NULL "
                f"GROUP BY market_archetype "
                f"ORDER BY n DESC"
            ), params).fetchall()
            by_archetype = [{
                "archetype":   r[0],
                "n":           int(r[1] or 0),
                "brier":       float(r[2]) if r[2] is not None else None,
                "mean_pred":   float(r[3]) if r[3] is not None else None,
                "mean_actual": float(r[4]) if r[4] is not None else None,
                "pnl_usd":     float(r[5]) if r[5] is not None else 0.0,
                "cost_usd":    float(r[6]) if r[6] is not None else 0.0,
                "wins":        int(r[7] or 0),
            } for r in arch_rows]

            # Horizon = hours between created_at and expected_resolution_at.
            # Rows missing expected_resolution_at are excluded from buckets.
            by_horizon: list[dict] = []
            for label, lo_h, hi_h in HORIZON_BUCKETS:
                horizon_filters = list(res_filters) + [
                    "expected_resolution_at IS NOT NULL",
                    "(julianday(expected_resolution_at) - julianday(created_at))"
                    " * 24.0 >= :h_lo",
                ]
                h_params = {**params, "h_lo": lo_h}
                if hi_h is not None:
                    horizon_filters.append(
                        "(julianday(expected_resolution_at) - julianday(created_at))"
                        " * 24.0 < :h_hi"
                    )
                    h_params["h_hi"] = hi_h
                h_where = "WHERE " + " AND ".join(horizon_filters)
                # Same shape as by_category / by_archetype: pnl + cost
                # + wins so the frontend can derive ROI and win rate.
                h_row = conn.execute(text(
                    f"SELECT COUNT(*) AS n, "
                    f"       AVG(POWER(claude_probability - ({outcome_expr}), 2)) AS brier, "
                    f"       AVG(claude_probability) AS mp, "
                    f"       AVG({outcome_expr}) AS ma, "
                    f"       COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
                    f"       COALESCE(SUM(cost_usd), 0) AS cost, "
                    f"       SUM(CASE WHEN settlement_outcome = side THEN 1 ELSE 0 END) AS wins "
                    f"FROM pm_positions {h_where}"
                ), h_params).fetchone()
                by_horizon.append({
                    "bucket":      label,
                    "n":           int(h_row[0] or 0),
                    "brier":       float(h_row[1]) if h_row[1] is not None else None,
                    "mean_pred":   float(h_row[2]) if h_row[2] is not None else None,
                    "mean_actual": float(h_row[3]) if h_row[3] is not None else None,
                    "pnl_usd":     float(h_row[4]) if h_row[4] is not None else 0.0,
                    "cost_usd":    float(h_row[5]) if h_row[5] is not None else 0.0,
                    "wins":        int(h_row[6] or 0),
                })

            # By market-favourite price band. Surfaces the
            # 0.55-0.60 / 0.70-0.80 leak finding from the
            # 2026-05-03 audit so the user can see at a glance
            # where their entries are losing money. The favourite
            # price is max(entry_price, 1 - entry_price) regardless
            # of whether we bought YES or NO.
            BAND_EDGES = [
                (0.50, 0.55), (0.55, 0.60), (0.60, 0.65),
                (0.65, 0.70), (0.70, 0.80), (0.80, 0.90),
                (0.90, 1.0001),
            ]
            by_price_band: list[dict] = []
            for lo, hi in BAND_EDGES:
                band_filters = list(res_filters) + [
                    "MAX(entry_price, 1 - entry_price) >= :pb_lo",
                    "MAX(entry_price, 1 - entry_price) <  :pb_hi",
                ]
                b_params = {**params, "pb_lo": lo, "pb_hi": hi}
                b_where = "WHERE " + " AND ".join(band_filters)
                b_row = conn.execute(text(
                    f"SELECT COUNT(*) AS n, "
                    f"       AVG(POWER(claude_probability - ({outcome_expr}), 2)) AS brier, "
                    f"       AVG(claude_probability) AS mp, "
                    f"       AVG({outcome_expr}) AS ma, "
                    f"       COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
                    f"       COALESCE(SUM(cost_usd), 0) AS cost, "
                    f"       SUM(CASE WHEN settlement_outcome = side THEN 1 ELSE 0 END) AS wins "
                    f"FROM pm_positions {b_where}"
                ), b_params).fetchone()
                # Display label: "0.55-0.60" with the upper-most
                # band rendered as "0.90+".
                hi_disp = 1.0 if hi > 1.0 else hi
                if hi_disp >= 1.0:
                    label = f"{lo:.2f}+"
                else:
                    label = f"{lo:.2f}-{hi_disp:.2f}"
                by_price_band.append({
                    "bucket":      label,
                    "n":           int(b_row[0] or 0),
                    "brier":       float(b_row[1]) if b_row[1] is not None else None,
                    "mean_pred":   float(b_row[2]) if b_row[2] is not None else None,
                    "mean_actual": float(b_row[3]) if b_row[3] is not None else None,
                    "pnl_usd":     float(b_row[4]) if b_row[4] is not None else 0.0,
                    "cost_usd":    float(b_row[5]) if b_row[5] is not None else 0.0,
                    "wins":        int(b_row[6] or 0),
                })

        return {
            "source":       source or "polymarket",
            "since_days":   since_days,
            "total":        total,
            "resolved":     resolved,
            "unresolved":   max(0, total - resolved),
            "brier":        brier,
            "mean_prob":    mean_prob,
            "mean_outcome": mean_outcome,
            "realized_pnl_usd": realized_pnl,
            "bins":         bins,
            "by_category":  by_category,
            "by_archetype": by_archetype,
            "by_price_band": by_price_band,
            "by_horizon":   by_horizon,
        }
    except Exception as exc:
        print(f"[calibration] get_report failed: {exc}", file=sys.stderr)
        return _empty_report(source, since_days)


# ── Utilities ────────────────────────────────────────────────────────────────
def conviction_to_probability(conviction: Optional[float]) -> float:
    """
    Map a 0-1 conviction score to a calibrated probability.

    Default mapping: identity with a slight pull toward 0.5 to reflect that
    raw LLM confidence scores are typically overconfident.  Once we have real
    calibration data we can refit this (isotonic regression on the bucket
    data) - that's exactly the feedback loop this module is built for.
    """
    if conviction is None:
        return 0.5
    try:
        c = float(conviction)
    except (TypeError, ValueError):
        return 0.5
    c = max(0.0, min(1.0, c))
    # Shrink 20% toward 0.5 - modest, correctable later.
    return 0.5 + 0.8 * (c - 0.5)
