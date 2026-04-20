"""
Calibration instrument — the foundation for knowing whether any of the
bot's strategies actually have edge.

Design:
  * Every actionable prediction (a crypto TRADE, a Polymarket bet, a backtest
    simulated trade) gets a row in the `predictions` table when it's made.
  * When the outcome is known (crypto trade closed, prediction market
    resolved, backtest simulated through horizon), the same row is
    resolved with the ground-truth outcome for the proposition that the
    probability referred to. For Polymarket rows, that means YES=1 / NO=0.
  * From that history we compute Brier score + reliability diagrams per
    source / per category / per bucket. That tells us whether the system's
    stated probabilities match reality — the single most important question.

No trading decisions flow through this module. Its job is purely to watch
and score. If the logger breaks the bot's trading path must still work —
every public function swallows exceptions and returns best-effort values.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
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

_PM_OUTCOME_RE = re.compile(r"\boutcome=(YES|NO)\b")
_LEGACY_YES_RE = re.compile(r"\bYES=(True|False)\b")


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
    market_archetype: Optional[str] = None,
    resolution_style: Optional[str] = None,
) -> int:
    """
    Record a prediction at the moment it's made.  Returns the row id, or -1
    on error — never raises; trading paths must not be broken by logging.

    `probability` is the calibrated probability of the proposition being
    scored. For Polymarket that proposition is always "YES resolves true".
    For other sources it can be another binary proposition, as long as the
    eventual `resolved_outcome` matches the same proposition semantics.
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
                " horizon_hours, reasoning, metadata, trade_id, "
                " market_archetype, resolution_style) "
                "VALUES "
                "(:source, :subject_key, :category, :probability, :confidence, "
                " :horizon_hours, :reasoning, :metadata, :trade_id, "
                " :market_archetype, :resolution_style) "
                "RETURNING id"
            ), {
                "source":            source,
                "subject_key":       subject_key,
                "category":          category,
                "probability":       p,
                "confidence":        confidence,
                "horizon_hours":     horizon_hours,
                "reasoning":         (reasoning or "")[:8000] or None,
                "metadata":          meta_json,
                "trade_id":          trade_id,
                "market_archetype":  market_archetype,
                "resolution_style":  resolution_style,
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
    Resolve every unresolved prediction tied to `trade_id`. Called from the
    close path. `outcome` must be the realized binary truth value for the
    proposition this prediction row represented. Never raises.
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


def repair_polymarket_resolution_history() -> dict:
    """
    Repair older Polymarket rows that were resolved using "was our side
    correct?" semantics instead of actual YES/NO market resolution.

    This is safe to run on every startup. Rows we cannot confidently infer are
    left untouched.
    """
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT p.id, p.resolved_outcome, p.resolved_note, "
                "       pp.settlement_outcome "
                "FROM predictions p "
                "LEFT JOIN pm_positions pp ON pp.prediction_id = p.id "
                "WHERE p.source = 'polymarket' "
                "  AND p.resolved_at IS NOT NULL"
            )).fetchall()

            checked = len(rows)
            fixed = 0
            skipped = 0

            for row in rows:
                pred_id = int(row[0])
                current = int(row[1]) if row[1] is not None else None
                note = str(row[2] or "")
                settlement_outcome = str(row[3] or "").upper()

                actual_yes = _infer_polymarket_yes_outcome(
                    settlement_outcome=settlement_outcome,
                    resolved_note=note,
                )
                if actual_yes is None:
                    skipped += 1
                    continue
                if current == actual_yes:
                    continue

                conn.execute(text(
                    "UPDATE predictions SET resolved_outcome = :outcome "
                    "WHERE id = :id"
                ), {"outcome": actual_yes, "id": pred_id})
                fixed += 1

        return {"checked": checked, "fixed": fixed, "skipped": skipped}
    except Exception as exc:
        print(f"[calibration] repair_polymarket_resolution_history failed: {exc}",
              file=sys.stderr)
        return {"checked": 0, "fixed": 0, "skipped": 0, "error": str(exc)}


