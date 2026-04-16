"""
Self-Improvement Analyser — analyses bot performance and proposes config changes
via Telegram, requiring explicit /apply approval before touching config.py.

The bot NEVER modifies config.py autonomously. Every change requires:
  1. analyse_and_report() runs (scheduled weekly or triggered manually).
  2. User reviews the Telegram message.
  3. User replies /apply (or /skip).
  4. Only then does apply_suggestions() write to config.py.

This is a human-in-the-loop system, not auto-optimisation.

Extended capabilities (post-M7):
  - _take_performance_snapshot(): weekly/monthly/quarterly snapshots
  - _evaluate_past_suggestions(): did the last changes help?
  - config_change_history: full audit trail of every /apply
  - generate_monthly_report(): deep monthly analysis with grade
"""

import importlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import create_engine, text

import config
from engine.memory import MemoryManager


class SelfImprovementAnalyser:
    """
    Collects 30-day performance data, sends it to Claude for analysis,
    and forwards structured suggestions to Telegram for human approval.
    """

    def __init__(self, notifier) -> None:
        self._notifier = notifier
        self.pending_suggestions: Optional[dict] = None
        self._memory    = MemoryManager()
        self._strategist = None

    def set_strategist(self, strategist) -> None:
        """Wire in Strategist so the weekly run can update Obsidian strategy memory."""
        self._strategist = strategist

    # ── Main entry point ──────────────────────────────────────────────────────

    async def analyse_and_report(self) -> None:
        """
        Full pipeline: snapshot → evaluate past → collect → analyse →
        parse → strategy-switching → store → send.
        Never raises — all errors are logged.
        """
        try:
            print("[self_improvement] Starting weekly analysis…", flush=True)

            # Take weekly snapshot before analysis
            await self._take_performance_snapshot("weekly")

            # Evaluate whether previous config changes helped
            outcome_assessment = await self._evaluate_past_suggestions()

            data = await self._collect_data()

            # Inject outcome context so Claude has memory of past changes
            if outcome_assessment:
                data["outcome_assessment"] = outcome_assessment

            # Step 3.5: Obsidian trade insights (qualitative patterns)
            insights = await self._generate_trade_insights()
            if insights and not insights.startswith("No trades"):
                data["trade_insights"] = insights

            # Step 3.6: Quantitative performance attribution (measured outcomes)
            attribution = await self._compute_performance_attribution(days=30)
            attribution_text = self._format_attribution(attribution)
            data["attribution"] = attribution_text

            raw = await self._get_suggestions(data)
            self._parse_suggestions(raw)

            # ── Strategy switching detection ──────────────────────────────────
            perf = await self._detect_strategy_performance()
            switching = self._get_switching_suggestion(perf)
            if switching is not None and self.pending_suggestions is not None:
                existing = self.pending_suggestions.get("suggestions", [])
                # Only add if ADX_TREND_THRESHOLD not already in Claude's suggestions
                already_suggested = any(
                    s.get("param") == "ADX_TREND_THRESHOLD" for s in existing
                )
                if not already_suggested and len(existing) < 4:
                    existing.append(switching)
                    self.pending_suggestions["suggestions"] = existing

            # Write historical record (two rows: trend + range)
            await self._write_strategy_performance(perf)

            await self._send_suggestion_message()

            # Step 4.5: Send attribution breakdown as a separate Telegram message
            if self._notifier.enabled:
                try:
                    await self._send_attribution_message(attribution)
                except Exception as exc:
                    print(
                        f"[self_improvement] attribution send error: {exc}",
                        file=sys.stderr,
                    )

            # Step 5 (new): Update Obsidian strategy memory via Strategist
            if self._strategist is not None:
                try:
                    await self._strategist.update_strategy_memory()
                except Exception as exc:
                    print(
                        f"[self_improvement] strategy memory update error: {exc}",
                        file=sys.stderr,
                    )

            # Step 6 (new): Append strategy memory summary to Telegram
            if self._notifier.enabled:
                try:
                    current_thesis = (
                        self._memory.read_strategy_memory().get("current_thesis", "")
                        or "No thesis yet — still learning"
                    )
                    await self._notifier.send(
                        f"🧠 <b>STRATEGY MEMORY UPDATED</b>\n"
                        f"Claude has reviewed the last 14 days of trades and updated "
                        f"its strategy memory. Current thesis:\n\n"
                        f"<i>{current_thesis[:600]}</i>\n\n"
                        f"This is what Claude will use for decisions this week."
                    )
                except Exception as exc:
                    print(
                        f"[self_improvement] memory summary send error: {exc}",
                        file=sys.stderr,
                    )

            print("[self_improvement] Analysis complete and sent.", flush=True)
        except Exception as exc:
            print(
                f"[self_improvement] analyse_and_report error: {exc}",
                file=sys.stderr,
            )

    # ── Performance snapshot ──────────────────────────────────────────────────

    async def _take_performance_snapshot(self, snapshot_type: str = "weekly") -> None:
        """
        Query trade stats, compute Sharpe + drawdown, snapshot config,
        and write a row to performance_snapshots.

        snapshot_type: 'weekly' (7 days) | 'monthly' (30 days) | 'quarterly' (90 days)
        """
        days_map = {"weekly": 7, "monthly": 30, "quarterly": 90}
        days = days_map.get(snapshot_type, 7)

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return

        try:
            from db.models import performance_snapshots

            engine = create_engine(database_url)
            paper_p = {"paper": config.PAPER_MODE}

            with engine.begin() as conn:
                # 1. Trade stats for the period
                overall = conn.execute(
                    text(
                        f"""
                        SELECT COUNT(*)                                         AS total_trades,
                               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)   AS wins,
                               AVG(pnl_usd)                                     AS avg_pnl,
                               SUM(pnl_usd)                                     AS total_pnl
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '{days} days'
                          AND timestamp_close IS NOT NULL
                        """
                    ),
                    paper_p,
                ).fetchone()

                total_trades = int(overall._mapping["total_trades"] or 0)
                wins = int(overall._mapping["wins"] or 0)
                win_rate = (wins / total_trades) if total_trades > 0 else 0.0
                total_pnl = float(overall._mapping["total_pnl"] or 0.0)
                avg_pnl_per_trade = float(overall._mapping["avg_pnl"] or 0.0)

                # 2. Daily P&L series for Sharpe ratio and max drawdown
                daily_rows = conn.execute(
                    text(
                        f"""
                        SELECT pnl_usd FROM daily_pnl
                        WHERE date >= CURRENT_DATE - INTERVAL '{days} days'
                          AND paper = :paper
                        ORDER BY date
                        """
                    ),
                    paper_p,
                ).fetchall()

                daily_values = [float(r._mapping["pnl_usd"]) for r in daily_rows]

                if len(daily_values) > 1:
                    mean_v = sum(daily_values) / len(daily_values)
                    variance = sum((x - mean_v) ** 2 for x in daily_values) / (
                        len(daily_values) - 1
                    )
                    stddev = math.sqrt(variance) if variance > 0 else 0.0
                    sharpe = mean_v / stddev if stddev > 0 else 0.0
                else:
                    sharpe = 0.0

                # Max drawdown = min of cumulative P&L series
                cumulative = 0.0
                cum_series: list[float] = []
                for v in daily_values:
                    cumulative += v
                    cum_series.append(cumulative)
                max_drawdown = min(cum_series) if cum_series else 0.0

                # 3. Trend vs range win rates
                playbook_rows = conn.execute(
                    text(
                        f"""
                        SELECT
                            CASE
                                WHEN regime_at_entry LIKE 'TREND%' THEN 'trend'
                                WHEN regime_at_entry LIKE 'RANGE%' THEN 'range'
                                ELSE 'other'
                            END AS playbook,
                            COUNT(*)                                        AS trades,
                            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)  AS wins
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '{days} days'
                          AND timestamp_close IS NOT NULL
                        GROUP BY playbook
                        """
                    ),
                    paper_p,
                ).fetchall()

                trend_win_rate: Optional[float] = None
                range_win_rate: Optional[float] = None
                for row in playbook_rows:
                    pb = row._mapping["playbook"]
                    t = int(row._mapping["trades"] or 0)
                    w = int(row._mapping["wins"] or 0)
                    rate = w / t if t > 0 else 0.0
                    if pb == "trend":
                        trend_win_rate = rate
                    elif pb == "range":
                        range_win_rate = rate

                # 4. Dominant regime
                dom_row = conn.execute(
                    text(
                        f"""
                        SELECT regime, COUNT(*) AS count
                        FROM ticks
                        WHERE timestamp >= CURRENT_DATE - INTERVAL '{days} days'
                        GROUP BY regime
                        ORDER BY count DESC
                        LIMIT 1
                        """
                    )
                ).fetchone()
                dominant_regime = (
                    str(dom_row._mapping["regime"]) if dom_row else None
                )

                # 5. NO_TRADE percentage from ticks
                nt_row = conn.execute(
                    text(
                        f"""
                        SELECT SUM(CASE WHEN decision = 'NO_TRADE' THEN 1 ELSE 0 END)
                                   * 100.0 / NULLIF(COUNT(*), 0) AS no_trade_pct
                        FROM ticks
                        WHERE timestamp >= CURRENT_DATE - INTERVAL '{days} days'
                        """
                    )
                ).fetchone()
                no_trade_pct = (
                    float(nt_row._mapping["no_trade_pct"])
                    if nt_row and nt_row._mapping["no_trade_pct"] is not None
                    else 0.0
                )

                # 6. Config snapshot — key parameter groups
                _prefixes = (
                    "CONVICTION_", "DERIBIT_", "PORTFOLIO_",
                    "ADX_", "ATR_", "FUNDING_", "SOL_",
                )
                config_snap = {
                    k: getattr(config, k)
                    for k in sorted(dir(config))
                    if any(k.startswith(p) for p in _prefixes)
                    and not k.startswith("__")
                    and isinstance(getattr(config, k), (int, float, str, bool))
                }
                config_snapshot_str = json.dumps(config_snap, default=str)

                # Write snapshot row
                conn.execute(
                    performance_snapshots.insert().values(
                        snapshot_date=datetime.now(timezone.utc).date(),
                        snapshot_type=snapshot_type,
                        total_trades=total_trades,
                        win_rate=win_rate,
                        total_pnl=total_pnl,
                        avg_pnl_per_trade=avg_pnl_per_trade,
                        sharpe_ratio=sharpe,
                        max_drawdown=max_drawdown,
                        trend_win_rate=trend_win_rate,
                        range_win_rate=range_win_rate,
                        dominant_regime=dominant_regime,
                        no_trade_pct=no_trade_pct,
                        config_snapshot=config_snapshot_str,
                        notes=None,
                    )
                )

            print(
                f"[self_improvement] Snapshot ({snapshot_type}): "
                f"{total_trades} trades, win_rate={win_rate:.1%}, "
                f"sharpe={sharpe:.2f}, drawdown={max_drawdown:.2f}",
                flush=True,
            )

        except Exception as exc:
            print(
                f"[self_improvement] _take_performance_snapshot error: {exc}",
                file=sys.stderr,
            )

    # ── Evaluate past suggestions ─────────────────────────────────────────────

    async def _evaluate_past_suggestions(self) -> str:
        """
        Find config changes from the last 28 days that are still 'pending'.
        For each, compare average daily P&L for the 7 days before vs after
        the change date. Update outcomes in config_change_history.

        Returns a formatted summary string for inclusion in the Claude prompt.
        Returns empty string if no pending changes.
        """
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return ""

        try:
            from db.models import config_change_history  # noqa: F401

            engine = create_engine(database_url)

            with engine.begin() as conn:
                pending_rows = conn.execute(
                    text(
                        """
                        SELECT id, param_name, old_value, new_value, changed_at
                        FROM config_change_history
                        WHERE outcome = 'pending'
                          AND changed_at >= NOW() - INTERVAL '28 days'
                        ORDER BY changed_at
                        """
                    )
                ).fetchall()

                if not pending_rows:
                    return ""

                outcome_lines = ["Previous changes assessment:"]

                for row in pending_rows:
                    cch_id = row._mapping["id"]
                    param = row._mapping["param_name"] or "unknown"
                    old_val = row._mapping["old_value"] or "?"
                    new_val = row._mapping["new_value"] or "?"
                    changed_at = row._mapping["changed_at"]

                    date_str = (
                        changed_at.strftime("%Y-%m-%d")
                        if hasattr(changed_at, "strftime")
                        else str(changed_at)[:10]
                    )

                    # Count days of data available after the change
                    after_count_row = conn.execute(
                        text(
                            """
                            SELECT COUNT(*) AS cnt
                            FROM daily_pnl
                            WHERE date >= :changed_date
                              AND date <  :changed_date + INTERVAL '7 days'
                              AND paper = :paper
                            """
                        ),
                        {
                            "changed_date": changed_at,
                            "paper": config.PAPER_MODE,
                        },
                    ).fetchone()
                    after_count = int(
                        after_count_row._mapping["cnt"] or 0
                    ) if after_count_row else 0

                    if after_count < 3:
                        outcome_lines.append(
                            f"- {param} {old_val}→{new_val} ({date_str}): "
                            f"PENDING ({after_count}/3 days of post-change data)"
                        )
                        continue

                    # Avg daily P&L for 7 days BEFORE the change
                    before_row = conn.execute(
                        text(
                            """
                            SELECT AVG(pnl_usd) AS avg_pnl
                            FROM daily_pnl
                            WHERE date >= :changed_date - INTERVAL '7 days'
                              AND date <  :changed_date
                              AND paper = :paper
                            """
                        ),
                        {
                            "changed_date": changed_at,
                            "paper": config.PAPER_MODE,
                        },
                    ).fetchone()

                    # Avg daily P&L for 7 days AFTER the change
                    after_row = conn.execute(
                        text(
                            """
                            SELECT AVG(pnl_usd) AS avg_pnl
                            FROM daily_pnl
                            WHERE date >= :changed_date
                              AND date <  :changed_date + INTERVAL '7 days'
                              AND paper = :paper
                            """
                        ),
                        {
                            "changed_date": changed_at,
                            "paper": config.PAPER_MODE,
                        },
                    ).fetchone()

                    before_avg = (
                        float(before_row._mapping["avg_pnl"])
                        if before_row and before_row._mapping["avg_pnl"] is not None
                        else None
                    )
                    after_avg = (
                        float(after_row._mapping["avg_pnl"])
                        if after_row and after_row._mapping["avg_pnl"] is not None
                        else None
                    )

                    # Determine outcome
                    if before_avg is None or after_avg is None:
                        outcome = "neutral"
                        detail = "insufficient baseline data"
                    elif abs(before_avg) < 0.001:
                        # Near-zero baseline — can't compute percentage
                        outcome = "neutral"
                        detail = "near-zero baseline P&L"
                    elif after_avg > before_avg * 1.1:
                        pct_chg = (after_avg - before_avg) / abs(before_avg) * 100
                        outcome = "improved"
                        detail = f"+{pct_chg:.0f}% avg daily P&L"
                    elif after_avg < before_avg * 0.9:
                        pct_chg = (after_avg - before_avg) / abs(before_avg) * 100
                        outcome = "worsened"
                        detail = f"{pct_chg:.0f}% avg daily P&L"
                    else:
                        outcome = "neutral"
                        detail = "no significant change"

                    # Update config_change_history row
                    conn.execute(
                        text(
                            "UPDATE config_change_history SET outcome = :outcome WHERE id = :id"
                        ),
                        {"outcome": outcome, "id": cch_id},
                    )

                    outcome_lines.append(
                        f"- {param} {old_val}→{new_val} ({date_str}): "
                        f"{outcome.upper()} ({detail})"
                    )

            if len(outcome_lines) == 1:
                # Only header line — nothing to report
                return ""

            return "\n".join(outcome_lines)

        except Exception as exc:
            print(
                f"[self_improvement] _evaluate_past_suggestions error: {exc}",
                file=sys.stderr,
            )
            return ""

    # ── Monthly report ────────────────────────────────────────────────────────

    async def generate_monthly_report(self) -> None:
        """
        Called on the 1st of each month at 00:30 UTC.
        Deep analysis with performance grade, capital allocation readiness,
        config change history, and regime-aware insights.
        """
        try:
            print("[self_improvement] Starting monthly report…", flush=True)

            # Step 1: Take monthly snapshot
            await self._take_performance_snapshot("monthly")

            database_url = os.environ.get("DATABASE_URL", "")
            if not database_url:
                return

            engine = create_engine(database_url)
            paper_p = {"paper": config.PAPER_MODE}

            with engine.begin() as conn:
                # Full 30-day trade stats
                overall = conn.execute(
                    text(
                        """
                        SELECT COUNT(*)                                         AS total_trades,
                               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)   AS wins,
                               AVG(pnl_usd)                                     AS avg_pnl,
                               SUM(pnl_usd)                                     AS total_pnl,
                               MIN(pnl_usd)                                     AS worst_trade,
                               MAX(pnl_usd)                                     AS best_trade
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '30 days'
                          AND timestamp_close IS NOT NULL
                        """
                    ),
                    paper_p,
                ).fetchone()

                total_trades = int(overall._mapping["total_trades"] or 0)
                wins = int(overall._mapping["wins"] or 0)
                win_rate = (wins / total_trades) if total_trades > 0 else 0.0
                total_pnl = float(overall._mapping["total_pnl"] or 0.0)
                avg_pnl = float(overall._mapping["avg_pnl"] or 0.0)

                # Daily P&L for Sharpe + drawdown
                daily_rows = conn.execute(
                    text(
                        """
                        SELECT pnl_usd FROM daily_pnl
                        WHERE date >= CURRENT_DATE - INTERVAL '30 days'
                          AND paper = :paper
                        ORDER BY date
                        """
                    ),
                    paper_p,
                ).fetchall()
                daily_values = [float(r._mapping["pnl_usd"]) for r in daily_rows]

                if len(daily_values) > 1:
                    mean_v = sum(daily_values) / len(daily_values)
                    var = sum((x - mean_v) ** 2 for x in daily_values) / (
                        len(daily_values) - 1
                    )
                    std = math.sqrt(var) if var > 0 else 0.0
                    sharpe = mean_v / std if std > 0 else 0.0
                else:
                    sharpe = 0.0

                cum = 0.0
                cum_series: list[float] = []
                for v in daily_values:
                    cum += v
                    cum_series.append(cum)
                max_drawdown = min(cum_series) if cum_series else 0.0

                # Config changes this month with outcomes
                change_rows = conn.execute(
                    text(
                        """
                        SELECT param_name, old_value, new_value, outcome, changed_at
                        FROM config_change_history
                        WHERE changed_at >= CURRENT_DATE - INTERVAL '30 days'
                        ORDER BY changed_at
                        """
                    )
                ).fetchall()

                # Previous monthly snapshot for comparison
                prev_snapshot = conn.execute(
                    text(
                        """
                        SELECT total_trades, total_pnl, win_rate, sharpe_ratio
                        FROM performance_snapshots
                        WHERE snapshot_type = 'monthly'
                          AND snapshot_date < CURRENT_DATE - INTERVAL '25 days'
                        ORDER BY snapshot_date DESC
                        LIMIT 1
                        """
                    )
                ).fetchone()

                # Regime distribution (top 5)
                regime_rows = conn.execute(
                    text(
                        """
                        SELECT regime,
                               COUNT(*) AS count,
                               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
                        FROM ticks
                        WHERE timestamp >= CURRENT_DATE - INTERVAL '30 days'
                        GROUP BY regime
                        ORDER BY count DESC
                        LIMIT 5
                        """
                    )
                ).fetchall()

                # Trade outcomes by regime
                regime_pnl_rows = conn.execute(
                    text(
                        """
                        SELECT regime_at_entry,
                               COUNT(*)                                        AS trades,
                               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)  AS wins,
                               AVG(pnl_usd)                                    AS avg_pnl,
                               SUM(pnl_usd)                                    AS total_pnl
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '30 days'
                          AND timestamp_close IS NOT NULL
                        GROUP BY regime_at_entry
                        ORDER BY avg_pnl DESC NULLS LAST
                        """
                    ),
                    paper_p,
                ).fetchall()

                # Top 3 winners and losers
                top_winners = conn.execute(
                    text(
                        """
                        SELECT pair, regime_at_entry, pnl_usd
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '30 days'
                          AND timestamp_close IS NOT NULL
                          AND pnl_usd IS NOT NULL
                        ORDER BY pnl_usd DESC
                        LIMIT 3
                        """
                    ),
                    paper_p,
                ).fetchall()

                top_losers = conn.execute(
                    text(
                        """
                        SELECT pair, regime_at_entry, pnl_usd
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '30 days'
                          AND timestamp_close IS NOT NULL
                          AND pnl_usd IS NOT NULL
                        ORDER BY pnl_usd ASC
                        LIMIT 3
                        """
                    ),
                    paper_p,
                ).fetchall()

                # Deribit IV context: high-IV cycle rate
                iv_row = conn.execute(
                    text(
                        """
                        SELECT
                            COUNT(CASE WHEN iv >= 80 THEN 1 END)  AS high_iv_cycles,
                            COUNT(*)                               AS iv_cycles_total,
                            ROUND(COUNT(CASE WHEN iv >= 80 THEN 1 END)
                                  * 100.0 / NULLIF(COUNT(*), 0), 1) AS high_iv_pct
                        FROM ticks
                        WHERE timestamp >= CURRENT_DATE - INTERVAL '30 days'
                          AND iv IS NOT NULL
                        """
                    )
                ).fetchone()

            # Step 3: Build user prompt sections
            regime_lines = [
                f"  {r._mapping['regime']}: {r._mapping['count']} cycles ({r._mapping['pct']}%)"
                for r in regime_rows
            ]
            regime_pnl_lines = []
            for r in regime_pnl_rows:
                t = int(r._mapping["trades"] or 0)
                w = int(r._mapping["wins"] or 0)
                wr = (w / t * 100) if t > 0 else 0
                ap = float(r._mapping["avg_pnl"] or 0)
                regime_pnl_lines.append(
                    f"  {r._mapping['regime_at_entry']}: {t} trades, "
                    f"{wr:.0f}% win rate, avg {ap:+.2f} USD"
                )

            change_lines = []
            for r in change_rows:
                ca = r._mapping["changed_at"]
                ds = ca.strftime("%b %d") if hasattr(ca, "strftime") else str(ca)[:10]
                outcome = (r._mapping["outcome"] or "pending").upper()
                change_lines.append(
                    f"  {r._mapping['param_name']} {r._mapping['old_value']}→"
                    f"{r._mapping['new_value']} ({ds}): {outcome}"
                )

            winner_lines = [
                f"  {r._mapping['pair']}: {float(r._mapping['pnl_usd']):+.2f} USD "
                f"({r._mapping['regime_at_entry']})"
                for r in top_winners
            ]
            loser_lines = [
                f"  {r._mapping['pair']}: {float(r._mapping['pnl_usd']):+.2f} USD "
                f"({r._mapping['regime_at_entry']})"
                for r in top_losers
            ]

            prev_context = ""
            if prev_snapshot:
                pv_t = int(prev_snapshot._mapping["total_trades"] or 0)
                pv_pnl = float(prev_snapshot._mapping["total_pnl"] or 0)
                pv_wr = float(prev_snapshot._mapping["win_rate"] or 0)
                pv_sh = float(prev_snapshot._mapping["sharpe_ratio"] or 0)
                prev_context = (
                    f"\nPREVIOUS MONTH: {pv_t} trades, "
                    f"{pv_wr * 100:.0f}% win rate, "
                    f"{pv_pnl:+.2f} USD P&L, Sharpe {pv_sh:.2f}"
                )

            iv_context = ""
            if (
                iv_row
                and iv_row._mapping["iv_cycles_total"]
                and int(iv_row._mapping["iv_cycles_total"] or 0) > 0
            ):
                hi = int(iv_row._mapping["high_iv_cycles"] or 0)
                pct = iv_row._mapping["high_iv_pct"] or 0
                iv_context = (
                    f"\nDERIBIT IV: {hi} high-IV cycles ({pct}% of IV-enabled cycles) "
                    f"in the last 30 days."
                )

            user_prompt = (
                f"Monthly performance data:\n\n"
                f"OVERALL: {total_trades} trades, {win_rate * 100:.0f}% win rate, "
                f"{total_pnl:+.2f} USD total P&L, avg {avg_pnl:+.2f} USD/trade\n"
                f"Sharpe ratio: {sharpe:.2f} | Max drawdown: {max_drawdown:.2f} USD"
                f"{prev_context}\n\n"
                f"REGIME DISTRIBUTION:\n"
                + ("\n".join(regime_lines) if regime_lines else "  (no data)")
                + "\n\nTRADE OUTCOMES BY REGIME:\n"
                + ("\n".join(regime_pnl_lines) if regime_pnl_lines else "  (no data)")
                + "\n\nCONFIG CHANGES THIS MONTH:\n"
                + ("\n".join(change_lines) if change_lines else "  None")
                + "\n\nTOP 3 WINNERS:\n"
                + ("\n".join(winner_lines) if winner_lines else "  (no trades)")
                + "\n\nTOP 3 LOSERS:\n"
                + ("\n".join(loser_lines) if loser_lines else "  (no trades)")
                + (f"\n\n{iv_context}" if iv_context else "")
            )

            system_prompt = (
                "You are a quantitative trading performance analyst doing a monthly "
                "review. Analyse one full month of trading data and provide:\n"
                "1. A performance grade: A/B/C/D/F with reasoning\n"
                "2. The single biggest improvement opportunity\n"
                "3. Whether the bot is ready to increase capital allocation\n"
                "4. One thing the bot did exceptionally well this month\n"
                "Keep response under 400 words. Be direct and honest.\n\n"
                "Format your response EXACTLY like this:\n\n"
                "GRADE: [A/B/C/D/F]\n"
                "GRADE_REASON: [one sentence]\n\n"
                "IMPROVEMENT: [one sentence on the single biggest opportunity]\n\n"
                "CAPITAL_READY: [YES/NO/MAYBE] — [one sentence reason]\n\n"
                "DOING_WELL: [one thing done exceptionally well]"
            )

            # Step 4: Call Claude
            claude_analysis = "Analysis unavailable."
            grade = "N/A"
            try:
                import anthropic

                client = anthropic.AsyncAnthropic()
                response = await client.messages.create(
                    model=config.CLAUDE_MODEL,
                    max_tokens=600,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                claude_raw = response.content[0].text

                m = re.search(r"GRADE:\s*([A-F])", claude_raw)
                if m:
                    grade = m.group(1)
                claude_analysis = claude_raw

            except Exception as exc:
                print(
                    f"[self_improvement] monthly Claude error: {exc}",
                    file=sys.stderr,
                )

            # Step 4.5 (new): Trade pattern insights from Obsidian vault
            trade_insights = await self._generate_trade_insights()

            # Step 4.6 (new): Write full monthly review to Obsidian vault
            month_str = datetime.now(timezone.utc).strftime("%Y-%m")
            try:
                monthly_vault_content = (
                    f"# Monthly Review: {datetime.now(timezone.utc).strftime('%B %Y')}\n\n"
                    f"## Performance Data\n{user_prompt}\n\n"
                    f"## Claude's Assessment\n{claude_analysis}\n\n"
                    f"## Trading Pattern Analysis\n{trade_insights}"
                )
                self._memory.write_monthly_review(
                    date=f"{month_str}-monthly",
                    content=monthly_vault_content,
                )
                print(
                    f"[self_improvement] Monthly review written to Obsidian vault.",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[self_improvement] Obsidian monthly write error: {exc}",
                    file=sys.stderr,
                )

            # Step 5: Build and send Telegram message
            now_utc = datetime.now(timezone.utc)
            now_myt = now_utc + timedelta(hours=8)
            month_display = now_myt.strftime("%B %Y")
            mode = "PAPER" if config.PAPER_MODE else "LIVE"

            change_tg = (
                "\n".join(f"  {l}" for l in change_lines)
                if change_lines
                else "  None this month"
            )

            # Trim insights for Telegram (avoid message-too-long errors)
            insights_tg = trade_insights[:600] if trade_insights else "No trade data yet."

            msg = (
                f"📅 <b>MONTHLY PERFORMANCE REVIEW</b>\n"
                f"{month_display} | Mode: {mode}\n\n"
                f"<b>Grade: {grade}</b>\n\n"
                f"<b>📊 Key metrics:</b>\n"
                f"Trades: {total_trades} | Win rate: {win_rate * 100:.0f}%\n"
                f"Total P&amp;L: {total_pnl:+.2f} USD\n"
                f"Sharpe ratio: {sharpe:.2f}\n"
                f"Max drawdown: {max_drawdown:.2f} USD\n\n"
                f"<b>Config changes this month:</b>\n"
                f"{change_tg}\n\n"
                f"<b>Claude's assessment:</b>\n"
                f"{claude_analysis[:600]}\n\n"
                f"📖 <b>TRADING PATTERN ANALYSIS</b>\n"
                f"{insights_tg}"
            )

            if self._notifier.enabled:
                await self._notifier.send(msg)

            print(
                f"[self_improvement] Monthly report sent: "
                f"grade={grade}, trades={total_trades}, pnl={total_pnl:+.2f}",
                flush=True,
            )

        except Exception as exc:
            print(
                f"[self_improvement] generate_monthly_report error: {exc}",
                file=sys.stderr,
            )

    # ── Data collection ───────────────────────────────────────────────────────

    async def _collect_data(self) -> dict:
        """Query DB for 30-day performance stats and return as a dict."""
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise RuntimeError("DATABASE_URL not set")

        engine = create_engine(database_url)
        result: dict = {}

        with engine.begin() as conn:
            paper_p = {"paper": config.PAPER_MODE}

            # 1. Overall stats (last 30 days, closed trades only)
            overall_row = conn.execute(
                text(
                    """
                    SELECT
                        COUNT(*)                                          AS total_trades,
                        SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)    AS wins,
                        AVG(pnl_usd)                                      AS avg_pnl,
                        SUM(pnl_usd)                                      AS total_pnl,
                        MIN(pnl_usd)                                      AS worst_trade,
                        MAX(pnl_usd)                                      AS best_trade
                    FROM trades
                    WHERE paper = :paper
                      AND timestamp_open >= CURRENT_DATE - INTERVAL '30 days'
                      AND timestamp_close IS NOT NULL
                    """
                ),
                paper_p,
            ).fetchone()
            result["overall"] = dict(overall_row._mapping) if overall_row else {}

            # 2. Rejection breakdown by layer (last 30 days)
            reject_rows = conn.execute(
                text(
                    """
                    SELECT
                        CASE
                            WHEN decision_reason LIKE 'Layer A%'          THEN 'Layer A (Regime)'
                            WHEN decision_reason LIKE 'Layer B%'          THEN 'Layer B (Direction)'
                            WHEN decision_reason LIKE 'Layer C%'          THEN 'Layer C (Confirmation)'
                            WHEN decision_reason LIKE 'Layer D%'          THEN 'Layer D (Execution)'
                            WHEN decision_reason LIKE 'Layer E%'          THEN 'Layer E (Events)'
                            WHEN decision_reason LIKE 'KILL SWITCH%'      THEN 'Kill switch'
                            WHEN decision_reason LIKE 'max simultaneous%' THEN 'Max positions'
                            ELSE 'Other'
                        END AS layer,
                        COUNT(*) AS count,
                        ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
                    FROM ticks
                    WHERE timestamp >= CURRENT_DATE - INTERVAL '30 days'
                      AND decision = 'REJECT'
                    GROUP BY layer
                    ORDER BY count DESC
                    """
                )
            ).fetchall()
            result["rejections"] = [dict(r._mapping) for r in reject_rows]

            # 3. Regime distribution (last 30 days)
            regime_dist_rows = conn.execute(
                text(
                    """
                    SELECT regime,
                           COUNT(*) AS count,
                           ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
                    FROM ticks
                    WHERE timestamp >= CURRENT_DATE - INTERVAL '30 days'
                    GROUP BY regime
                    ORDER BY count DESC
                    """
                )
            ).fetchall()
            result["regime_dist"] = [dict(r._mapping) for r in regime_dist_rows]

            # 4. Trade outcomes by regime
            regime_trade_rows = conn.execute(
                text(
                    """
                    SELECT regime_at_entry,
                           COUNT(*) AS trades,
                           SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                           AVG(pnl_usd) AS avg_pnl
                    FROM trades
                    WHERE paper = :paper
                      AND timestamp_open >= CURRENT_DATE - INTERVAL '30 days'
                      AND timestamp_close IS NOT NULL
                    GROUP BY regime_at_entry
                    ORDER BY trades DESC
                    """
                ),
                paper_p,
            ).fetchall()
            result["regime_trades"] = [dict(r._mapping) for r in regime_trade_rows]

            # 5. Close reason breakdown
            close_reason_rows = conn.execute(
                text(
                    """
                    SELECT close_reason, COUNT(*) AS count
                    FROM trades
                    WHERE paper = :paper
                      AND timestamp_open >= CURRENT_DATE - INTERVAL '30 days'
                      AND timestamp_close IS NOT NULL
                    GROUP BY close_reason
                    ORDER BY count DESC
                    """
                ),
                paper_p,
            ).fetchall()
            result["close_reasons"] = [dict(r._mapping) for r in close_reason_rows]

        # 6. Current config values (read from live module)
        result["config"] = {
            "ADX_TREND_THRESHOLD":                config.ADX_TREND_THRESHOLD,
            "ADX_AMBIGUOUS_LOW":                  config.ADX_AMBIGUOUS_LOW,
            "ADX_AMBIGUOUS_HIGH":                 config.ADX_AMBIGUOUS_HIGH,
            "FUNDING_CROWDED_PERCENTILE":         config.FUNDING_CROWDED_PERCENTILE,
            "FUNDING_SHORTS_CROWDED_PERCENTILE":  config.FUNDING_SHORTS_CROWDED_PERCENTILE,
            "ATR_STOP_MULTIPLIER":                config.ATR_STOP_MULTIPLIER,
            "ATR_TP_MULTIPLIER":                  config.ATR_TP_MULTIPLIER,
            "MAX_POSITION_PCT":                   config.MAX_POSITION_PCT,
            "DAILY_LOSS_CAP_USD":                 config.DAILY_LOSS_CAP_USD,
            "SIZE_MULTIPLIER_CROWDED":            config.SIZE_MULTIPLIER_CROWDED,
            "SIZE_MULTIPLIER_RANGE_UNSTABLE":     config.SIZE_MULTIPLIER_RANGE_UNSTABLE,
            "CLAUDE_SEVERITY_BLOCK_THRESHOLD":    config.CLAUDE_SEVERITY_BLOCK_THRESHOLD,
            "CLAUDE_SEVERITY_EVENT_RISK_THRESHOLD": config.CLAUDE_SEVERITY_EVENT_RISK_THRESHOLD,
        }

        return result

    # ── Strategy switching detection ─────────────────────────────────────────

    async def _detect_strategy_performance(self) -> dict:
        """
        Query last 14 days of trade and tick data to assess trend vs range
        playbook performance and whether the bot is trading enough.

        Returns a dict with keys:
          trend_trades, trend_wins, trend_win_rate (0.0-1.0), trend_avg_pnl,
          range_trades, range_wins, range_win_rate (0.0-1.0), range_avg_pnl,
          no_trade_pct, dominant_regime, recommendation
        """
        _empty: dict = {
            "trend_trades": 0, "trend_wins": 0,
            "trend_win_rate": 0.0, "trend_avg_pnl": 0.0,
            "range_trades": 0, "range_wins": 0,
            "range_win_rate": 0.0, "range_avg_pnl": 0.0,
            "no_trade_pct": 0.0,
            "dominant_regime": "NO_TRADE",
            "recommendation": "insufficient_data",
        }

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return _empty

        try:
            engine = create_engine(database_url)
            result = dict(_empty)

            with engine.begin() as conn:
                # ── 1. Trade outcomes by playbook ─────────────────────────────
                rows = conn.execute(
                    text(
                        """
                        SELECT
                          CASE
                            WHEN regime_at_entry LIKE 'TREND%' THEN 'trend'
                            WHEN regime_at_entry LIKE 'RANGE%' THEN 'range'
                            ELSE 'other'
                          END AS playbook,
                          COUNT(*)                                        AS trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)  AS wins,
                          AVG(pnl_usd)                                    AS avg_pnl
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '14 days'
                          AND timestamp_close IS NOT NULL
                          AND close_reason NOT IN (
                                'test_cleanup','manual_cleanup','pre_m7_cleanup'
                              )
                        GROUP BY playbook
                        """
                    ),
                    {"paper": config.PAPER_MODE},
                ).fetchall()

                for row in rows:
                    pb     = row._mapping["playbook"]
                    trades = int(row._mapping["trades"] or 0)
                    wins   = int(row._mapping["wins"] or 0)
                    avg    = float(row._mapping["avg_pnl"] or 0.0)
                    rate   = (wins / trades) if trades > 0 else 0.0
                    if pb == "trend":
                        result["trend_trades"]   = trades
                        result["trend_wins"]     = wins
                        result["trend_win_rate"] = rate
                        result["trend_avg_pnl"]  = avg
                    elif pb == "range":
                        result["range_trades"]   = trades
                        result["range_wins"]     = wins
                        result["range_win_rate"] = rate
                        result["range_avg_pnl"]  = avg

                # ── 2. NO_TRADE percentage ────────────────────────────────────
                row = conn.execute(
                    text(
                        """
                        SELECT
                          SUM(CASE WHEN decision = 'NO_TRADE' THEN 1 ELSE 0 END)
                            * 100.0 / NULLIF(COUNT(*), 0) AS no_trade_pct
                        FROM ticks
                        WHERE timestamp >= CURRENT_DATE - INTERVAL '14 days'
                        """
                    )
                ).fetchone()
                if row and row._mapping["no_trade_pct"] is not None:
                    result["no_trade_pct"] = float(row._mapping["no_trade_pct"])

                # ── 3. Dominant regime ────────────────────────────────────────
                row = conn.execute(
                    text(
                        """
                        SELECT regime, COUNT(*) AS count
                        FROM ticks
                        WHERE timestamp >= CURRENT_DATE - INTERVAL '14 days'
                        GROUP BY regime
                        ORDER BY count DESC
                        LIMIT 1
                        """
                    )
                ).fetchone()
                if row:
                    result["dominant_regime"] = str(row._mapping["regime"])

            # ── Recommendation logic ──────────────────────────────────────────
            total = result["trend_trades"] + result["range_trades"]
            if total < 10:
                result["recommendation"] = "insufficient_data"
            elif result["no_trade_pct"] > 60:
                result["recommendation"] = "loosen_filter"
            elif result["trend_trades"] >= 5 and result["range_trades"] >= 5:
                tw = result["trend_win_rate"]
                rw = result["range_win_rate"]
                if tw > rw + 0.2:
                    result["recommendation"] = "favour_trend"
                elif rw > tw + 0.2:
                    result["recommendation"] = "favour_range"
                else:
                    result["recommendation"] = "balanced"
            else:
                result["recommendation"] = "balanced"

            return result

        except Exception as exc:
            print(
                f"[self_improvement] _detect_strategy_performance error: {exc}",
                file=sys.stderr,
            )
            return _empty

    def _get_switching_suggestion(self, perf: dict) -> dict | None:
        """
        Convert a strategy performance recommendation into a concrete config
        suggestion dict, or return None if no switching action is needed.
        """
        rec = perf.get("recommendation", "insufficient_data")
        if rec in ("balanced", "insufficient_data"):
            return None

        current_val = config.ADX_TREND_THRESHOLD

        if rec == "loosen_filter":
            new_val = current_val - 2
            reason = (
                f"⚡ Auto-detected: Bot in NO_TRADE {perf.get('no_trade_pct', 0):.0f}% "
                f"of cycles. Lowering ADX threshold will allow more trades."
            )
        elif rec == "favour_trend":
            new_val = current_val + 3
            reason = (
                f"⚡ Auto-detected: Trend trades winning at "
                f"{perf.get('trend_win_rate', 0) * 100:.0f}% vs range at "
                f"{perf.get('range_win_rate', 0) * 100:.0f}%. "
                f"Raising ADX threshold favours trend playbook."
            )
        elif rec == "favour_range":
            new_val = current_val - 3
            reason = (
                f"⚡ Auto-detected: Range trades winning at "
                f"{perf.get('range_win_rate', 0) * 100:.0f}% vs trend at "
                f"{perf.get('trend_win_rate', 0) * 100:.0f}%. "
                f"Lowering ADX threshold favours range playbook."
            )
        else:
            return None

        return {
            "param":     "ADX_TREND_THRESHOLD",
            "current":   str(current_val),
            "suggested": str(new_val),
            "reason":    reason,
        }

    async def _write_strategy_performance(self, perf: dict) -> None:
        """
        Write two rows (trend + range) to strategy_performance table for this week.
        Builds a historical record of playbook performance over time.
        """
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return
        try:
            from db.models import strategy_performance
            engine = create_engine(database_url)
            week_start = datetime.now(timezone.utc).date()
            recommendation = perf.get("recommendation", "insufficient_data")
            no_trade_pct   = perf.get("no_trade_pct", 0.0)

            with engine.begin() as conn:
                conn.execute(
                    strategy_performance.insert().values(
                        week_start=week_start,
                        playbook="trend",
                        trades=perf.get("trend_trades", 0),
                        wins=perf.get("trend_wins", 0),
                        avg_pnl=perf.get("trend_avg_pnl", 0.0),
                        no_trade_pct=no_trade_pct,
                        recommendation=recommendation,
                    )
                )
                conn.execute(
                    strategy_performance.insert().values(
                        week_start=week_start,
                        playbook="range",
                        trades=perf.get("range_trades", 0),
                        wins=perf.get("range_wins", 0),
                        avg_pnl=perf.get("range_avg_pnl", 0.0),
                        no_trade_pct=no_trade_pct,
                        recommendation=recommendation,
                    )
                )
        except Exception as exc:
            print(
                f"[self_improvement] _write_strategy_performance error: {exc}",
                file=sys.stderr,
            )

    # ── Obsidian trade insights ───────────────────────────────────────────────

    async def _generate_trade_insights(self) -> str:
        """
        Read recent trade markdown files from the Obsidian vault and ask
        Claude to identify patterns in wins and losses.

        Also queries the DB for structured playbook analytics so Claude can
        identify which playbooks are working and which are not.

        Returns the analysis as a plain-text string, or a short fallback
        message if there are no trade files yet.
        """
        try:
            trade_files = self._memory.get_recent_trades(days=30)
            if not trade_files:
                return "No trades recorded in Obsidian vault yet."

            trade_text = "\n\n---\n\n".join(trade_files[:20])

            # ── Query DB for playbook analytics ───────────────────────────────
            playbook_section = ""
            database_url = os.environ.get("DATABASE_URL", "")
            if database_url:
                try:
                    engine = create_engine(database_url)
                    with engine.begin() as conn:
                        rows = conn.execute(
                            text(
                                """
                                SELECT
                                    COALESCE(playbook, 'unknown') AS playbook,
                                    COUNT(*)                                        AS trades,
                                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)  AS wins,
                                    AVG(pnl_usd)                                    AS avg_pnl,
                                    AVG(risk_reward)                                AS avg_rr_planned,
                                    AVG(time_horizon_days)                          AS avg_hold_planned
                                FROM trades
                                WHERE paper = :paper
                                  AND timestamp_close IS NOT NULL
                                  AND close_reason NOT IN (
                                        'test_cleanup','manual_cleanup','pre_m7_cleanup'
                                      )
                                GROUP BY playbook
                                ORDER BY COUNT(*) DESC
                                """
                            ),
                            {"paper": config.PAPER_MODE},
                        ).fetchall()

                    if rows:
                        pb_lines = ["PERFORMANCE BY PLAYBOOK:"]
                        for row in rows:
                            t   = int(row._mapping["trades"] or 0)
                            w   = int(row._mapping["wins"] or 0)
                            avg = float(row._mapping["avg_pnl"] or 0)
                            rr  = row._mapping["avg_rr_planned"]
                            hz  = row._mapping["avg_hold_planned"]
                            wr  = (w / t * 100) if t > 0 else 0.0
                            rr_str = f", avg planned R/R {float(rr):.1f}" if rr is not None else ""
                            hz_str = f", avg hold {float(hz):.1f}d" if hz is not None else ""
                            pb_lines.append(
                                f"  {row._mapping['playbook']}: {t} trades, "
                                f"win_rate {wr:.0f}%, avg_pnl {avg:+.2f} USD"
                                f"{rr_str}{hz_str}"
                            )
                        playbook_section = "\n".join(pb_lines)
                except Exception as db_exc:
                    print(
                        f"[self_improvement] playbook analytics query error: {db_exc}",
                        file=sys.stderr,
                    )

            import anthropic
            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=1000,
                system=(
                    "You are analysing your own past trading decisions. "
                    "Be brutally honest. Identify specific patterns, not generalities."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Review these {len(trade_files[:20])} recent trades from your "
                        f"Obsidian vault.\n"
                        "For each trade file, note:\n"
                        "- Was the entry thesis correct?\n"
                        "- Was the exit timing right?\n"
                        "- What market condition were you in?\n"
                        "- Did you follow your own rules?\n\n"
                        "Identify:\n"
                        "1. The 2-3 setups that consistently work for you\n"
                        "2. The 2-3 mistakes you keep making\n"
                        "3. Any market conditions you should avoid entirely\n"
                        "4. Whether your position sizing has been appropriate\n"
                        "5. Which playbooks are performing best and worst\n\n"
                        + (f"{playbook_section}\n\n" if playbook_section else "")
                        + f"Trade files:\n{trade_text}"
                    ),
                }],
            )
            return response.content[0].text

        except Exception as exc:
            print(
                f"[self_improvement] _generate_trade_insights error: {exc}",
                file=sys.stderr,
            )
            return f"Trade insights unavailable: {exc}"

    # ── Performance attribution ───────────────────────────────────────────────

    async def _compute_performance_attribution(self, days: int = 30) -> dict:
        """
        Query the trades table for structured per-playbook, per-condition,
        slippage, exit-type, and time-horizon analytics.

        Returns a dict with keys: playbooks, conditions, slippage, exits,
        time_horizon.  Each value is a list of row dicts (or None if query
        returned no rows).  Returns an empty dict on DB error.
        """
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return {}

        paper_p = {"paper": config.PAPER_MODE, "days": days}
        interval = f"{days} days"

        try:
            engine = create_engine(database_url)
            result: dict = {}

            with engine.begin() as conn:
                # 1. Per-playbook performance
                rows = conn.execute(
                    text(
                        f"""
                        SELECT
                          playbook,
                          COUNT(*)                                         AS total_trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)   AS wins,
                          ROUND(AVG(pnl_usd)::numeric, 4)                 AS avg_pnl,
                          ROUND(AVG(risk_reward)::numeric, 2)             AS avg_planned_rr,
                          ROUND(AVG(
                            CASE WHEN direction = 'LONG'
                            THEN (exit_price - entry_price)
                                 / NULLIF(entry_price - stop_loss, 0)
                            ELSE (entry_price - exit_price)
                                 / NULLIF(stop_loss - entry_price, 0)
                            END
                          )::numeric, 2)                                  AS avg_actual_rr,
                          ROUND(AVG(
                            EXTRACT(EPOCH FROM (timestamp_close - timestamp_open))
                            / 3600
                          )::numeric, 1)                                  AS avg_hold_hours
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_close IS NOT NULL
                          AND playbook IS NOT NULL
                          AND close_reason NOT IN (
                                'test_cleanup','manual_cleanup','pre_m7_cleanup'
                              )
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '{interval}'
                        GROUP BY playbook
                        ORDER BY avg_pnl DESC
                        """
                    ),
                    paper_p,
                ).fetchall()
                result["playbooks"] = [dict(r._mapping) for r in rows] or None

                # 2. Per-market-condition performance
                rows = conn.execute(
                    text(
                        f"""
                        SELECT
                          market_condition,
                          COUNT(*)                                        AS trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)  AS wins,
                          ROUND(AVG(pnl_usd)::numeric, 4)                AS avg_pnl
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_close IS NOT NULL
                          AND market_condition IS NOT NULL
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '{interval}'
                        GROUP BY market_condition
                        ORDER BY avg_pnl DESC
                        """
                    ),
                    paper_p,
                ).fetchall()
                result["conditions"] = [dict(r._mapping) for r in rows] or None

                # 3. Overall slippage (planned vs actual RR)
                row = conn.execute(
                    text(
                        f"""
                        SELECT
                          ROUND(AVG(risk_reward)::numeric, 2) AS avg_planned_rr,
                          ROUND(AVG(
                            CASE WHEN direction = 'LONG'
                            THEN (exit_price - entry_price)
                                 / NULLIF(entry_price - stop_loss, 0)
                            ELSE (entry_price - exit_price)
                                 / NULLIF(stop_loss - entry_price, 0)
                            END
                          )::numeric, 2)                      AS avg_actual_rr
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_close IS NOT NULL
                          AND risk_reward IS NOT NULL
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '{interval}'
                        """
                    ),
                    paper_p,
                ).fetchone()
                result["slippage"] = dict(row._mapping) if row else None

                # 4. Exit-type breakdown
                rows = conn.execute(
                    text(
                        f"""
                        SELECT
                          exit_type,
                          COUNT(*)                              AS count,
                          ROUND(AVG(pnl_usd)::numeric, 4)      AS avg_pnl
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_close IS NOT NULL
                          AND exit_type IS NOT NULL
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '{interval}'
                        GROUP BY exit_type
                        ORDER BY count DESC
                        """
                    ),
                    paper_p,
                ).fetchall()
                result["exits"] = [dict(r._mapping) for r in rows] or None

                # 5. Time-horizon accuracy (planned vs actual)
                row = conn.execute(
                    text(
                        f"""
                        SELECT
                          ROUND(AVG(time_horizon_days)::numeric, 1)       AS avg_planned_days,
                          ROUND(AVG(
                            EXTRACT(EPOCH FROM (timestamp_close - timestamp_open))
                            / 86400
                          )::numeric, 1)                                   AS avg_actual_days
                        FROM trades
                        WHERE paper = :paper
                          AND timestamp_close IS NOT NULL
                          AND time_horizon_days IS NOT NULL
                          AND timestamp_open >= CURRENT_DATE - INTERVAL '{interval}'
                        """
                    ),
                    paper_p,
                ).fetchone()
                result["time_horizon"] = dict(row._mapping) if row else None

            return result

        except Exception as exc:
            print(
                f"[self_improvement] _compute_performance_attribution error: {exc}",
                file=sys.stderr,
            )
            return {}

    def _format_attribution(self, attribution: dict) -> str:
        """
        Format the attribution dict as a readable string for the Claude prompt
        and Telegram messages.  Returns a graceful fallback if no data.
        """
        if not attribution:
            return "Insufficient trade data for attribution analysis."

        # Check whether any section has rows
        has_data = any(
            attribution.get(k) for k in ("playbooks", "conditions", "exits")
        )
        if not has_data:
            return "Insufficient trade data for attribution analysis."

        lines: list[str] = ["=== QUANTITATIVE PERFORMANCE ATTRIBUTION ==="]

        # Playbook performance
        playbooks = attribution.get("playbooks") or []
        if playbooks:
            lines.append("\nPLAYBOOK PERFORMANCE:")
            for row in playbooks:
                pb         = row.get("playbook") or "unknown"
                total      = int(row.get("total_trades") or 0)
                wins       = int(row.get("wins") or 0)
                avg_pnl    = float(row.get("avg_pnl") or 0)
                planned_rr = row.get("avg_planned_rr")
                actual_rr  = row.get("avg_actual_rr")
                win_rate   = (wins / total * 100) if total > 0 else 0.0

                rr_part = ""
                if planned_rr is not None and actual_rr is not None:
                    p = float(planned_rr)
                    a = float(actual_rr)
                    rr_part = f" | RR planned {p:.2f} vs actual {a:.2f}"
                    if p != 0:
                        deg = (a - p) / abs(p) * 100
                        rr_part += f" ({deg:+.0f}% slippage)"
                elif planned_rr is not None:
                    rr_part = f" | RR planned {float(planned_rr):.2f}"

                lines.append(
                    f"  {pb}: {total} trades | {win_rate:.0f}% win | "
                    f"avg P&L {avg_pnl:+.4f} USD{rr_part}"
                )

        # Market condition performance
        conditions = attribution.get("conditions") or []
        if conditions:
            lines.append("\nMARKET CONDITION PERFORMANCE:")
            for row in conditions:
                cond     = row.get("market_condition") or "unknown"
                trades   = int(row.get("trades") or 0)
                wins     = int(row.get("wins") or 0)
                avg_pnl  = float(row.get("avg_pnl") or 0)
                win_rate = (wins / trades * 100) if trades > 0 else 0.0
                lines.append(
                    f"  {cond}: {trades} trades | {win_rate:.0f}% win | "
                    f"avg {avg_pnl:+.4f} USD"
                )

        # Exit analysis
        exits = attribution.get("exits") or []
        if exits:
            lines.append("\nEXIT ANALYSIS:")
            for row in exits:
                et      = row.get("exit_type") or "unknown"
                count   = int(row.get("count") or 0)
                avg_pnl = float(row.get("avg_pnl") or 0)
                lines.append(f"  {et}: {count} exits | avg {avg_pnl:+.4f} USD")

        # Execution quality (slippage)
        slippage = attribution.get("slippage") or {}
        planned_rr = slippage.get("avg_planned_rr")
        actual_rr  = slippage.get("avg_actual_rr")
        if planned_rr is not None or actual_rr is not None:
            lines.append("\nEXECUTION QUALITY:")
            p_str = f"{float(planned_rr):.2f}" if planned_rr is not None else "N/A"
            a_str = f"{float(actual_rr):.2f}"  if actual_rr  is not None else "N/A"
            lines.append(f"  Planned RR: {p_str} | Actual RR: {a_str}")
            if planned_rr is not None and actual_rr is not None:
                p = float(planned_rr)
                a = float(actual_rr)
                if p != 0:
                    deg = (a - p) / abs(p) * 100
                    lines.append(f"  RR degradation: {deg:+.0f}%")

        # Hold time accuracy
        th = attribution.get("time_horizon") or {}
        planned_days = th.get("avg_planned_days")
        actual_days  = th.get("avg_actual_days")
        if planned_days is not None or actual_days is not None:
            lines.append("\nHOLD TIME:")
            p_str = f"{float(planned_days):.1f}" if planned_days is not None else "N/A"
            a_str = f"{float(actual_days):.1f}"  if actual_days  is not None else "N/A"
            lines.append(f"  Planned: {p_str} days | Actual: {a_str} days")

        lines.append("\n=== END ATTRIBUTION ===")
        return "\n".join(lines)

    async def _send_attribution_message(self, attribution: dict) -> None:
        """
        Send a concise performance attribution breakdown to Telegram.
        Adds ✅/⚠️ signals for playbooks that are clearly working or failing.
        """
        playbooks = (attribution or {}).get("playbooks") or []
        if not playbooks:
            return  # Nothing to send — no attributed trades yet

        lines: list[str] = [
            "📊 <b>PERFORMANCE ATTRIBUTION</b>",
            "Last 30 days\n",
        ]

        for row in playbooks:
            pb      = row.get("playbook") or "unknown"
            total   = int(row.get("total_trades") or 0)
            wins    = int(row.get("wins") or 0)
            avg_pnl = float(row.get("avg_pnl") or 0)
            wr      = (wins / total * 100) if total > 0 else 0.0

            planned_rr = row.get("avg_planned_rr")
            actual_rr  = row.get("avg_actual_rr")
            rr_str = ""
            if planned_rr is not None and actual_rr is not None:
                rr_str = f" | RR {float(planned_rr):.1f}→{float(actual_rr):.1f}"

            lines.append(
                f"<b>{pb}</b>: {total}T | {wr:.0f}% win | "
                f"{avg_pnl:+.2f} USD avg{rr_str}"
            )

            # Signals
            if wr < 40 and avg_pnl < 0:
                lines.append(
                    f"  ⚠️ {pb} underperforming — Claude will reduce use next week"
                )
            elif wr > 60 and avg_pnl > 0:
                lines.append(
                    f"  ✅ {pb} performing well — Claude will continue this approach"
                )

        # Execution quality summary
        slippage = (attribution or {}).get("slippage") or {}
        planned_rr = slippage.get("avg_planned_rr")
        actual_rr  = slippage.get("avg_actual_rr")
        if planned_rr is not None and actual_rr is not None:
            p = float(planned_rr)
            a = float(actual_rr)
            if p != 0:
                deg = (a - p) / abs(p) * 100
                lines.append(
                    f"\n<b>RR execution:</b> planned {p:.2f} → actual {a:.2f} "
                    f"({deg:+.0f}%)"
                )

        await self._notifier.send("\n".join(lines))

    # ── Claude API call ───────────────────────────────────────────────────────

    async def _get_suggestions(self, data: dict) -> str:
        """Send performance data to Claude and return the raw suggestion text."""
        try:
            import anthropic

            client = anthropic.AsyncAnthropic()

            # Build formatted user prompt from data
            overall = data.get("overall", {})
            total_trades = int(overall.get("total_trades") or 0)
            wins         = int(overall.get("wins") or 0)
            win_rate     = (wins / total_trades * 100) if total_trades else 0.0
            avg_pnl      = float(overall.get("avg_pnl") or 0)
            total_pnl    = float(overall.get("total_pnl") or 0)

            def _section(rows, key_field, count_field, extra=""):
                if not rows:
                    return "  (no data)"
                lines = []
                for r in rows:
                    pct = r.get("pct", "")
                    pct_str = f" ({pct}%)" if pct else ""
                    lines.append(f"  {r[key_field]}: {r[count_field]}{pct_str}{extra}")
                return "\n".join(lines)

            reject_section = _section(data.get("rejections", []), "layer", "count")
            regime_section = _section(data.get("regime_dist", []), "regime", "count")

            regime_trade_lines = []
            for r in data.get("regime_trades", []):
                avg = float(r["avg_pnl"] or 0)
                regime_trade_lines.append(
                    f"  {r['regime_at_entry']}: {r['trades']} trades, "
                    f"{r['wins']} wins, avg {avg:+.4f} USD"
                )
            regime_trade_section = (
                "\n".join(regime_trade_lines) if regime_trade_lines else "  (no data)"
            )

            close_lines = [
                f"  {r['close_reason'] or 'unknown'}: {r['count']}"
                for r in data.get("close_reasons", [])
            ]
            close_section = "\n".join(close_lines) if close_lines else "  (no data)"

            config_lines = [
                f"  {k} = {v}" for k, v in data.get("config", {}).items()
            ]
            config_section = "\n".join(config_lines)

            user_prompt = (
                f"Performance data for last 30 days:\n\n"
                f"OVERALL: {total_trades} trades, {win_rate:.0f}% win rate, "
                f"{total_pnl:+.4f} USD total P&L, avg {avg_pnl:+.4f} USD/trade\n\n"
                f"REJECTION BREAKDOWN:\n{reject_section}\n\n"
                f"REGIME DISTRIBUTION:\n{regime_section}\n\n"
                f"TRADE OUTCOMES BY REGIME:\n{regime_trade_section}\n\n"
                f"CLOSE REASONS:\n{close_section}\n\n"
                f"CURRENT CONFIG:\n{config_section}"
            )

            # Include past change outcomes so Claude has memory of what was tried
            outcome_assessment = data.get("outcome_assessment", "")
            if outcome_assessment:
                user_prompt += (
                    f"\n\nRECENT CONFIG CHANGES AND OUTCOMES:\n{outcome_assessment}"
                )

            # Quantitative attribution — measured per-playbook/condition outcomes
            attribution = data.get("attribution", "")
            if attribution and "Insufficient" not in attribution:
                user_prompt += f"\n\nQUANTITATIVE PERFORMANCE DATA:\n{attribution}"

            # Include Obsidian trade pattern analysis so parameter suggestions
            # are grounded in actual trade behaviour, not just aggregate stats.
            trade_insights = data.get("trade_insights", "")
            if trade_insights:
                user_prompt += (
                    f"\n\nTRADING PATTERN ANALYSIS (last 30 days from Obsidian vault):\n"
                    f"{trade_insights}\n\n"
                    f"Use this to inform your parameter suggestions. "
                    f"If specific setup types are consistently losing, "
                    f"that may require config changes to filter them out."
                )

            system_prompt = (
                "You are a quantitative trading strategy analyst reviewing an automated "
                "crypto trading bot. The bot trades BTC and ETH perpetual futures on OKX "
                "using a rule-based multi-layer signal engine. Your job is to suggest "
                "specific config improvements based on performance data.\n\n"
                "CRITICAL RULES:\n"
                "- Only suggest changes to existing config parameters listed in the data.\n"
                "- Never suggest ML, model training, or adding new indicators.\n"
                "- Each suggestion must include: the exact parameter name, current value, "
                "suggested new value, and one-sentence reason.\n"
                "- Maximum 3 suggestions. Quality over quantity.\n"
                "- If performance looks good, say so and suggest no changes.\n"
                "- If a recent config change WORSENED performance, consider reverting it.\n"
                "- End with a rating: READY TO GO LIVE / NEEDS TUNING / MAJOR ISSUES\n\n"
                "You have access to quantitative performance data showing exactly which "
                "of your trading strategies are working and which are not. Use this data:\n"
                "- Recommend reducing reliance on playbooks with negative expectancy\n"
                "- Recommend leaning into playbooks with positive expectancy\n"
                "- Consider market conditions where the bot consistently loses\n"
                "- Flag high RR degradation (planned vs actual) as an execution problem\n"
                "- Your suggestions must be grounded in measured outcomes, "
                "not general trading theory\n\n"
                "Format your response EXACTLY like this — no deviations:\n\n"
                "SUMMARY: [one sentence overall assessment]\n\n"
                "SUGGESTION_1:\n"
                "param: [EXACT_CONFIG_PARAM_NAME]\n"
                "current: [current value]\n"
                "suggested: [new value]\n"
                "reason: [one sentence]\n\n"
                "SUGGESTION_2:\n"
                "param: [EXACT_CONFIG_PARAM_NAME]\n"
                "current: [current value]\n"
                "suggested: [new value]\n"
                "reason: [one sentence]\n\n"
                "SUGGESTION_3:\n"
                "param: [EXACT_CONFIG_PARAM_NAME]\n"
                "current: [current value]\n"
                "suggested: [new value]\n"
                "reason: [one sentence]\n\n"
                "DOING_WELL: [one sentence about what should not change]\n\n"
                "RATING: [READY TO GO LIVE / NEEDS TUNING / MAJOR ISSUES]"
            )

            response = await client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text

        except Exception as exc:
            print(
                f"[self_improvement] _get_suggestions error: {exc}",
                file=sys.stderr,
            )
            return "API_ERROR"

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_suggestions(self, raw: str) -> list:
        """
        Parse Claude's structured response into suggestion dicts.
        Stores result in self.pending_suggestions.
        Returns the suggestions list (may be empty).
        """
        suggestions: list = []
        summary     = ""
        doing_well  = ""
        rating      = "NEEDS TUNING"

        try:
            if raw == "API_ERROR":
                self.pending_suggestions = {
                    "suggestions": [],
                    "summary":     "API error — could not generate analysis.",
                    "doing_well":  "",
                    "rating":      "NEEDS TUNING",
                    "generated_at": datetime.now().isoformat(),
                }
                return []

            # Extract SUMMARY
            m = re.search(r"SUMMARY:\s*([^\n]+)", raw)
            if m:
                summary = m.group(1).strip()

            # Extract DOING_WELL
            m = re.search(r"DOING_WELL:\s*([^\n]+)", raw)
            if m:
                doing_well = m.group(1).strip()

            # Extract RATING
            m = re.search(
                r"RATING:\s*(READY TO GO LIVE|NEEDS TUNING|MAJOR ISSUES)", raw
            )
            if m:
                rating = m.group(1).strip()

            # Extract up to 3 SUGGESTION blocks
            for i in range(1, 4):
                m = re.search(
                    rf"SUGGESTION_{i}:\s*\n"
                    rf"param:\s*([^\n]+)\n"
                    rf"current:\s*([^\n]+)\n"
                    rf"suggested:\s*([^\n]+)\n"
                    rf"reason:\s*([^\n]+)",
                    raw,
                )
                if m:
                    suggestions.append(
                        {
                            "param":     m.group(1).strip(),
                            "current":   m.group(2).strip(),
                            "suggested": m.group(3).strip(),
                            "reason":    m.group(4).strip(),
                        }
                    )

        except Exception as exc:
            print(
                f"[self_improvement] _parse_suggestions error: {exc}",
                file=sys.stderr,
            )

        self.pending_suggestions = {
            "suggestions":  suggestions,
            "summary":      summary,
            "doing_well":   doing_well,
            "rating":       rating,
            "generated_at": datetime.now().isoformat(),
        }
        return suggestions

    # ── Telegram output ───────────────────────────────────────────────────────

    async def _send_suggestion_message(self) -> None:
        """
        Format and send the strategy review to Telegram.
        Handles up to 4 suggestions (3 from Claude + 1 auto-detected switching).
        """
        if not self._notifier.enabled:
            return
        if self.pending_suggestions is None:
            return

        ps          = self.pending_suggestions
        suggestions = ps.get("suggestions", [])
        summary     = ps.get("summary", "")
        doing_well  = ps.get("doing_well", "")
        rating      = ps.get("rating", "NEEDS TUNING")
        mode        = "PAPER" if config.PAPER_MODE else "LIVE"
        today_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rating_emoji = {
            "READY TO GO LIVE": "✅",
            "NEEDS TUNING":     "⚠️",
            "MAJOR ISSUES":     "🚨",
        }.get(rating, "⚠️")

        if not suggestions:
            suggestions_section = "✅ No changes needed this week."
            footer = ""
        else:
            lines = []
            for n, s in enumerate(suggestions, 1):
                # Auto-detected suggestions already carry the ⚡ prefix in reason
                lines.append(
                    f"{n}. <code>{s['param']}</code>: {s['current']} → {s['suggested']}\n"
                    f"   {s['reason']}"
                )
            suggestions_section = "\n\n".join(lines)
            footer = (
                "\n─────────────────\n"
                "Reply /apply to implement these changes\n"
                "Reply /skip to keep current settings\n"
                "Changes apply on the next decision cycle."
            )

        doing_well_line = (
            f"\n\n<b>What's working:</b> {doing_well}" if doing_well else ""
        )

        msg = (
            f"🤖 <b>WEEKLY STRATEGY REVIEW</b>\n"
            f"{today_str} | Mode: {mode}\n"
            f"\n"
            f"<b>Assessment:</b> {summary}\n"
            f"\n"
            f"<b>Suggested changes:</b>\n"
            f"{suggestions_section}"
            f"{doing_well_line}\n"
            f"\n"
            f"<b>Rating:</b> {rating_emoji} {rating}"
            f"{footer}"
        )
        await self._notifier.send(msg)

    # ── Apply / Skip ──────────────────────────────────────────────────────────

    async def apply_suggestions(self) -> None:
        """
        Called when user replies /apply in Telegram.
        Edits config.py in-place, reloads the config module, and records
        each change in config_change_history for outcome tracking.
        """
        if self.pending_suggestions is None:
            await self._notifier.send("No pending suggestions to apply.")
            return

        suggestions = self.pending_suggestions.get("suggestions", [])
        if not suggestions:
            await self._notifier.send(
                "No changes were suggested — nothing to apply."
            )
            return

        # Locate config.py relative to this file (engine/ → project root)
        config_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.py")
        )

        try:
            with open(config_path, "r") as fh:
                content = fh.read()
        except Exception as exc:
            print(
                f"[self_improvement] apply_suggestions read error: {exc}",
                file=sys.stderr,
            )
            await self._notifier.send(f"❌ Error reading config.py: {exc}")
            return

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # BUG 2 DIAGNOSIS: log the full pending_suggestions before applying
        print(
            f"[self_improvement] apply_suggestions — pending_suggestions: "
            f"{self.pending_suggestions}",
            flush=True,
        )

        applied: list = []

        for s in suggestions:
            param   = s["param"]
            new_val = s["suggested"].strip()

            # BUG 2 FIX: use [^\n]* (not [^\n#]+) to consume the entire existing
            # line, including any trailing comment from a previous self-improvement
            # run.  This prevents old comments from being left in place and ensures
            # the written value is exactly new_val.
            pattern = rf"^{re.escape(param)}\s*=\s*[^\n]*"
            replacement = f"{param} = {new_val}  # Updated {today_str} by self-improvement"

            # BUG 2 DIAGNOSIS: log exact match before substitution
            existing_match = re.search(pattern, content, flags=re.MULTILINE)
            print(
                f"[self_improvement] Replacing param='{param}': "
                f"match='{existing_match.group(0) if existing_match else None}' "
                f"→ '{replacement}'",
                flush=True,
            )

            new_content, count = re.subn(
                pattern, replacement, content, flags=re.MULTILINE, count=1
            )
            if count > 0:
                content = new_content
                applied.append({**s, "applied_val": new_val})
            else:
                print(
                    f"[self_improvement] WARNING: param '{param}' not found in config.py",
                    file=sys.stderr,
                )

        if not applied:
            await self._notifier.send(
                "❌ No matching config parameters found. Nothing was changed."
            )
            return

        # BUG 3 FIX: send a confirmation message BEFORE writing config.py so the
        # user has an audit trail of exactly what is about to change.
        confirm_lines = [
            f"  • <code>{s['param']}</code>: {s['current']} → {s['applied_val']}"
            for s in applied
        ]
        await self._notifier.send(
            f"⏳ <b>Applying {len(applied)} change(s) to config.py…</b>\n"
            + "\n".join(confirm_lines)
            + "\nWriting file now…"
        )

        try:
            with open(config_path, "w") as fh:
                fh.write(content)
        except Exception as exc:
            print(
                f"[self_improvement] apply_suggestions write error: {exc}",
                file=sys.stderr,
            )
            await self._notifier.send(f"❌ Error writing config.py: {exc}")
            return

        # Reload config so the running bot uses the new values immediately
        try:
            import config as cfg_module
            importlib.reload(cfg_module)
            print(
                "[self_improvement] config.py reloaded after applying changes.",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[self_improvement] config reload error: {exc}",
                file=sys.stderr,
            )

        # ── Record each change in config_change_history ───────────────────────
        database_url = os.environ.get("DATABASE_URL", "")
        if database_url:
            try:
                from db.models import config_change_history

                engine = create_engine(database_url)
                week_start = datetime.now(timezone.utc).date()
                with engine.begin() as conn:
                    for s in applied:
                        conn.execute(
                            config_change_history.insert().values(
                                param_name=s["param"],
                                old_value=s.get("current", ""),
                                new_value=s["applied_val"],
                                reason=s.get("reason", ""),
                                suggested_by="claude",
                                week_start=week_start,
                                outcome="pending",
                            )
                        )
                print(
                    f"[self_improvement] Recorded {len(applied)} change(s) "
                    f"in config_change_history.",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[self_improvement] config_change_history insert error: {exc}",
                    file=sys.stderr,
                )

        # Build STRATEGY UPDATED message with per-change reason lines
        utc_now   = datetime.now(timezone.utc)
        myt_now   = utc_now + timedelta(hours=8)          # MYT = UTC+8
        date_str  = myt_now.strftime("%Y-%m-%d")
        time_str  = myt_now.strftime("%H:%M MYT")

        change_lines = []
        for s in applied:
            change_lines.append(
                f"  • <code>{s['param']}</code>: {s['current']} → {s['applied_val']}\n"
                f"    Reason: {s.get('reason', '—')}"
            )

        msg = (
            f"⚙️ <b>STRATEGY UPDATED</b>\n"
            f"{date_str} {time_str}\n"
            f"\n"
            f"Applied {len(applied)} change(s):\n"
            + "\n".join(change_lines)
            + "\n\nNext analysis: Sunday 08:30 MYT"
        )
        await self._notifier.send(msg)

        self.pending_suggestions = None

    async def skip_suggestions(self) -> None:
        """Called when user replies /skip in Telegram."""
        await self._notifier.send("⏭ Suggestions skipped. Config unchanged.")
        self.pending_suggestions = None
