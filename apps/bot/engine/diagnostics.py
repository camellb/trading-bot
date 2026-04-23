"""
Diagnostic engine - shared metrics module for forecaster, sizer,
and system health.

Single source of truth for both the dashboard (read-only) and the learning
cadence (evidence for pending_suggestions). Every public function reads from
the canonical tables (`predictions`, `pm_positions`, `market_evaluations`)
and returns structured data that always includes sample size so downstream
consumers can gate on n ≥ 20 or similar thresholds.

Design principles:
  * Read-only. Nothing in this module mutates DB state or trading behaviour.
  * 5-minute TTL cache. Each public function routes through an `_impl` that
    is `@lru_cache`-decorated with a time-bucketed key. A manual
    `clear_cache()` hook is provided for tests and for forced refresh.
  * Scope-aware. Every forecaster metric accepts `scope ∈ {all, traded,
    skipped}` - "traded" restricts to predictions linked to a pm_position;
    "skipped" restricts to predictions not linked to one (bot evaluated
    but didn't take the bet in that mode).
  * Best-effort. Every public function swallows DB/computation errors and
    returns an empty/zero shape instead of raising - diagnostics must never
    take the trading path down.
"""

from __future__ import annotations

import sys
import time
from functools import lru_cache
from typing import Iterable, Literal, Optional

# COST_ASSUMPTION mirrors the sizer - imported lazily inside cost_validation
# to keep this module importable when the sizer is unavailable (tests).


# ── Constants ────────────────────────────────────────────────────────────────
Scope = Literal["all", "traded", "skipped"]

# 10 equal-width reliability bins for the diagnostic view (finer than
# calibration.py's 5-bin roll-up - the extra resolution is needed to spot
# overconfidence at the tails).
CALIBRATION_BINS: list[tuple[float, float]] = [
    (i / 10.0, (i + 1) / 10.0) for i in range(10)
]

# Horizon buckets in hours. (label, lo, hi) - hi=None means unbounded.
HORIZON_BUCKETS: list[tuple[str, float, Optional[float]]] = [
    ("< 1d",  0.0,   24.0),
    ("1-7d",  24.0,  168.0),
    ("7-30d", 168.0, 720.0),
    ("30d+",  720.0, None),
]

# EV buckets mirror backtester.forecast_backtester.EV_BUCKETS so diagnostics
# can compare realised-vs-simulated across the same grid. Under the
# three-gate sizer doctrine EV is no longer a fire condition - these
# buckets exist for schema continuity and historical reporting only.
EV_BUCKETS: list[tuple[str, float, float]] = [
    ("3-5%",   0.03, 0.05),
    ("5-10%",  0.05, 0.10),
    ("10-20%", 0.10, 0.20),
    ("20%+",   0.20, float("inf")),
]

# Minimum sample for any per-bucket output to carry a "usable" flag.
MIN_BUCKET_N = 20

# Counterfactual stake used for the skipped-side selection-quality
# calculation. Flat by design - the goal is to compare the directional
# call, not bankroll-adjusted sizing.
_HYPOTHETICAL_STAKE_USD = 10.0

_CACHE_TTL_SECONDS = 300  # 5 minutes

# Log-score floor - avoids -inf when a resolved outcome sat at p≈0 or p≈1.
_LOGSCORE_EPS = 1e-9