def _infer_polymarket_yes_outcome(
    settlement_outcome: str,
    resolved_note: str,
) -> Optional[int]:
    if settlement_outcome == "YES":
        return 1
    if settlement_outcome == "NO":
        return 0

    match = _PM_OUTCOME_RE.search(resolved_note or "")
    if match:
        return 1 if match.group(1) == "YES" else 0

    match = _LEGACY_YES_RE.search(resolved_note or "")
    if match:
        return 1 if match.group(1) == "True" else 0

    return None


def record_calibration_snapshot(
    source: str,
    note: Optional[str] = None,
    force: bool = False,
    captured_at: Optional[datetime] = None,
) -> bool:
    """
    Persist a point-in-time calibration snapshot for dashboard trend charts.
    Duplicate consecutive snapshots are skipped unless `force=True`.
    """
    try:
        report = get_report(source=source)
        with get_engine().begin() as conn:
            latest = conn.execute(text(
                "SELECT resolved, total, brier, mean_prob, mean_outcome, realized_pnl_usd "
                "FROM calibration_snapshots "
                "WHERE source = :source "
                "ORDER BY captured_at DESC "
                "LIMIT 1"
            ), {"source": source}).fetchone()

            payload = {
                "source": source,
                "resolved": int(report.get("resolved", 0) or 0),
                "total": int(report.get("total", 0) or 0),
                "brier": float(report["brier"]) if report.get("brier") is not None else None,
                "mean_prob": float(report["mean_prob"]) if report.get("mean_prob") is not None else None,
                "mean_outcome": float(report["mean_outcome"]) if report.get("mean_outcome") is not None else None,
                "realized_pnl_usd": float(report["realized_pnl_usd"]) if report.get("realized_pnl_usd") is not None else None,
                "note": (note or "")[:2000] or None,
            }

            if not force and latest is not None:
                latest_tuple = tuple(latest)
                next_tuple = (
                    payload["resolved"],
                    payload["total"],
                    payload["brier"],
                    payload["mean_prob"],
                    payload["mean_outcome"],
                    payload["realized_pnl_usd"],
                )
                if latest_tuple == next_tuple:
                    return False

            sql = (
                "INSERT INTO calibration_snapshots "
                "(source, resolved, total, brier, mean_prob, mean_outcome, realized_pnl_usd, note"
            )
            values = (
                ") VALUES "
                "(:source, :resolved, :total, :brier, :mean_prob, :mean_outcome, :realized_pnl_usd, :note"
            )
            if captured_at is not None:
                sql += ", captured_at"
                values += ", :captured_at"
                payload["captured_at"] = captured_at
            sql += values + ")"
            conn.execute(text(sql), payload)
        return True
    except Exception as exc:
        print(f"[calibration] record_calibration_snapshot failed: {exc}",
              file=sys.stderr)
        return False


