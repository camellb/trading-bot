"""
Calibration instrument — the foundation for knowing whether any of the
bot's strategies actually have edge.

Design:
  * Every actionable prediction (a crypto TRADE, a Polymarket bet, a backtest
    simulated trade) gets a row in the `predictions` table when it's made.
  * When the outcome is known (crypto trade closed, prediction market
    resolved, backtest simulated through horizon), the same row is
    resolved with outcome (1 = correct, 0 = incorrect) and realized P&L.
  * From that history we compute Brier score + reliability diagrams per
    source / per category / per bucket.  That tells us whether the system's
    stated probabilities match reality — the single most important question.

No trading decisions flow through this module.  Its job is purely to watch
and score.  If the logger breaks the bot's trading path must still work —
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

# Bucket edges for reliability diagrams — five equal-width bins in [0,1].
RELIABILITY_BINS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]

# Time-horizon buckets for Brier breakdowns — (label, lower_hours, upper_hours).
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
    on error — never raises; trading paths must not be broken by logging.

    `probability` is the (claimed) calibrated probability the prediction
    is correct, in [0,1].  For a LONG crypto trade, "correct" = "closes
    with positive P&L".  For a Polymarket bet, "correct" = "market resolves
    YES if the bet was YES".  The scoring primitive is the same everywhere.
    """
    try:
        p = float(probability)
        # Clamp defensively — a sloppy source shouldn't corrupt the dataset.
        p = max(0.0, min(1.0, p))
        meta_json = json.dumps(metadata) if metadata is not None else None
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "INSERT INTO predictions "
                "(source, subject_key, category, probability, confidence, "
                " horizon_hours, reasoning, metadata, trade_id) "
                "VALUES "
                "(:source, :subject_key, :category, :probability, :confidence, "
                " :horizon_hours, :reasoning, :metadata, :trade_id) "
                "RETURNING id"
            ), {
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
                "  resolved_at      = NOW(), "
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
    """Direct-id resolution — used by prediction-market + backtest paths."""
    try:
        if outcome not in (0, 1):
            outcome = 1 if outcome else 0
        with get_engine().begin() as conn:
            conn.execute(text(
                "UPDATE predictions SET "
                "  resolved_at      = NOW(), "
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
def get_report(
    source: Optional[str] = None,
    since_days: Optional[int] = None,
) -> dict:
    """
    Return everything the dashboard needs to render the calibration card.

    Shape:
      {
        "source": <source or "all">,
        "since_days": <int or null>,
        "total":       <all predictions (resolved+unresolved)>,
        "resolved":    <resolved count>,
        "unresolved":  <unresolved count>,
        "brier":       <float|null>,
        "mean_prob":   <float|null>,   # average stated probability
        "mean_outcome": <float|null>,  # empirical base rate
        "realized_pnl_usd": <float|null>,
        "bins":        [ {lo, hi, n, mean_pred, mean_actual}, ... ],
        "by_category": [ {category, n, brier, mean_pred, mean_actual}, ... ],
      }

    All keys are always present.  Empty dataset → nulls + zero counts.
    """
    try:
        filters = []
        params: dict = {}
        if source:
            filters.append("source = :source")
            params["source"] = source
        if since_days and since_days > 0:
            filters.append("resolved_at >= NOW() - INTERVAL ':d days'".replace(":d", str(int(since_days))))
        where_clause = ("WHERE " + " AND ".join(filters)) if filters else ""

        with get_engine().begin() as conn:
            totals = conn.execute(text(
                f"SELECT "
                f"  COUNT(*) AS total, "
                f"  COUNT(resolved_at) AS resolved "
                f"FROM predictions {where_clause}"
            ), params).fetchone()
            total     = int(totals[0] or 0)
            resolved  = int(totals[1] or 0)

            # Resolved-only aggregates
            res_filters = filters + ["resolved_at IS NOT NULL"]
            res_where = "WHERE " + " AND ".join(res_filters)
            agg = conn.execute(text(
                f"SELECT "
                f"  AVG(probability)                AS mean_prob, "
                f"  AVG(resolved_outcome::float)    AS mean_outcome, "
                f"  AVG((probability - resolved_outcome)^2) AS brier, "
                f"  SUM(resolved_pnl_usd)           AS pnl "
                f"FROM predictions {res_where}"
            ), params).fetchone()
            mean_prob    = float(agg[0]) if agg[0] is not None else None
            mean_outcome = float(agg[1]) if agg[1] is not None else None
            brier        = float(agg[2]) if agg[2] is not None else None
            realized_pnl = float(agg[3]) if agg[3] is not None else None

            # Reliability bins — predicted prob bucket → actual win rate
            bins: list[dict] = []
            for lo, hi in RELIABILITY_BINS:
                # Top bucket is inclusive on the high edge so 1.0 counts.
                cmp_hi = "<=" if hi == 1.0 else "<"
                bin_sql = (
                    f"SELECT COUNT(*) AS n, AVG(probability) AS mp, "
                    f"       AVG(resolved_outcome::float) AS ma "
                    f"FROM predictions {res_where} "
                    f"  AND probability >= :lo "
                    f"  AND probability {cmp_hi} :hi"
                )
                row = conn.execute(text(bin_sql), {**params, "lo": lo, "hi": hi}).fetchone()
                n = int(row[0] or 0)
                bins.append({
                    "lo": lo,
                    "hi": hi,
                    "n":  n,
                    "mean_pred":   float(row[1]) if row[1] is not None else None,
                    "mean_actual": float(row[2]) if row[2] is not None else None,
                })

            # Per-category breakdown — playbook calibration on the crypto side.
            cat_rows = conn.execute(text(
                f"SELECT category, "
                f"       COUNT(*) AS n, "
                f"       AVG((probability - resolved_outcome)^2) AS brier, "
                f"       AVG(probability) AS mp, "
                f"       AVG(resolved_outcome::float) AS ma "
                f"FROM predictions {res_where} "
                f"  AND category IS NOT NULL "
                f"GROUP BY category "
                f"ORDER BY n DESC"
            ), params).fetchall()
            by_category = [{
                "category":     r[0],
                "n":            int(r[1] or 0),
                "brier":        float(r[2]) if r[2] is not None else None,
                "mean_pred":    float(r[3]) if r[3] is not None else None,
                "mean_actual":  float(r[4]) if r[4] is not None else None,
            } for r in cat_rows]

            # Per-horizon-bucket Brier breakdown
            by_horizon: list[dict] = []
            for label, lo_h, hi_h in HORIZON_BUCKETS:
                horizon_filters = list(res_filters) + ["horizon_hours IS NOT NULL"]
                horizon_filters.append("horizon_hours >= :h_lo")
                h_params = {**params, "h_lo": lo_h}
                if hi_h is not None:
                    horizon_filters.append("horizon_hours < :h_hi")
                    h_params["h_hi"] = hi_h
                h_where = "WHERE " + " AND ".join(horizon_filters)
                h_row = conn.execute(text(
                    f"SELECT COUNT(*) AS n, "
                    f"       AVG((probability - resolved_outcome)^2) AS brier, "
                    f"       AVG(probability) AS mp, "
                    f"       AVG(resolved_outcome::float) AS ma "
                    f"FROM predictions {h_where}"
                ), h_params).fetchone()
                n = int(h_row[0] or 0)
                by_horizon.append({
                    "bucket":      label,
                    "n":           n,
                    "brier":       float(h_row[1]) if h_row[1] is not None else None,
                    "mean_pred":   float(h_row[2]) if h_row[2] is not None else None,
                    "mean_actual": float(h_row[3]) if h_row[3] is not None else None,
                })

        return {
            "source":       source or "all",
            "since_days":   since_days,
            "total":        total,
            "resolved":     resolved,
            "unresolved":   total - resolved,
            "brier":        brier,
            "mean_prob":    mean_prob,
            "mean_outcome": mean_outcome,
            "realized_pnl_usd": realized_pnl,
            "bins":         bins,
            "by_category":  by_category,
            "by_horizon":   by_horizon,
        }
    except Exception as exc:
        print(f"[calibration] get_report failed: {exc}", file=sys.stderr)
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
            "bins":         [{"lo": lo, "hi": hi, "n": 0, "mean_pred": None, "mean_actual": None}
                             for lo, hi in RELIABILITY_BINS],
            "by_category":  [],
            "by_horizon":   [{"bucket": label, "n": 0, "brier": None,
                              "mean_pred": None, "mean_actual": None}
                             for label, _, _ in HORIZON_BUCKETS],
        }


# ── Utilities ────────────────────────────────────────────────────────────────
def conviction_to_probability(conviction: Optional[float]) -> float:
    """
    Map a 0-1 conviction score to a calibrated probability.

    Default mapping: identity with a slight pull toward 0.5 to reflect that
    raw LLM confidence scores are typically overconfident.  Once we have real
    calibration data we can refit this (isotonic regression on the bucket
    data) — that's exactly the feedback loop this module is built for.
    """
    if conviction is None:
        return 0.5
    try:
        c = float(conviction)
    except (TypeError, ValueError):
        return 0.5
    c = max(0.0, min(1.0, c))
    # Shrink 20% toward 0.5 — modest, correctable later.
    return 0.5 + 0.8 * (c - 0.5)