def _cache_bucket() -> int:
    """Integer bucket that rotates every _CACHE_TTL_SECONDS seconds."""
    return int(time.time() // _CACHE_TTL_SECONDS)


def clear_cache() -> None:
    """Invalidate every cached impl. Call from tests or after large writes."""
    for fn in (
        _calibration_curve_impl,
        _brier_score_impl,
        _log_score_impl,
        _brier_by_archetype_impl,
        _brier_by_horizon_impl,
        _selection_quality_impl,
        _roi_by_ev_bucket_impl,
        _cost_validation_impl,
        _bankroll_series_impl,
        _theoretical_optimal_roi_impl,
        _archetype_pnl_attribution_impl,
    ):
        try:
            fn.cache_clear()
        except Exception:
            pass


# ── Scope helper ────────────────────────────────────────────────────────────
def _scope_clause(scope: str) -> str:
    """SQL fragment (no leading AND) constraining predictions by scope."""
    if scope == "traded":
        return "p.trade_id IS NOT NULL"
    if scope == "skipped":
        return "p.trade_id IS NULL"
    return "TRUE"


def _get_engine():
    """Lazy import so the module is importable without DATABASE_URL."""
    from db.engine import get_engine  # noqa: WPS433
    return get_engine()


# ── Small numeric helpers ───────────────────────────────────────────────────
def _safe_div(num: float, den: float) -> Optional[float]:
    try:
        if den == 0:
            return None
        return float(num) / float(den)
    except Exception:
        return None


def _brier(p: float, outcome: int) -> float:
    return (float(p) - float(outcome)) ** 2


def _log_loss(p: float, outcome: int) -> float:
    import math
    p = min(max(float(p), _LOGSCORE_EPS), 1.0 - _LOGSCORE_EPS)
    return -(float(outcome) * math.log(p) + (1 - float(outcome)) * math.log(1 - p))


# ── Public: calibration curve ───────────────────────────────────────────────
def calibration_curve(scope: Scope = "all") -> dict:
    """10-bin reliability diagram over resolved predictions."""
    return _calibration_curve_impl(scope, _cache_bucket())


@lru_cache(maxsize=32)
def _calibration_curve_impl(scope: str, _bucket: int) -> dict:
    try:
        from sqlalchemy import text
        bins: list[dict] = []
        with _get_engine().begin() as conn:
            total_row = conn.execute(text(
                "SELECT COUNT(*) FROM predictions p "
                "WHERE p.source IN ('polymarket','polymarket_live','polymarket_simulation') "
                "  AND p.resolved_outcome IN (0,1) "
                "  AND p.probability IS NOT NULL "
                f"  AND {_scope_clause(scope)}"
            )).scalar()
            total = int(total_row or 0)
            for i, (lo, hi) in enumerate(CALIBRATION_BINS):
                cmp_hi = "<=" if i == len(CALIBRATION_BINS) - 1 else "<"
                row = conn.execute(text(
                    "SELECT COUNT(*) AS n, "
                    "       AVG(p.probability) AS mp, "
                    "       AVG(p.resolved_outcome::float) AS ma "
                    "FROM predictions p "
                    "WHERE p.source IN ('polymarket','polymarket_live','polymarket_simulation') "
                    "  AND p.resolved_outcome IN (0,1) "
                    "  AND p.probability IS NOT NULL "
                    f"  AND {_scope_clause(scope)} "
                    "  AND p.probability >= :lo "
                    f"  AND p.probability {cmp_hi} :hi"
                ), {"lo": lo, "hi": hi}).fetchone()
                n = int(row[0] or 0)
                bins.append({
                    "lo":           lo,
                    "hi":           hi,
                    "n":            n,
                    "mean_pred":    float(row[1]) if row[1] is not None else None,
                    "mean_actual":  float(row[2]) if row[2] is not None else None,
                    "usable":       n >= MIN_BUCKET_N,
                })
        return {"scope": scope, "total": total, "bins": bins}
    except Exception as exc:
        print(f"[diagnostics] calibration_curve failed: {exc}", file=sys.stderr)
        return {
            "scope": scope, "total": 0,
            "bins": [{"lo": lo, "hi": hi, "n": 0, "mean_pred": None,
                      "mean_actual": None, "usable": False}
                     for lo, hi in CALIBRATION_BINS],
        }


# ── Public: Brier and log score ─────────────────────────────────────────────
def brier_score(
    scope: Scope = "all",
    archetype: Optional[str] = None,
    horizon_bucket: Optional[str] = None,
) -> dict:
    """Global Brier score, optionally restricted to archetype / horizon bucket."""
    return _brier_score_impl(scope, archetype, horizon_bucket, _cache_bucket())


@lru_cache(maxsize=128)
def _brier_score_impl(scope: str, archetype: Optional[str],
                      horizon_bucket: Optional[str], _bucket: int) -> dict:
    try:
        from sqlalchemy import text
        filters = [
            "p.source IN ('polymarket','polymarket_live','polymarket_simulation')",
            "p.resolved_outcome IN (0,1)",
            "p.probability IS NOT NULL",
            _scope_clause(scope),
        ]
        params: dict = {}
        if archetype is not None:
            filters.append("p.category = :cat")
            params["cat"] = archetype
        if horizon_bucket is not None:
            lo_h, hi_h = _horizon_range(horizon_bucket)
            if lo_h is None:
                return _empty_score(scope, archetype, horizon_bucket)
            filters.append("p.horizon_hours IS NOT NULL")
            filters.append("p.horizon_hours >= :hlo")
            params["hlo"] = lo_h
            if hi_h is not None:
                filters.append("p.horizon_hours < :hhi")
                params["hhi"] = hi_h
        where = " AND ".join(filters)
        with _get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT COUNT(*) AS n, "
                "       AVG((p.probability - p.resolved_outcome)^2) AS brier, "
                "       AVG(p.probability) AS mp, "
                "       AVG(p.resolved_outcome::float) AS ma "
                f"FROM predictions p WHERE {where}"
            ), params).fetchone()
        n = int(row[0] or 0)
        return {
            "scope":       scope,
            "archetype":   archetype,
            "horizon":     horizon_bucket,
            "n":           n,
            "brier":       float(row[1]) if row[1] is not None else None,
            "mean_pred":   float(row[2]) if row[2] is not None else None,
            "mean_actual": float(row[3]) if row[3] is not None else None,
            "usable":      n >= MIN_BUCKET_N,
        }
    except Exception as exc:
        print(f"[diagnostics] brier_score failed: {exc}", file=sys.stderr)
        return _empty_score(scope, archetype, horizon_bucket)