def seed_polymarket_snapshot_history() -> dict:
    """
    Backfill a minimal Brier history for the Polymarket dashboard.

    Because older dashboard versions did not store snapshot points, we seed:
      1. a reconstructed pre-fix snapshot using the legacy "was our side
         correct?" semantics, and
      2. a current post-fix snapshot using actual YES/NO scoring.
    """
    try:
        with get_engine().begin() as conn:
            existing = int(conn.execute(text(
                "SELECT COUNT(*) FROM calibration_snapshots WHERE source = 'polymarket'"
            )).scalar() or 0)
            if existing > 0:
                return {"seeded": False, "reason": "already_present", "count": existing}

            report = get_report(source="polymarket")
            resolved = int(report.get("resolved", 0) or 0)
            total = int(report.get("total", 0) or 0)
            if resolved <= 0:
                return {"seeded": False, "reason": "no_resolved_predictions", "count": 0}

            legacy = conn.execute(text(
                "SELECT AVG(POWER(probability - old_outcome, 2)) AS legacy_brier "
                "FROM ("
                "  SELECT p.probability, "
                "         CASE "
                "           WHEN p.resolved_note ~ 'pm_settlement outcome=(YES|NO) side=(YES|NO)' THEN "
                "             CASE "
                "               WHEN substring(p.resolved_note from 'outcome=(YES|NO)') = substring(p.resolved_note from 'side=(YES|NO)') "
                "                 THEN 1 ELSE 0 END "
                "           WHEN p.resolved_note ~ 'legacy_resolution YES=(True|False) NO=(True|False)' THEN "
                "             CASE "
                "               WHEN ((p.probability >= 0.5) AND p.resolved_note ~ 'YES=True') "
                "                 OR ((p.probability < 0.5) AND p.resolved_note ~ 'NO=True') "
                "                 THEN 1 ELSE 0 END "
                "           ELSE NULL "
                "         END AS old_outcome "
                "  FROM predictions p "
                "  WHERE p.source = 'polymarket' AND p.resolved_at IS NOT NULL"
                ") q "
                "WHERE old_outcome IS NOT NULL"
            )).fetchone()
            legacy_brier = float(legacy[0]) if legacy and legacy[0] is not None else None
            current_brier = float(report["brier"]) if report.get("brier") is not None else None

        seeded = 0
        now = datetime.now(timezone.utc)
        if legacy_brier is not None and current_brier is not None and abs(legacy_brier - current_brier) > 1e-9:
            seeded += 1 if _insert_snapshot_row(
                source="polymarket",
                resolved=resolved,
                total=total,
                brier=legacy_brier,
                mean_prob=report.get("mean_prob"),
                mean_outcome=report.get("mean_outcome"),
                realized_pnl_usd=report.get("realized_pnl_usd"),
                note="reconstructed legacy scoring (pre-fix)",
                captured_at=now - timedelta(seconds=1),
            ) else 0
        seeded += 1 if record_calibration_snapshot(
            source="polymarket",
            note="current corrected scoring",
            force=True,
            captured_at=now,
        ) else 0
        return {"seeded": bool(seeded), "inserted": seeded}
    except Exception as exc:
        print(f"[calibration] seed_polymarket_snapshot_history failed: {exc}",
              file=sys.stderr)
        return {"seeded": False, "error": str(exc)}


def _insert_snapshot_row(
    source: str,
    resolved: int,
    total: int,
    brier: Optional[float],
    mean_prob: Optional[float],
    mean_outcome: Optional[float],
    realized_pnl_usd: Optional[float],
    note: Optional[str],
    captured_at: datetime,
) -> bool:
    try:
        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO calibration_snapshots "
                "(captured_at, source, resolved, total, brier, mean_prob, mean_outcome, realized_pnl_usd, note) "
                "VALUES "
                "(:captured_at, :source, :resolved, :total, :brier, :mean_prob, :mean_outcome, :realized_pnl_usd, :note)"
            ), {
                "captured_at": captured_at,
                "source": source,
                "resolved": resolved,
                "total": total,
                "brier": brier,
                "mean_prob": mean_prob,
                "mean_outcome": mean_outcome,
                "realized_pnl_usd": realized_pnl_usd,
                "note": (note or "")[:2000] or None,
            })
        return True
    except Exception as exc:
        print(f"[calibration] _insert_snapshot_row failed: {exc}", file=sys.stderr)
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
        "mean_outcome": <float|null>,  # empirical YES base rate
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

            # Per-archetype Brier breakdown
            archetype_rows = conn.execute(text(
                f"SELECT market_archetype, "
                f"       COUNT(*) AS n, "
                f"       AVG((probability - resolved_outcome)^2) AS brier, "
                f"       AVG(probability) AS mp, "
                f"       AVG(resolved_outcome::float) AS ma, "
                f"       AVG(confidence) AS mean_conf "
                f"FROM predictions {res_where} "
                f"  AND market_archetype IS NOT NULL "
                f"GROUP BY market_archetype "
                f"ORDER BY n DESC"
            ), params).fetchall()
            by_archetype = [{
                "archetype":    r[0],
                "n":            int(r[1] or 0),
                "brier":        float(r[2]) if r[2] is not None else None,
                "mean_pred":    float(r[3]) if r[3] is not None else None,
                "mean_actual":  float(r[4]) if r[4] is not None else None,
                "mean_conf":    float(r[5]) if r[5] is not None else None,
            } for r in archetype_rows]

            # Per-resolution-style breakdown
            res_style_rows = conn.execute(text(
                f"SELECT resolution_style, "
                f"       COUNT(*) AS n, "
                f"       AVG((probability - resolved_outcome)^2) AS brier, "
                f"       AVG(probability) AS mp, "
                f"       AVG(resolved_outcome::float) AS ma "
                f"FROM predictions {res_where} "
                f"  AND resolution_style IS NOT NULL "
                f"GROUP BY resolution_style "
                f"ORDER BY n DESC"
            ), params).fetchall()
            by_resolution_style = [{
                "resolution_style": r[0],
                "n":                int(r[1] or 0),
                "brier":            float(r[2]) if r[2] is not None else None,
                "mean_pred":        float(r[3]) if r[3] is not None else None,
                "mean_actual":      float(r[4]) if r[4] is not None else None,
            } for r in res_style_rows]

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
            "by_archetype": by_archetype,
            "by_resolution_style": by_resolution_style,
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
            "by_archetype": [],
            "by_resolution_style": [],
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


