"""
Analytics engine — comprehensive performance analytics for the Polymarket bot.

Provides rolling performance summaries, P&L attribution by multiple dimensions,
time-series data for charting, benchmark comparisons, best/worst trade lists,
and streak analysis.  All queries run against the pm_positions table via
SQLAlchemy text() on the shared PostgreSQL engine.

Design principles:
  * No numpy/pandas — stdlib math + statistics only.
  * Every public method swallows exceptions and returns safe defaults so the
    API layer never crashes.
  * The `days` parameter filters on settled_at (last N days). None = all-time.
  * All monetary values are in USD.
"""

from __future__ import annotations

import math
import statistics
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text

import config
from db.engine import get_engine


# ── Helpers ─────────────────────────────────────────────────────────────────

def _safe_div(a: float, b: float, default: Optional[float] = None) -> Optional[float]:
    """Division that returns `default` when divisor is zero."""
    if b == 0:
        return default
    return a / b


def _annualised_sharpe(daily_returns: list[float]) -> Optional[float]:
    """Annualised Sharpe ratio from a daily P&L series (risk-free = 0)."""
    if len(daily_returns) < 5:
        return None
    try:
        mu = statistics.mean(daily_returns)
        sigma = statistics.stdev(daily_returns)
        if sigma == 0:
            return None
        return (mu / sigma) * math.sqrt(365)
    except Exception:
        return None


def _annualised_sortino(daily_returns: list[float]) -> Optional[float]:
    """Annualised Sortino ratio (downside deviation only, target = 0)."""
    if len(daily_returns) < 5:
        return None
    try:
        mu = statistics.mean(daily_returns)
        downsides = [r for r in daily_returns if r < 0]
        if len(downsides) < 2:
            return None
        downside_dev = math.sqrt(statistics.mean([d ** 2 for d in downsides]))
        if downside_dev == 0:
            return None
        return (mu / downside_dev) * math.sqrt(365)
    except Exception:
        return None


def _max_drawdown(cumulative_pnl: list[float]) -> Optional[float]:
    """Peak-to-trough drawdown as a fraction of peak equity."""
    if not cumulative_pnl:
        return None
    starting = float(getattr(config, "PM_SHADOW_STARTING_CASH", 1000.0))
    peak = starting
    worst_dd = 0.0
    for cum in cumulative_pnl:
        equity = starting + cum
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > worst_dd:
            worst_dd = dd
    return worst_dd if worst_dd > 0 else None


def _date_filter(days: Optional[int], col: str = "settled_at") -> str:
    """Return a SQL fragment filtering `col` to the last N days, or empty."""
    if days is None or days <= 0:
        return ""
    return f" AND {col} >= NOW() - INTERVAL '{int(days)} days'"


# ── Main class ──────────────────────────────────────────────────────────────