def _empty_score(scope, archetype, horizon_bucket) -> dict:
    return {"scope": scope, "archetype": archetype, "horizon": horizon_bucket,
            "n": 0, "brier": None, "mean_pred": None, "mean_actual": None,
            "usable": False}


def _horizon_range(label: str) -> tuple[Optional[float], Optional[float]]:
    for lab, lo, hi in HORIZON_BUCKETS:
        if lab == label:
            return lo, hi
    return None, None


def log_score(scope: Scope = "all",
              archetype: Optional[str] = None) -> dict:
    """Mean log-loss (negative log probability assigned to the true outcome)."""
    return _log_score_impl(scope, archetype, _cache_bucket())


@lru_cache(maxsize=64)
def _log_score_impl(scope: str, archetype: Optional[str], _bucket: int) -> dict:
    try:
        rows = _fetch_resolved_rows(scope=scope, archetype=archetype)
        if not rows:
            return {"scope": scope, "archetype": archetype, "n": 0,
                    "log_loss": None, "usable": False}
        losses = [_log_loss(p, o) for (p, o, *_) in rows]
        n = len(rows)
        mean = sum(losses) / n
        return {"scope": scope, "archetype": archetype, "n": n,
                "log_loss": mean, "usable": n >= MIN_BUCKET_N}
    except Exception as exc:
        print(f"[diagnostics] log_score failed: {exc}", file=sys.stderr)
        return {"scope": scope, "archetype": archetype, "n": 0,
                "log_loss": None, "usable": False}