# ── Enhanced calibration metrics ────────────────────────────────────────────
# Bet-weighted Brier, log score, and Murphy decomposition provide deeper
# insight than aggregate Brier alone. These tell us WHERE money is being
# lost vs where accuracy is merely fine.

def bet_weighted_brier(source: Optional[str] = None) -> Optional[float]:
    """
    Brier score weighted by stake size. Measures calibration WHERE it
    costs money — a miscalibrated prediction on a $100 bet matters more
    than on a $2 bet.
    """
    try:
        filters = ["p.resolved_at IS NOT NULL"]
        params: dict = {}
        if source:
            filters.append("p.source = :source")
            params["source"] = source
        where = "WHERE " + " AND ".join(filters)
        with get_engine().begin() as conn:
            row = conn.execute(text(
                f"SELECT "
                f"  SUM(pp.cost_usd * POWER(p.probability - p.resolved_outcome, 2)) AS weighted_brier, "
                f"  SUM(pp.cost_usd) AS total_weight "
                f"FROM predictions p "
                f"JOIN pm_positions pp ON pp.prediction_id = p.id "
                f"{where} "
                f"AND pp.cost_usd > 0"
            ), params).fetchone()
            if row and row[0] is not None and row[1] and float(row[1]) > 0:
                return float(row[0]) / float(row[1])
        return None
    except Exception as exc:
        print(f"[calibration] bet_weighted_brier failed: {exc}", file=sys.stderr)
        return None


def log_score(source: Optional[str] = None) -> Optional[float]:
    """
    Logarithmic scoring rule — theoretically aligned with Kelly sizing.
    Lower is better (like Brier). Uninformed baseline = ln(2) ≈ 0.693.

    LogScore = -mean[outcome * ln(p) + (1-outcome) * ln(1-p)]
    """
    try:
        filters = ["resolved_at IS NOT NULL"]
        params: dict = {}
        if source:
            filters.append("source = :source")
            params["source"] = source
        where = "WHERE " + " AND ".join(filters)
        with get_engine().begin() as conn:
            # Clamp probabilities to [0.001, 0.999] to prevent log(0)
            row = conn.execute(text(
                f"SELECT AVG("
                f"  -(resolved_outcome * LN(GREATEST(probability, 0.001)) "
                f"    + (1 - resolved_outcome) * LN(GREATEST(1 - probability, 0.001)))"
                f") AS log_score "
                f"FROM predictions {where}"
            ), params).fetchone()
            if row and row[0] is not None:
                return float(row[0])
        return None
    except Exception as exc:
        print(f"[calibration] log_score failed: {exc}", file=sys.stderr)
        return None