class AnalyticsEngine:
    """
    Read-only analytics over pm_positions.  Every method accepts a `mode`
    ('shadow' or 'live') and an optional `days` window.
    """

    # ── 1. Performance summary ──────────────────────────────────────────────

    def get_performance_summary(
        self,
        mode: str = "shadow",
        days: Optional[int] = None,
    ) -> dict:
        """
        Rolling performance over configurable windows.

        Returns total P&L, win rate, total trades, Sharpe, Sortino,
        max drawdown, profit factor, avg win/loss, expectancy, and
        Kelly edge estimate from realised results.
        """
        try:
            date_filt = _date_filter(days)
            with get_engine().begin() as conn:
                # ── Aggregate stats ─────────────────────────────────────────
                row = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) AS total, "
                    "  COALESCE(SUM(realized_pnl_usd), 0) AS total_pnl, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd <= 0) AS losses, "
                    "  COALESCE(SUM(realized_pnl_usd) FILTER (WHERE realized_pnl_usd > 0), 0) AS gross_wins, "
                    "  COALESCE(SUM(ABS(realized_pnl_usd)) FILTER (WHERE realized_pnl_usd <= 0), 0) AS gross_losses, "
                    "  COALESCE(AVG(realized_pnl_usd) FILTER (WHERE realized_pnl_usd > 0), 0) AS avg_win, "
                    "  COALESCE(AVG(realized_pnl_usd) FILTER (WHERE realized_pnl_usd <= 0), 0) AS avg_loss "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status IN ('settled', 'invalid')"
                    + date_filt
                ), {"m": mode}).fetchone()

                total      = int(row[0] or 0)
                total_pnl  = float(row[1] or 0)
                wins       = int(row[2] or 0)
                losses     = int(row[3] or 0)
                gross_wins = float(row[4] or 0)
                gross_losses = float(row[5] or 0)
                avg_win    = float(row[6] or 0)
                avg_loss   = float(row[7] or 0)

                win_rate = _safe_div(wins, total)
                profit_factor = _safe_div(gross_wins, gross_losses)
                wl_ratio = _safe_div(abs(avg_win), abs(avg_loss)) if avg_loss != 0 else None
                expectancy = _safe_div(total_pnl, total)

                # Kelly edge from realised win rate + avg win/loss
                kelly_edge = None
                if win_rate is not None and wl_ratio is not None and wl_ratio > 0:
                    # Kelly% = W - (1-W)/R
                    kelly_edge = win_rate - (1 - win_rate) / wl_ratio

                # ── Daily P&L series for Sharpe/Sortino/Drawdown ────────────
                daily_rows = conn.execute(text(
                    "SELECT DATE(settled_at) AS d, SUM(realized_pnl_usd) AS dpnl "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status IN ('settled', 'invalid') "
                    "  AND settled_at IS NOT NULL"
                    + date_filt +
                    " GROUP BY DATE(settled_at) "
                    "ORDER BY d"
                ), {"m": mode}).fetchall()

                daily_returns = [float(r[1]) for r in daily_rows]
                cum_pnl = []
                running = 0.0
                for d in daily_returns:
                    running += d
                    cum_pnl.append(running)

                sharpe  = _annualised_sharpe(daily_returns)
                sortino = _annualised_sortino(daily_returns)
                max_dd  = _max_drawdown(cum_pnl)

            return {
                "days":           days,
                "total_trades":   total,
                "total_pnl":      round(total_pnl, 2),
                "win_rate":       round(win_rate, 4) if win_rate is not None else None,
                "wins":           wins,
                "losses":         losses,
                "sharpe":         round(sharpe, 2) if sharpe is not None else None,
                "sortino":        round(sortino, 2) if sortino is not None else None,
                "max_drawdown":   round(max_dd, 4) if max_dd is not None else None,
                "profit_factor":  round(profit_factor, 2) if profit_factor is not None else None,
                "avg_win":        round(avg_win, 2),
                "avg_loss":       round(avg_loss, 2),
                "win_loss_ratio": round(wl_ratio, 2) if wl_ratio is not None else None,
                "expectancy":     round(expectancy, 2) if expectancy is not None else None,
                "kelly_edge":     round(kelly_edge, 4) if kelly_edge is not None else None,
                "gross_wins":     round(gross_wins, 2),
                "gross_losses":   round(gross_losses, 2),
            }
        except Exception as exc:
            print(f"[analytics] get_performance_summary failed: {exc}", file=sys.stderr)
            return {
                "days": days, "total_trades": 0, "total_pnl": 0.0,
                "win_rate": None, "wins": 0, "losses": 0,
                "sharpe": None, "sortino": None, "max_drawdown": None,
                "profit_factor": None, "avg_win": 0.0, "avg_loss": 0.0,
                "win_loss_ratio": None, "expectancy": None,
                "kelly_edge": None, "gross_wins": 0.0, "gross_losses": 0.0,
            }

    # ── 2. P&L attribution ─────────────────────────────────────────────────

    def get_pnl_attribution(
        self,
        mode: str = "shadow",
        days: Optional[int] = None,
    ) -> dict:
        """
        P&L broken down by archetype, side, edge bucket, confidence bucket,
        entry price zone, day of week, and hour of day.
        """
        try:
            date_filt = _date_filter(days)
            base_where = (
                "WHERE mode = :m AND status IN ('settled', 'invalid')"
                + date_filt
            )
            params = {"m": mode}

            with get_engine().begin() as conn:
                # ── By archetype ────────────────────────────────────────────
                arch_rows = conn.execute(text(
                    "SELECT COALESCE(market_archetype, 'unknown') AS arch, "
                    "  SUM(realized_pnl_usd) AS pnl, "
                    "  COUNT(*) AS cnt, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins, "
                    "  AVG(edge_bps) AS avg_edge "
                    "FROM pm_positions " + base_where +
                    " GROUP BY arch ORDER BY pnl DESC"
                ), params).fetchall()
                by_archetype = [{
                    "archetype": r[0],
                    "pnl":       round(float(r[1] or 0), 2),
                    "count":     int(r[2] or 0),
                    "win_rate":  round(int(r[3] or 0) / int(r[2]), 4) if int(r[2] or 0) > 0 else None,
                    "avg_edge":  round(float(r[4] or 0), 0),
                } for r in arch_rows]

                # ── By side ─────────────────────────────────────────────────
                side_rows = conn.execute(text(
                    "SELECT side, "
                    "  SUM(realized_pnl_usd) AS pnl, "
                    "  COUNT(*) AS cnt, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins "
                    "FROM pm_positions " + base_where +
                    " GROUP BY side ORDER BY pnl DESC"
                ), params).fetchall()
                by_side = [{
                    "side":     r[0],
                    "pnl":      round(float(r[1] or 0), 2),
                    "count":    int(r[2] or 0),
                    "win_rate": round(int(r[3] or 0) / int(r[2]), 4) if int(r[2] or 0) > 0 else None,
                } for r in side_rows]

                # ── By edge bucket ──────────────────────────────────────────
                edge_buckets = [
                    ("0-300",   0,    300),
                    ("300-600", 300,  600),
                    ("600-1000", 600, 1000),
                    ("1000+",  1000, 100000),
                ]
                by_edge = []
                for label, lo, hi in edge_buckets:
                    eb = conn.execute(text(
                        "SELECT SUM(realized_pnl_usd), COUNT(*), "
                        "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) "
                        "FROM pm_positions " + base_where +
                        " AND edge_bps >= :lo AND edge_bps < :hi"
                    ), {**params, "lo": lo, "hi": hi}).fetchone()
                    cnt = int(eb[1] or 0)
                    by_edge.append({
                        "bucket":   label,
                        "pnl":      round(float(eb[0] or 0), 2),
                        "count":    cnt,
                        "win_rate": round(int(eb[2] or 0) / cnt, 4) if cnt > 0 else None,
                    })

                # ── By confidence bucket ────────────────────────────────────
                conf_buckets = [
                    ("0-0.3",   0,    0.3),
                    ("0.3-0.5", 0.3,  0.5),
                    ("0.5-0.7", 0.5,  0.7),
                    ("0.7+",    0.7,  1.01),
                ]
                by_confidence = []
                for label, lo, hi in conf_buckets:
                    cb = conn.execute(text(
                        "SELECT SUM(realized_pnl_usd), COUNT(*), "
                        "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) "
                        "FROM pm_positions " + base_where +
                        " AND confidence >= :lo AND confidence < :hi"
                    ), {**params, "lo": lo, "hi": hi}).fetchone()
                    cnt = int(cb[1] or 0)
                    by_confidence.append({
                        "bucket":   label,
                        "pnl":      round(float(cb[0] or 0), 2),
                        "count":    cnt,
                        "win_rate": round(int(cb[2] or 0) / cnt, 4) if cnt > 0 else None,
                    })

                # ── By entry price zone ─────────────────────────────────────
                price_zones = [
                    ("0-0.20",    0,    0.20),
                    ("0.20-0.40", 0.20, 0.40),
                    ("0.40-0.60", 0.40, 0.60),
                    ("0.60-0.80", 0.60, 0.80),
                    ("0.80-1.0",  0.80, 1.01),
                ]
                by_entry_price = []
                for label, lo, hi in price_zones:
                    pz = conn.execute(text(
                        "SELECT SUM(realized_pnl_usd), COUNT(*), "
                        "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) "
                        "FROM pm_positions " + base_where +
                        " AND entry_price >= :lo AND entry_price < :hi"
                    ), {**params, "lo": lo, "hi": hi}).fetchone()
                    cnt = int(pz[1] or 0)
                    by_entry_price.append({
                        "zone":     label,
                        "pnl":      round(float(pz[0] or 0), 2),
                        "count":    cnt,
                        "win_rate": round(int(pz[2] or 0) / cnt, 4) if cnt > 0 else None,
                    })

                # ── By day of week ──────────────────────────────────────────
                dow_rows = conn.execute(text(
                    "SELECT EXTRACT(DOW FROM settled_at)::int AS dow, "
                    "  SUM(realized_pnl_usd) AS pnl, "
                    "  COUNT(*) AS cnt, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins "
                    "FROM pm_positions " + base_where +
                    " AND settled_at IS NOT NULL "
                    "GROUP BY dow ORDER BY dow"
                ), params).fetchall()
                day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                by_day_of_week = [{
                    "day":      day_names[int(r[0])] if 0 <= int(r[0]) <= 6 else str(r[0]),
                    "dow":      int(r[0]),
                    "pnl":      round(float(r[1] or 0), 2),
                    "count":    int(r[2] or 0),
                    "win_rate": round(int(r[3] or 0) / int(r[2]), 4) if int(r[2] or 0) > 0 else None,
                } for r in dow_rows]

                # ── By hour of day (UTC) ────────────────────────────────────
                hour_rows = conn.execute(text(
                    "SELECT EXTRACT(HOUR FROM settled_at)::int AS hr, "
                    "  SUM(realized_pnl_usd) AS pnl, "
                    "  COUNT(*) AS cnt, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins "
                    "FROM pm_positions " + base_where +
                    " AND settled_at IS NOT NULL "
                    "GROUP BY hr ORDER BY hr"
                ), params).fetchall()
                by_hour = [{
                    "hour":     int(r[0]),
                    "pnl":      round(float(r[1] or 0), 2),
                    "count":    int(r[2] or 0),
                    "win_rate": round(int(r[3] or 0) / int(r[2]), 4) if int(r[2] or 0) > 0 else None,
                } for r in hour_rows]

            return {
                "days":            days,
                "by_archetype":    by_archetype,
                "by_side":         by_side,
                "by_edge":         by_edge,
                "by_confidence":   by_confidence,
                "by_entry_price":  by_entry_price,
                "by_day_of_week":  by_day_of_week,
                "by_hour":         by_hour,
            }
        except Exception as exc:
            print(f"[analytics] get_pnl_attribution failed: {exc}", file=sys.stderr)
            return {
                "days": days,
                "by_archetype": [], "by_side": [], "by_edge": [],
                "by_confidence": [], "by_entry_price": [],
                "by_day_of_week": [], "by_hour": [],
            }

    # ── 3. Rolling stats (time series) ──────────────────────────────────────

    def get_rolling_stats(
        self,
        mode: str = "shadow",
        window_days: int = 7,
    ) -> list[dict]:
        """
        Daily time-series for charting: date, daily P&L, cumulative P&L,
        daily trades, daily win rate, rolling Sharpe, rolling Brier, bankroll.
        One row per day for the last `window_days` days.
        """
        try:
            starting = float(getattr(config, "PM_SHADOW_STARTING_CASH", 1000.0))
            if mode == "live":
                starting = float(getattr(config, "PM_LIVE_STARTING_CASH", 200.0))

            with get_engine().begin() as conn:
                # All settled positions up to now, ordered by date, so we can
                # compute cumulative and rolling stats.
                rows = conn.execute(text(
                    "SELECT DATE(settled_at) AS d, "
                    "  SUM(realized_pnl_usd) AS dpnl, "
                    "  COUNT(*) AS cnt, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status IN ('settled', 'invalid') "
                    "  AND settled_at IS NOT NULL "
                    "GROUP BY DATE(settled_at) "
                    "ORDER BY d"
                ), {"m": mode}).fetchall()

                # Brier per day — from predictions table
                brier_rows = conn.execute(text(
                    "SELECT DATE(resolved_at) AS d, "
                    "  AVG((probability - resolved_outcome)^2) AS brier "
                    "FROM predictions "
                    "WHERE source = 'polymarket' "
                    "  AND resolved_at IS NOT NULL "
                    "  AND resolved_outcome IS NOT NULL "
                    "GROUP BY DATE(resolved_at) "
                    "ORDER BY d"
                ), {}).fetchall()

            # Build lookup: date -> brier
            brier_by_date: dict[str, float] = {}
            for br in brier_rows:
                if br[0] is not None and br[1] is not None:
                    brier_by_date[str(br[0])] = float(br[1])

            if not rows:
                return []

            # Build full daily series
            all_days: list[dict] = []
            cum_pnl = 0.0
            all_daily_pnls: list[float] = []

            for r in rows:
                d_str = str(r[0])
                dpnl = float(r[1] or 0)
                cnt = int(r[2] or 0)
                wins = int(r[3] or 0)
                cum_pnl += dpnl
                all_daily_pnls.append(dpnl)

                # Rolling Sharpe over last 30 settled days (or all if fewer)
                rolling_sharpe = _annualised_sharpe(all_daily_pnls[-30:])

                # Rolling Brier: running average of daily Brier scores
                # (approximate — we use the daily brier if available)
                daily_brier = brier_by_date.get(d_str)

                all_days.append({
                    "date":           d_str,
                    "daily_pnl":      round(dpnl, 2),
                    "cumulative_pnl": round(cum_pnl, 2),
                    "daily_trades":   cnt,
                    "daily_win_rate": round(wins / cnt, 4) if cnt > 0 else None,
                    "rolling_sharpe": round(rolling_sharpe, 2) if rolling_sharpe is not None else None,
                    "rolling_brier":  round(daily_brier, 4) if daily_brier is not None else None,
                    "bankroll":       round(starting + cum_pnl, 2),
                })

            # Return only the last `window_days` entries
            return all_days[-window_days:]

        except Exception as exc:
            print(f"[analytics] get_rolling_stats failed: {exc}", file=sys.stderr)
            return []

    # ── 4. Benchmark comparison ─────────────────────────────────────────────

    def get_benchmark_comparison(self, mode: str = "shadow") -> dict:
        """
        Compare bot performance vs simple benchmarks:
        - "Always YES at market price" — buy YES at observed price on every market
        - "Always NO at market price" — buy NO at observed price on every market
        - "Random 50/50" expected P&L — just sum the vig/spread cost
        - Bot's actual P&L
        - Alpha = bot P&L - best benchmark P&L
        """
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text(
                    "SELECT side, entry_price, cost_usd, shares, "
                    "       settlement_outcome, realized_pnl_usd "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status IN ('settled', 'invalid') "
                    "  AND settlement_outcome IS NOT NULL"
                ), {"m": mode}).fetchall()

            if not rows:
                return {
                    "bot_pnl":         0.0,
                    "always_yes_pnl":  0.0,
                    "always_no_pnl":   0.0,
                    "random_pnl":      0.0,
                    "alpha":           0.0,
                    "total_trades":    0,
                }

            bot_pnl = 0.0
            always_yes_pnl = 0.0
            always_no_pnl = 0.0
            random_pnl = 0.0

            for r in rows:
                side = str(r[0])
                entry_price = float(r[1])
                cost_usd = float(r[2])
                shares = float(r[3])
                outcome = str(r[4]).upper()
                pnl = float(r[5] or 0)

                bot_pnl += pnl

                # For benchmarks, we use the same cost/shares but pretend
                # we always chose one side.

                # "Always YES" benchmark: buy YES at the entry_price the
                # bot used for this trade. If YES won, profit = shares - cost.
                # If NO won, loss = -cost.
                if outcome == "YES":
                    always_yes_pnl += (shares - cost_usd)
                    always_no_pnl += (-cost_usd)
                elif outcome == "NO":
                    always_yes_pnl += (-cost_usd)
                    always_no_pnl += (shares - cost_usd)
                else:
                    # INVALID — roughly break even
                    always_yes_pnl += (shares * 0.5 - cost_usd)
                    always_no_pnl += (shares * 0.5 - cost_usd)

                # "Random 50/50" — expected value is the vig cost.
                # E[PnL] per trade = 0.5*(shares - cost) + 0.5*(-cost)
                #                  = 0.5*shares - cost
                random_pnl += (0.5 * shares - cost_usd)

            best_benchmark = max(always_yes_pnl, always_no_pnl, random_pnl)
            alpha = bot_pnl - best_benchmark

            return {
                "bot_pnl":        round(bot_pnl, 2),
                "always_yes_pnl": round(always_yes_pnl, 2),
                "always_no_pnl":  round(always_no_pnl, 2),
                "random_pnl":     round(random_pnl, 2),
                "alpha":          round(alpha, 2),
                "total_trades":   len(rows),
            }
        except Exception as exc:
            print(f"[analytics] get_benchmark_comparison failed: {exc}", file=sys.stderr)
            return {
                "bot_pnl": 0.0, "always_yes_pnl": 0.0,
                "always_no_pnl": 0.0, "random_pnl": 0.0,
                "alpha": 0.0, "total_trades": 0,
            }

    # ── 5. Worst trades ─────────────────────────────────────────────────────

    def get_worst_trades(
        self,
        mode: str = "shadow",
        limit: int = 10,
    ) -> list[dict]:
        """Top N worst trades by realised P&L, with full details."""
        return self._get_extreme_trades(mode, limit, ascending=True)

    # ── 6. Best trades ──────────────────────────────────────────────────────

    def get_best_trades(
        self,
        mode: str = "shadow",
        limit: int = 10,
    ) -> list[dict]:
        """Top N best trades by realised P&L, with full details."""
        return self._get_extreme_trades(mode, limit, ascending=False)

    def _get_extreme_trades(
        self,
        mode: str,
        limit: int,
        ascending: bool,
    ) -> list[dict]:
        """Shared implementation for best/worst trade lookups."""
        order = "ASC" if ascending else "DESC"
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text(
                    "SELECT id, question, side, entry_price, cost_usd, shares, "
                    "       edge_bps, confidence, market_archetype, "
                    "       realized_pnl_usd, settlement_outcome, "
                    "       created_at, settled_at, reasoning, "
                    "       claude_probability "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status IN ('settled', 'invalid') "
                    f"ORDER BY realized_pnl_usd {order} NULLS LAST "
                    "LIMIT :lim"
                ), {"m": mode, "lim": limit}).fetchall()

            return [{
                "id":               r[0],
                "question":         r[1],
                "side":             r[2],
                "entry_price":      round(float(r[3]), 3) if r[3] is not None else None,
                "cost_usd":         round(float(r[4]), 2) if r[4] is not None else None,
                "shares":           round(float(r[5]), 2) if r[5] is not None else None,
                "edge_bps":         round(float(r[6]), 0) if r[6] is not None else None,
                "confidence":       round(float(r[7]), 2) if r[7] is not None else None,
                "archetype":        r[8],
                "realized_pnl":     round(float(r[9]), 2) if r[9] is not None else None,
                "settlement_outcome": r[10],
                "opened_at":        r[11].isoformat() if r[11] else None,
                "settled_at":       r[12].isoformat() if r[12] else None,
                "reasoning_snippet": (str(r[13])[:300] + "...") if r[13] and len(str(r[13])) > 300 else r[13],
                "claude_probability": round(float(r[14]), 3) if r[14] is not None else None,
            } for r in rows]
        except Exception as exc:
            print(f"[analytics] _get_extreme_trades failed: {exc}", file=sys.stderr)
            return []

    # ── 7. Streak analysis ──────────────────────────────────────────────────

    def get_streak_analysis(self, mode: str = "shadow") -> dict:
        """
        Current and historical streaks.

        Returns current streak (type + length), longest win streak,
        longest loss streak, and average streak length.
        """
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text(
                    "SELECT realized_pnl_usd "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status IN ('settled', 'invalid') "
                    "ORDER BY settled_at ASC NULLS LAST, id ASC"
                ), {"m": mode}).fetchall()

            if not rows:
                return {
                    "current_streak_type":   None,
                    "current_streak_length": 0,
                    "longest_win_streak":    0,
                    "longest_loss_streak":   0,
                    "avg_streak_length":     None,
                    "total_streaks":         0,
                }

            # Walk through trades, building streaks
            streaks: list[tuple[str, int]] = []  # (type, length)
            current_type: Optional[str] = None
            current_len = 0

            for r in rows:
                pnl = float(r[0] or 0)
                result = "win" if pnl > 0 else "loss"

                if result == current_type:
                    current_len += 1
                else:
                    if current_type is not None:
                        streaks.append((current_type, current_len))
                    current_type = result
                    current_len = 1

            # Don't forget the final streak
            if current_type is not None:
                streaks.append((current_type, current_len))

            longest_win = max(
                (length for typ, length in streaks if typ == "win"),
                default=0
            )
            longest_loss = max(
                (length for typ, length in streaks if typ == "loss"),
                default=0
            )
            avg_streak = (
                statistics.mean([length for _, length in streaks])
                if streaks else None
            )

            return {
                "current_streak_type":   current_type,
                "current_streak_length": current_len,
                "longest_win_streak":    longest_win,
                "longest_loss_streak":   longest_loss,
                "avg_streak_length":     round(avg_streak, 1) if avg_streak is not None else None,
                "total_streaks":         len(streaks),
            }
        except Exception as exc:
            print(f"[analytics] get_streak_analysis failed: {exc}", file=sys.stderr)
            return {
                "current_streak_type": None, "current_streak_length": 0,
                "longest_win_streak": 0, "longest_loss_streak": 0,
                "avg_streak_length": None, "total_streaks": 0,
            }