def _fetch_resolved_rows(scope: str,
                         archetype: Optional[str] = None
                         ) -> list[tuple[float, int, Optional[str], Optional[float]]]:
    """(probability, resolved_outcome, category, horizon_hours) tuples."""
    from sqlalchemy import text
    filters = [
        "p.source IN ('polymarket','polymarket_live','polymarket_simulation')",
        "p.resolved_outcome IN (0,1)",
        "p.probability IS NOT NULL",
        _scope_clause(scope),
    ]
    params: dict = {}
    if archetype is not None:
        filters.append("p.category = :cat")
        params["cat"] = archetype
    where = " AND ".join(filters)
    with _get_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT p.probability, p.resolved_outcome, p.category, p.horizon_hours "
            f"FROM predictions p WHERE {where}"
        ), params).fetchall()
    return [(float(r[0]), int(r[1]),
             r[2] if r[2] is not None else None,
             float(r[3]) if r[3] is not None else None)
            for r in rows]


# ── Public: breakdowns ──────────────────────────────────────────────────────
def brier_by_archetype(scope: Scope = "all",
                       flag_threshold: float = 0.25) -> list[dict]:
    """Brier broken out by category. flag=True means brier > threshold."""
    return _brier_by_archetype_impl(scope, flag_threshold, _cache_bucket())


@lru_cache(maxsize=16)
def _brier_by_archetype_impl(scope: str, flag_threshold: float,
                             _bucket: int) -> list[dict]:
    try:
        from sqlalchemy import text
        with _get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT p.category, COUNT(*) AS n, "
                "       AVG((p.probability - p.resolved_outcome)^2) AS brier, "
                "       AVG(p.probability) AS mp, "
                "       AVG(p.resolved_outcome::float) AS ma "
                "FROM predictions p "
                "WHERE p.source IN ('polymarket','polymarket_live','polymarket_simulation') "
                "  AND p.resolved_outcome IN (0,1) "
                "  AND p.probability IS NOT NULL "
                "  AND p.category IS NOT NULL "
                f"  AND {_scope_clause(scope)} "
                "GROUP BY p.category ORDER BY n DESC"
            )).fetchall()
        out = []
        for r in rows:
            n = int(r[1] or 0)
            brier = float(r[2]) if r[2] is not None else None
            out.append({
                "archetype":   r[0],
                "n":           n,
                "brier":       brier,
                "mean_pred":   float(r[3]) if r[3] is not None else None,
                "mean_actual": float(r[4]) if r[4] is not None else None,
                "usable":      n >= MIN_BUCKET_N,
                "flagged":     (brier is not None
                                and brier > flag_threshold
                                and n >= MIN_BUCKET_N),
            })
        return out
    except Exception as exc:
        print(f"[diagnostics] brier_by_archetype failed: {exc}", file=sys.stderr)
        return []


def brier_by_horizon(scope: Scope = "all",
                     flag_threshold: float = 0.25) -> list[dict]:
    return _brier_by_horizon_impl(scope, flag_threshold, _cache_bucket())


@lru_cache(maxsize=16)
def _brier_by_horizon_impl(scope: str, flag_threshold: float,
                           _bucket: int) -> list[dict]:
    out: list[dict] = []
    for label, _, _ in HORIZON_BUCKETS:
        rec = _brier_score_impl(scope, None, label, _bucket)
        n = int(rec["n"])
        out.append({
            "bucket":      label,
            "n":           n,
            "brier":       rec["brier"],
            "mean_pred":   rec["mean_pred"],
            "mean_actual": rec["mean_actual"],
            "usable":      n >= MIN_BUCKET_N,
            "flagged":     (rec["brier"] is not None
                            and rec["brier"] > flag_threshold
                            and n >= MIN_BUCKET_N),
        })
    return out


# ── Public: selection quality (traded vs skipped counterfactual) ────────────
def selection_quality(user_id: Optional[str] = None) -> dict:
    """
    Compare realised ROI on traded positions against a flat $10 counterfactual
    on skipped evaluations. Tells us whether the selection gate is picking
    the right markets. Sample sizes included.

    When `user_id` is provided, the traded side is scoped to that user's
    positions. The skipped counterfactual remains global (predictions are
    shared across tenants).
    """
    return _selection_quality_impl(user_id, _cache_bucket())