def murphy_decomposition(source: Optional[str] = None, n_bins: int = 10) -> Optional[dict]:
    """
    Decompose Brier score into three components:

    Brier = Reliability - Resolution + Uncertainty

    - Reliability: calibration error (lower is better)
    - Resolution: how much predictions vary from base rate (higher is better)
    - Uncertainty: inherent outcome variance (not controllable)

    Good forecasters have LOW reliability and HIGH resolution.
    """
    try:
        filters = ["resolved_at IS NOT NULL"]
        params: dict = {}
        if source:
            filters.append("source = :source")
            params["source"] = source
        where = "WHERE " + " AND ".join(filters)
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                f"SELECT probability, resolved_outcome::float "
                f"FROM predictions {where}"
            ), params).fetchall()

        if not rows or len(rows) < 5:
            return None

        predictions = [(float(r[0]), float(r[1])) for r in rows]
        n_total = len(predictions)
        base_rate = sum(o for _, o in predictions) / n_total
        uncertainty = base_rate * (1 - base_rate)

        # Bin predictions
        bin_width = 1.0 / n_bins
        reliability = 0.0
        resolution = 0.0

        for i in range(n_bins):
            lo = i * bin_width
            hi = (i + 1) * bin_width
            in_bin = [(p, o) for p, o in predictions if lo <= p < hi or (hi == 1.0 and p == 1.0)]
            if not in_bin:
                continue
            n_k = len(in_bin)
            mean_pred = sum(p for p, _ in in_bin) / n_k
            mean_outcome = sum(o for _, o in in_bin) / n_k
            reliability += n_k * (mean_pred - mean_outcome) ** 2
            resolution += n_k * (mean_outcome - base_rate) ** 2

        reliability /= n_total
        resolution /= n_total
        brier_reconstructed = reliability - resolution + uncertainty

        return {
            "reliability": reliability,
            "resolution": resolution,
            "uncertainty": uncertainty,
            "brier_reconstructed": brier_reconstructed,
            "base_rate": base_rate,
            "n": n_total,
        }
    except Exception as exc:
        print(f"[calibration] murphy_decomposition failed: {exc}", file=sys.stderr)
        return None


def edge_pnl_by_bucket(source: Optional[str] = None) -> list[dict]:
    """
    For each edge decile, compute expected vs realized P&L.
    Reveals whether higher claimed edge actually produces higher returns.
    """
    try:
        filters = ["p.resolved_at IS NOT NULL", "pp.status IN ('settled', 'invalid')"]
        params: dict = {}
        if source:
            filters.append("p.source = :source")
            params["source"] = source
        where = "WHERE " + " AND ".join(filters)
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                f"SELECT pp.edge_bps, pp.realized_pnl_usd, pp.cost_usd, "
                f"       pp.side, pp.entry_price "
                f"FROM predictions p "
                f"JOIN pm_positions pp ON pp.prediction_id = p.id "
                f"{where} "
                f"AND pp.edge_bps IS NOT NULL "
                f"ORDER BY pp.edge_bps ASC"
            ), params).fetchall()

        if not rows:
            return []

        # Create edge buckets: 0-200, 200-400, 400-800, 800-1500, 1500+
        buckets = [
            ("0-200bps", 0, 200),
            ("200-400bps", 200, 400),
            ("400-800bps", 400, 800),
            ("800-1500bps", 800, 1500),
            ("1500+bps", 1500, 999999),
        ]
        result = []
        for label, lo, hi in buckets:
            in_bucket = [r for r in rows if lo <= float(r[0]) < hi]
            if not in_bucket:
                result.append({"bucket": label, "n": 0, "total_pnl": 0, "avg_pnl": None, "win_rate": None})
                continue
            n = len(in_bucket)
            total_pnl = sum(float(r[1] or 0) for r in in_bucket)
            wins = sum(1 for r in in_bucket if float(r[1] or 0) > 0)
            result.append({
                "bucket": label,
                "n": n,
                "total_pnl": total_pnl,
                "avg_pnl": total_pnl / n,
                "win_rate": wins / n if n > 0 else None,
            })
        return result
    except Exception as exc:
        print(f"[calibration] edge_pnl_by_bucket failed: {exc}", file=sys.stderr)
        return []


def get_enhanced_report(source: Optional[str] = None) -> dict:
    """
    Extended calibration report with all enhanced metrics.
    Includes everything from get_report() plus bet-weighted Brier,
    log score, Murphy decomposition, and edge-P&L analysis.
    """
    report = get_report(source=source)
    report["bet_weighted_brier"] = bet_weighted_brier(source=source)
    report["log_score"] = log_score(source=source)
    report["murphy"] = murphy_decomposition(source=source)
    report["edge_pnl_buckets"] = edge_pnl_by_bucket(source=source)
    return report