@lru_cache(maxsize=32)
def _selection_quality_impl(user_id: Optional[str], _bucket: int) -> dict:
    try:
        from sqlalchemy import text
        user_clause = "  AND user_id = :uid " if user_id else ""
        params: dict = {"uid": user_id} if user_id else {}
        with _get_engine().begin() as conn:
            traded = conn.execute(text(
                "SELECT COUNT(*), "
                "       COALESCE(SUM(realized_pnl_usd),0), "
                "       COALESCE(SUM(cost_usd),0) "
                "FROM pm_positions "
                "WHERE status IN ('settled','invalid') "
                "  AND realized_pnl_usd IS NOT NULL "
                f"{user_clause}"
            ), params).fetchone()
            traded_n    = int(traded[0] or 0)
            traded_pnl  = float(traded[1] or 0.0)
            traded_cost = float(traded[2] or 0.0)

            # Counterfactual: for every resolved prediction NOT linked to a
            # position, compute the $10-flat-stake P&L of the side Claude
            # preferred (p > 0.5 → YES; p < 0.5 → NO; == 0.5 → skip).
            rows = conn.execute(text(
                "SELECT me.market_price_yes, p.probability, p.resolved_outcome "
                "FROM predictions p "
                "LEFT JOIN market_evaluations me ON me.prediction_id = p.id "
                "WHERE p.trade_id IS NULL "
                "  AND p.resolved_outcome IN (0,1) "
                "  AND me.market_price_yes IS NOT NULL "
                "  AND p.probability IS NOT NULL"
            )).fetchall()
        skipped_n = 0
        skipped_pnl = 0.0
        skipped_cost = 0.0
        for mprice, prob, outcome in rows:
            mprice = float(mprice)
            prob = float(prob)
            outcome = int(outcome)
            if prob > 0.5:
                side = "YES"
                entry = mprice
            elif prob < 0.5:
                side = "NO"
                entry = 1.0 - mprice
            else:
                continue
            if entry <= 0 or entry >= 1:
                continue
            shares = _HYPOTHETICAL_STAKE_USD / entry
            won = (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)
            proceeds = shares if won else 0.0
            pnl = proceeds - _HYPOTHETICAL_STAKE_USD
            skipped_n += 1
            skipped_pnl += pnl
            skipped_cost += _HYPOTHETICAL_STAKE_USD
        return {
            "traded": {
                "n":       traded_n,
                "pnl":     traded_pnl,
                "cost":    traded_cost,
                "roi":     _safe_div(traded_pnl, traded_cost),
                "usable":  traded_n >= MIN_BUCKET_N,
            },
            "skipped_counterfactual": {
                "n":            skipped_n,
                "hypothetical_pnl":  skipped_pnl,
                "hypothetical_cost": skipped_cost,
                "roi":          _safe_div(skipped_pnl, skipped_cost),
                "usable":       skipped_n >= MIN_BUCKET_N,
                "stake_usd":    _HYPOTHETICAL_STAKE_USD,
            },
        }
    except Exception as exc:
        print(f"[diagnostics] selection_quality failed: {exc}", file=sys.stderr)
        return {
            "traded": {"n": 0, "pnl": 0.0, "cost": 0.0, "roi": None,
                       "usable": False},
            "skipped_counterfactual": {"n": 0, "hypothetical_pnl": 0.0,
                                       "hypothetical_cost": 0.0, "roi": None,
                                       "usable": False,
                                       "stake_usd": _HYPOTHETICAL_STAKE_USD},
        }


# ── Public: ROI by EV bucket (realised) ────────────────────────────────────
def roi_by_ev_bucket(user_id: Optional[str] = None) -> list[dict]:
    """Realised ROI per EV bucket from pm_positions.ev_bps."""
    return _roi_by_ev_bucket_impl(user_id, _cache_bucket())


@lru_cache(maxsize=32)
def _roi_by_ev_bucket_impl(user_id: Optional[str], _bucket: int) -> list[dict]:
    try:
        from sqlalchemy import text
        user_clause = "  AND user_id = :uid " if user_id else ""
        params: dict = {"uid": user_id} if user_id else {}
        with _get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT ev_bps, cost_usd, realized_pnl_usd, status "
                "FROM pm_positions "
                "WHERE ev_bps IS NOT NULL "
                "  AND status IN ('settled','invalid') "
                f"{user_clause}"
            ), params).fetchall()
        out = []
        for label, lo, hi in EV_BUCKETS:
            in_bucket = [
                (float(r[1] or 0.0), float(r[2] or 0.0))
                for r in rows
                if r[0] is not None and lo <= (float(r[0]) / 10000.0) < hi
            ]
            n = len(in_bucket)
            cost = sum(c for c, _ in in_bucket)
            pnl  = sum(p for _, p in in_bucket)
            out.append({
                "bucket":   label,
                "ev_lo":    lo,
                "ev_hi":    hi if hi != float("inf") else None,
                "n":        n,
                "pnl":      pnl,
                "cost":     cost,
                "roi":      _safe_div(pnl, cost),
                "usable":   n >= MIN_BUCKET_N,
            })
        return out
    except Exception as exc:
        print(f"[diagnostics] roi_by_ev_bucket failed: {exc}", file=sys.stderr)
        return []


# ── Public: cost validation (theoretical vs realised) ───────────────────────
def cost_validation(user_id: Optional[str] = None) -> dict:
    """
    Compare assumed COST_ASSUMPTION (sizer) against the realised cost implied
    by the delta between theoretical (perfect-settlement-at-$1) P&L and
    actually realised P&L on settled positions.

      implied_cost = (theoretical_pnl - realised_pnl) / total_notional
    """
    return _cost_validation_impl(user_id, _cache_bucket())


@lru_cache(maxsize=32)
def _cost_validation_impl(user_id: Optional[str], _bucket: int) -> dict:
    try:
        # Defer import to avoid hard coupling during tests.
        try:
            from execution.pm_sizer import COST_ASSUMPTION as _ASSUMED  # type: ignore
        except Exception:
            _ASSUMED = 0.015
        from sqlalchemy import text
        user_clause = "  AND user_id = :uid " if user_id else ""
        params: dict = {"uid": user_id} if user_id else {}
        with _get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT shares, entry_price, cost_usd, realized_pnl_usd, "
                "       side, settlement_outcome "
                "FROM pm_positions "
                "WHERE status IN ('settled','invalid') "
                "  AND realized_pnl_usd IS NOT NULL "
                "  AND shares IS NOT NULL AND entry_price IS NOT NULL "
                f"{user_clause}"
            ), params).fetchall()
        n = 0
        total_notional = 0.0
        realised_pnl = 0.0
        theoretical_pnl = 0.0
        for shares, entry, cost, realised, side, outcome in rows:
            shares = float(shares)
            entry = float(entry)
            cost = float(cost)
            realised = float(realised)
            notional = cost
            if notional <= 0:
                continue
            won = (
                (side == "YES" and outcome == "YES") or
                (side == "NO"  and outcome == "NO")
            )
            proceeds_theoretical = shares if won else 0.0
            # Theoretical P&L assumes zero fees/slippage.
            theoretical = proceeds_theoretical - cost
            n += 1
            total_notional += notional
            realised_pnl += realised
            theoretical_pnl += theoretical
        implied = _safe_div(theoretical_pnl - realised_pnl, total_notional)
        return {
            "n":               n,
            "assumed_cost":    float(_ASSUMED),
            "implied_cost":    implied,
            "theoretical_pnl": theoretical_pnl,
            "realised_pnl":    realised_pnl,
            "total_notional":  total_notional,
            "usable":          n >= MIN_BUCKET_N,
        }
    except Exception as exc:
        print(f"[diagnostics] cost_validation failed: {exc}", file=sys.stderr)
        return {"n": 0, "assumed_cost": 0.015, "implied_cost": None,
                "theoretical_pnl": 0.0, "realised_pnl": 0.0,
                "total_notional": 0.0, "usable": False}


# ── Public: bankroll series ─────────────────────────────────────────────────
def bankroll_series(resolution: str = "daily",
                    starting_cash: Optional[float] = None,
                    user_id: Optional[str] = None) -> list[dict]:
    """
    Cumulative realised P&L over time, by day or hour.

    When `user_id` is provided the series is scoped to that user's settled
    positions only. When user_id is None the series is global (admin use).
    """
    return _bankroll_series_impl(resolution, starting_cash, user_id,
                                  _cache_bucket())


@lru_cache(maxsize=32)
def _bankroll_series_impl(resolution: str, starting_cash: Optional[float],
                          user_id: Optional[str],
                          _bucket: int) -> list[dict]:
    try:
        from sqlalchemy import text
        grain = "day" if resolution != "hour" else "hour"
        params: dict = {}
        user_clause = ""
        if user_id:
            user_clause = "  AND user_id = :uid "
            params["uid"] = user_id
        with _get_engine().begin() as conn:
            rows = conn.execute(text(
                f"SELECT date_trunc('{grain}', settled_at) AS ts, "
                "       COALESCE(SUM(realized_pnl_usd),0) AS pnl "
                "FROM pm_positions "
                "WHERE status IN ('settled','invalid') "
                "  AND settled_at IS NOT NULL "
                "  AND realized_pnl_usd IS NOT NULL "
                f"{user_clause}"
                "GROUP BY ts ORDER BY ts ASC"
            ), params).fetchall()
        base = float(starting_cash) if starting_cash is not None else 0.0
        cum = base
        out = []
        for ts, pnl in rows:
            cum += float(pnl or 0.0)
            out.append({
                "ts":       ts.isoformat() if ts is not None else None,
                "pnl":      float(pnl or 0.0),
                "bankroll": cum,
            })
        return out
    except Exception as exc:
        print(f"[diagnostics] bankroll_series failed: {exc}", file=sys.stderr)
        return []


# ── Public: theoretical optimal (best case if every call were resolved) ─────
def theoretical_optimal_roi(user_id: Optional[str] = None) -> dict:
    """
    Upper-bound ROI if every Delfi call had resolved in the claimed direction
    with zero fees. Gap between this and realised ROI is slippage + missed
    calls.

    When `user_id` is supplied the counterfactual is restricted to predictions
    that this user's positions are linked to (so the per-user report stays
    aligned with their own trade history).
    """
    return _theoretical_optimal_roi_impl(user_id, _cache_bucket())


@lru_cache(maxsize=32)
def _theoretical_optimal_roi_impl(user_id: Optional[str],
                                  _bucket: int) -> dict:
    try:
        from sqlalchemy import text
        user_clause = ""
        params: dict = {}
        if user_id:
            user_clause = (
                "  AND p.trade_id IN "
                "(SELECT id FROM pm_positions WHERE user_id = :uid) "
            )
            params["uid"] = user_id
        with _get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT me.market_price_yes, p.probability, p.resolved_outcome "
                "FROM predictions p "
                "LEFT JOIN market_evaluations me ON me.prediction_id = p.id "
                "WHERE p.resolved_outcome IN (0,1) "
                "  AND p.probability IS NOT NULL "
                "  AND me.market_price_yes IS NOT NULL "
                f"{user_clause}"
            ), params).fetchall()
        n = 0
        pnl = 0.0
        cost = 0.0
        for mprice, prob, outcome in rows:
            mprice = float(mprice)
            prob = float(prob)
            outcome = int(outcome)
            if prob == 0.5:
                continue
            entry = mprice if prob > 0.5 else (1.0 - mprice)
            if entry <= 0 or entry >= 1:
                continue
            shares = _HYPOTHETICAL_STAKE_USD / entry
            side = "YES" if prob > 0.5 else "NO"
            won = (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)
            proceeds = shares if won else 0.0
            pnl += proceeds - _HYPOTHETICAL_STAKE_USD
            cost += _HYPOTHETICAL_STAKE_USD
            n += 1
        return {
            "n":        n,
            "pnl":      pnl,
            "cost":     cost,
            "roi":      _safe_div(pnl, cost),
            "stake_usd": _HYPOTHETICAL_STAKE_USD,
            "usable":   n >= MIN_BUCKET_N,
        }
    except Exception as exc:
        print(f"[diagnostics] theoretical_optimal_roi failed: {exc}",
              file=sys.stderr)
        return {"n": 0, "pnl": 0.0, "cost": 0.0, "roi": None,
                "stake_usd": _HYPOTHETICAL_STAKE_USD, "usable": False}


# ── Public: archetype P&L attribution ───────────────────────────────────────
def archetype_pnl_attribution(user_id: Optional[str] = None) -> list[dict]:
    """Realised P&L and trade count per archetype (category)."""
    return _archetype_pnl_attribution_impl(user_id, _cache_bucket())


@lru_cache(maxsize=32)
def _archetype_pnl_attribution_impl(user_id: Optional[str],
                                    _bucket: int) -> list[dict]:
    try:
        from sqlalchemy import text
        user_clause = "  AND user_id = :uid " if user_id else ""
        params: dict = {"uid": user_id} if user_id else {}
        with _get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT category, "
                "       COUNT(*) AS n, "
                "       COALESCE(SUM(realized_pnl_usd),0) AS pnl, "
                "       COALESCE(SUM(cost_usd),0) AS cost, "
                "       SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS wins "
                "FROM pm_positions "
                "WHERE status IN ('settled','invalid') "
                "  AND realized_pnl_usd IS NOT NULL "
                f"{user_clause}"
                "GROUP BY category ORDER BY n DESC"
            ), params).fetchall()
        out = []
        for cat, n, pnl, cost, wins in rows:
            n = int(n or 0)
            pnl = float(pnl or 0.0)
            cost = float(cost or 0.0)
            out.append({
                "archetype": cat or "other",
                "n":         n,
                "pnl":       pnl,
                "cost":      cost,
                "wins":      int(wins or 0),
                "win_rate":  _safe_div(int(wins or 0), n),
                "roi":       _safe_div(pnl, cost),
                "usable":    n >= MIN_BUCKET_N,
            })
        return out
    except Exception as exc:
        print(f"[diagnostics] archetype_pnl_attribution failed: {exc}",
              file=sys.stderr)
        return []


# ── Public: full report (packaged for the dashboard + learning cadence) ─────
def full_report(scope: Scope = "all",
                user_id: Optional[str] = None) -> dict:
    """
    Bundle every metric into one dict. Used by /api/diagnostics.

    When `user_id` is provided, metrics that can be user-scoped are filtered
    to that user. Forecaster-level metrics (calibration, brier) read from the
    shared `predictions` table and remain global - they are admin-only and the
    user-facing dashboard should route around them.
    """
    return {
        "scope":           scope,
        "generated_at":    time.time(),
        "cache_ttl_s":     _CACHE_TTL_SECONDS,
        "forecaster": {
            "calibration_curve":    calibration_curve(scope),
            "brier":                brier_score(scope),
            "log_score":            log_score(scope),
            "brier_by_archetype":   brier_by_archetype(scope),
            "brier_by_horizon":     brier_by_horizon(scope),
        },
        "sizer": {
            "selection_quality":    selection_quality(user_id=user_id),
            "roi_by_ev_bucket":     roi_by_ev_bucket(user_id=user_id),
            "cost_validation":      cost_validation(user_id=user_id),
            "theoretical_optimal":  theoretical_optimal_roi(user_id=user_id),
            "archetype_attribution": archetype_pnl_attribution(user_id=user_id),
        },
        "system": {
            "bankroll_series":      bankroll_series("daily",
                                                    user_id=user_id),
        },
    }
