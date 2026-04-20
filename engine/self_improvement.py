"""
Self-improvement analyser — Polymarket edition.

Runs every Sunday 08:30 MYT. Pipeline:

    1. Pull resolved predictions from the last 7 / 30 / 90 days.
    2. Compute calibration stats (Brier, reliability bins, per-category).
    3. Pull realized P&L and win rate from pm_positions.
    4. Ask Claude for one config tuning suggestion based on the data.
    5. Store the suggestion in config_change_history (status=pending),
       send a Telegram message with /apply and /skip buttons.
    6. Append lessons to the Obsidian vault (`strategy/*.md`).

Design rules:
  * Only one change per week. Over-tuning = spurious patterns.
  * Minimum sample size (SELF_IMPROVE_MIN_RESOLVED) must be met.
  * Suggestions must target a whitelist of keys (same as bot_api).
  * If no change is justified, the module silently stores no suggestion.
  * Every branch is exception-safe — this job must never take the bot down.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text

import anthropic

import calibration
import config
from config_utils import ALLOWED_CONFIG_KEYS, persist_config_value
from db.engine import get_engine

ALLOWED_KEYS = ALLOWED_CONFIG_KEYS
MAX_RELATIVE_CHANGE = float(getattr(config, "SELF_IMPROVE_MAX_RELATIVE_CHANGE", 0.15))
CONFIG_BOUNDS: dict[str, tuple[float | int | None, float | int | None]] = {
    "PM_SHADOW_MIN_EDGE_BPS":      (50.0, 3000.0),
    "PM_SHADOW_MIN_CONFIDENCE":    (0.05, 0.95),
    "PM_LIVE_MIN_EDGE_BPS":        (50.0, 3000.0),
    "PM_LIVE_MIN_CONFIDENCE":      (0.05, 0.95),
    "PM_KELLY_FRACTION":           (0.01, 1.0),
    "PM_MAX_POSITION_PCT":         (0.01, 0.50),
    "PM_MIN_TRADE_USD":            (1.0, 1000.0),
    "PM_MAX_TRADE_USD":            (1.0, 5000.0),
    "PM_MAX_CONCURRENT_POSITIONS": (1, 200),
    "PM_SCAN_LIMIT":               (1, 200),
    "PM_MIN_VOLUME_24H_USD":       (100.0, 1_000_000.0),
    "PM_MAX_DAYS_TO_END":          (1, 180),
    "PM_SKIP_EXISTING_DAYS":       (0, 30),
    "PM_MIN_RESOLUTION_QUALITY":   (0.0, 1.0),
    "PM_SHADOW_SPREAD_ESTIMATE":   (0.0, 0.05),
    "PM_SHADOW_FEE_RATE":          (0.0, 0.02),
    "PM_DAILY_LOSS_LIMIT_PCT":     (0.02, 0.30),
    "PM_WEEKLY_LOSS_LIMIT_PCT":    (0.05, 0.50),
    "PM_LOSS_STREAK_THRESHOLD":    (2, 10),
    "PM_LOSS_STREAK_SIZE_MULT":    (0.1, 1.0),
    "PM_MAX_PORTFOLIO_HEAT_PCT":   (0.10, 0.80),
    "PM_MAX_PER_ARCHETYPE":        (3, 30),
    "PM_DRAWDOWN_HALT_PCT":        (0.30, 0.90),
}


ARCHETYPE_RELATED_KEYS = {
    "PM_ARCHETYPE_EDGE_OVERRIDES", "PM_ARCHETYPE_CONFIDENCE_OVERRIDES",
    "PM_MAX_PER_ARCHETYPE",
}

# Known archetype names for reason-text detection.
_KNOWN_ARCHETYPES = {
    "price_threshold", "binary_event", "sports_match", "sports_prop",
    "geopolitical", "macro_release", "crypto", "entertainment",
    "scientific", "legal", "weather", "other",
}


def _has_sufficient_power(key: str, archetype: Optional[str] = None) -> tuple[bool, str]:
    """Check if we have enough data for a statistically meaningful config change."""
    min_samples = int(getattr(config, "SELF_IMPROVE_MIN_POWER_SAMPLES", 30))
    try:
        with get_engine().begin() as conn:
            if archetype:
                n = int(conn.execute(text(
                    "SELECT COUNT(*) FROM predictions "
                    "WHERE resolved_at IS NOT NULL AND market_archetype = :arch"
                ), {"arch": archetype}).scalar() or 0)
                if n < min_samples:
                    return False, f"only {n} resolved predictions for archetype '{archetype}' (need {min_samples})"
            # Global minimum
            total = int(conn.execute(text(
                "SELECT COUNT(*) FROM predictions WHERE resolved_at IS NOT NULL"
            )).scalar() or 0)
            min_total = int(getattr(config, "SELF_IMPROVE_MIN_RESOLVED", 15))
            if total < min_total:
                return False, f"only {total} total resolved predictions (need {min_total})"
        return True, "sufficient data"
    except Exception as exc:
        return False, f"power check failed: {exc}"


def _detect_archetype_from_reason(reason: str) -> Optional[str]:
    """Extract an archetype name from Claude's reasoning text, if any."""
    if not reason:
        return None
    reason_lower = reason.lower()
    for arch in _KNOWN_ARCHETYPES:
        # Match whole words: "sports_match" or "sports match"
        if arch in reason_lower or arch.replace("_", " ") in reason_lower:
            return arch
    return None


SYSTEM_PROMPT = (
    "You are the post-mortem analyst for a Polymarket prediction-market bot. "
    "Every Sunday you look at the last week of resolved predictions plus "
    "their outcomes and realized P&L, and propose at most one config change "
    "to improve calibration or risk-adjusted returns.\n\n"

    "CRITICAL ANALYSIS AREAS — you must address each:\n"
    "1. WIN/LOSS ASYMMETRY: Compare avg win P&L vs avg loss P&L. "
    "If avg_loss > 2x avg_win, the bot wins small and loses big — a fatal "
    "pattern even with >50% win rate. Identify which bets are causing the "
    "outsized losses.\n"
    "2. EDGE ACCURACY: Check edge_buckets. Is higher claimed edge correlated "
    "with actually winning? If extreme-edge bets (>2000bps) have ≤50% win "
    "rate, Claude's edge estimate is anti-predictive at high values — the "
    "bot is sizing up precisely its worst bets.\n"
    "3. CONFIDENCE ACCURACY: Check confidence_buckets. If high-confidence "
    "bets lose more than low-confidence bets, confidence is anti-predictive "
    "and needs dampening. Since confidence directly multiplies stake size, "
    "this creates catastrophic losses.\n"
    "4. SIDE ANALYSIS: Check YES vs NO performance. NO bets on cheap "
    "contracts (entry <15c) are especially dangerous — Kelly sizes them "
    "huge because the payoff ratio is attractive, but the entire stake "
    "is lost when they fail.\n"
    "5. WORST LOSSES: Examine the specific worst losses. What do they have "
    "in common? (usually: extreme edge + high confidence + NO side + sports).\n"
    "6. CORRELATIONS: Check edge_win_corr and conf_win_corr. Negative "
    "correlations mean the signal is anti-predictive.\n"
    "7. ENTRY PRICE ZONES: Check which price zones (extreme/moderate/balanced) "
    "are profitable vs losing.\n\n"

    "Constraints: (a) propose AT MOST ONE config change, (b) change must be "
    "in the provided whitelist, (c) change must be small (±15% of current "
    "value). If no change is warranted, return an empty suggestion.\n"
    "Also write a short update to 'what works' and 'what doesn't work' based "
    "on the measured results — be specific, cite numbers from the data.\n"
    "Include archetype-specific lessons with confidence_dampen factors where "
    "appropriate.\n\n"

    "Output STRICT JSON only (no markdown):\n"
    "{\"suggestion\": {\"key\": <str|null>, \"value\": <number|null>, "
    "\"reason\": <str>},\n"
    "\"risk_assessment\": <str, ≤200 words — cover win/loss asymmetry, "
    "worst losses, anti-predictive signals>,\n"
    "\"what_works\": <str, ≤200 words>,\n"
    "\"what_doesnt\": <str, ≤200 words>,\n"
    "\"current_thesis\": <str, ≤200 words>,\n"
    "\"archetype_lessons\": {<archetype>: {\"lesson\": <str>, "
    "\"confidence_dampen\": <float 0.5-1.0 or null>}, ...}}"
)




class SelfImprovementAnalyser:
    def __init__(self, notifier=None, memory=None):
        self._notifier = notifier
        self._memory   = memory
        try:
            self._claude = anthropic.Anthropic()
        except Exception as exc:
            print(f"[self_improve] Anthropic init failed: {exc}", file=sys.stderr)
            self._claude = None

    # ── Public entry points ──────────────────────────────────────────────────
    async def analyse_and_report(self) -> None:
        """Sunday job — compute stats, ask Claude, store suggestion, notify."""
        try:
            stats = await asyncio.get_running_loop().run_in_executor(None, self._gather_stats)
            min_resolved = int(getattr(config, "SELF_IMPROVE_MIN_RESOLVED", 15))
            if stats["resolved_window"] < min_resolved:
                await self._send(
                    f"📉 <b>Weekly review skipped</b>\n"
                    f"Only {stats['resolved_window']} resolved predictions in the last "
                    f"{stats['window_days']} days — need {min_resolved} for meaningful analysis."
                )
                return

            suggestion = await self._ask_claude(stats)
            if suggestion is None:
                await self._send("⚠️ Claude unavailable — no weekly review generated.")
                return

            # Update Obsidian memory immediately with what_works / what_doesnt / thesis / risk.
            try:
                if self._memory is not None:
                    risk_assessment = suggestion.get("risk_assessment", "")[:4000]
                    what_doesnt = suggestion.get("what_doesnt", "")[:4000]
                    # Prepend risk assessment to what_doesnt for visibility
                    if risk_assessment:
                        what_doesnt = f"[RISK] {risk_assessment}\n\n{what_doesnt}"
                    await asyncio.to_thread(
                        self._memory.update_strategy_memory,
                        what_works   = suggestion.get("what_works", "")[:4000],
                        what_doesnt  = what_doesnt,
                        current_thesis = suggestion.get("current_thesis", "")[:4000],
                    )
                    # Write archetype-specific lessons to memory.
                    archetype_lessons = suggestion.get("archetype_lessons")
                    if isinstance(archetype_lessons, dict) and archetype_lessons:
                        await asyncio.to_thread(self._update_archetype_memory, archetype_lessons)
            except Exception as exc:
                print(f"[self_improve] memory update failed: {exc}", file=sys.stderr)

            # Apply confidence dampening suggestions.
            try:
                self._apply_confidence_dampening(suggestion.get("archetype_lessons"))
            except Exception as exc:
                print(f"[self_improve] confidence dampening failed: {exc}", file=sys.stderr)

            # Persist and notify.
            change = (suggestion.get("suggestion") or {})
            key = (change.get("key") or "").strip()
            val = change.get("value")
            reason = (change.get("reason") or "").strip()

            msg_head = self._format_stats_telegram(stats)
            if not key or val is None or key not in ALLOWED_KEYS:
                await self._send(
                    f"{msg_head}\n\n"
                    f"<i>No config change recommended this week.</i>\n"
                    f"Reasoning: {reason or '(none)'}"
                )
                return

            current_config = stats.get("current_config") or {}
            val_cast, validation_error = self._validate_change(
                key=key,
                value=val,
                current_config=current_config,
            )
            if validation_error:
                await self._send(
                    f"{msg_head}\n\n"
                    f"⚠️ Claude proposed <code>{key}={val}</code> but it was rejected.\n"
                    f"Reason: {validation_error}"
                )
                return

            # ── Power analysis gate ───────────────────────────────────
            # Detect if this change targets a specific archetype (either
            # via an archetype-related key or archetype mentioned in the
            # reasoning). Require sufficient resolved predictions before
            # allowing the change through.
            target_archetype = _detect_archetype_from_reason(reason)
            power_ok, power_msg = await asyncio.to_thread(
                _has_sufficient_power, key, target_archetype,
            )
            if not power_ok:
                await self._send(
                    f"{msg_head}\n\n"
                    f"⚠️ Claude proposed <code>{key}={val_cast}</code> but blocked by power analysis.\n"
                    f"Reason: {power_msg}"
                )
                return

            current = current_config.get(key)
            await asyncio.to_thread(
                self._store_pending_change, key, current, val_cast, reason,
                week_start=date.today(),
            )

            await self._send(
                f"{msg_head}\n\n"
                f"💡 <b>Proposed change</b>\n"
                f"<code>{key}</code>: {current} → {val_cast}\n"
                f"Reason: {reason}\n\n"
                f"Reply /apply to accept, /skip to dismiss."
            )
        except Exception as exc:
            print(f"[self_improve] analyse_and_report failed: {exc}", file=sys.stderr)

    async def apply_suggestions(self) -> None:
        """Handler for /apply — applies the most recent pending change."""
        pending = await asyncio.to_thread(self._latest_pending_change)
        if not pending:
            await self._send("No pending suggestion to apply.")
            return
        key, val = pending["param_name"], pending["new_value"]
        if key not in ALLOWED_KEYS:
            await self._send(f"⚠️ Ignoring out-of-whitelist key: <code>{key}</code>")
            await asyncio.to_thread(self._mark_change, pending["id"], "rejected")
            return
        try:
            current_config = {k: getattr(config, k, None) for k in ALLOWED_KEYS}
            cast_val, validation_error = self._validate_change(
                key=key,
                value=val,
                current_config=current_config,
            )
            if validation_error:
                await self._send(
                    f"⚠️ Refusing to apply <code>{key}={val}</code>: {validation_error}"
                )
                await asyncio.to_thread(self._mark_change, pending["id"], "rejected")
                return
            # Save in-memory-only state that reload would wipe.
            saved_dampen = getattr(config, "PM_CONFIDENCE_DAMPEN", {})
            await asyncio.to_thread(persist_config_value, key, cast_val)
            import importlib
            importlib.reload(config)
            # Restore confidence dampening (learned weekly, not persisted to file).
            if saved_dampen:
                config.PM_CONFIDENCE_DAMPEN = saved_dampen
            await asyncio.to_thread(self._mark_change, pending["id"], "applied")
            await self._send(
                f"✅ Applied <code>{key}</code> → {cast_val} "
                f"(was {pending.get('old_value')})"
            )
        except Exception as exc:
            await self._send(f"⚠️ Apply failed: {exc}")
            await asyncio.to_thread(self._mark_change, pending["id"], "error")

    async def skip_suggestions(self) -> None:
        """Handler for /skip — discards the most recent pending change."""
        pending = await asyncio.to_thread(self._latest_pending_change)
        if not pending:
            await self._send("No pending suggestion to skip.")
            return
        await asyncio.to_thread(self._mark_change, pending["id"], "rejected")
        await self._send(
            f"❎ Skipped suggestion: <code>{pending['param_name']}</code> "
            f"→ {pending['new_value']}"
        )

    async def generate_monthly_report(self) -> None:
        """First-of-month summary — written to Obsidian + sent to Telegram."""
        try:
            stats_30 = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._gather_stats(since_days=30)
            )
            brier_30 = stats_30['brier_window']
            brier_line = f"\nBrier (30d): {brier_30:.3f}" if brier_30 is not None else ""
            body = (
                f"📅 <b>Monthly PM report</b>\n"
                f"Resolved (30d): {stats_30['resolved_window']}"
                f"{brier_line}"
            )
            await self._send(body or "No data for monthly report.")
            if self._memory is not None:
                snapshot = json.dumps(stats_30, indent=2, default=str)
                self._memory.write_monthly_review(
                    date.today().isoformat(),
                    f"# Monthly PM review — {date.today().isoformat()}\n\n"
                    f"```\n{snapshot}\n```\n",
                )
        except Exception as exc:
            print(f"[self_improve] monthly_report failed: {exc}", file=sys.stderr)

    # ── Internals ────────────────────────────────────────────────────────────
    def _gather_stats(self, since_days: int = 7) -> dict:
        """Pull everything Claude needs in one DB hit."""
        report_all = calibration.get_report(source="polymarket")
        report_win = calibration.get_report(source="polymarket", since_days=since_days)

        stats = {
            "resolved_total":       report_all.get("resolved", 0),
            "resolved_window":      report_win.get("resolved", 0),
            "brier":                report_all.get("brier"),
            "brier_window":         report_win.get("brier"),
            "by_category":          report_win.get("by_category") or [],
            "by_archetype":         report_win.get("by_archetype") or [],
            "by_resolution_style":  report_win.get("by_resolution_style") or [],
            "by_archetype_alltime": report_all.get("by_archetype") or [],
            "bins":                 report_win.get("bins") or [],
            "current_config":       {k: getattr(config, k, None) for k in ALLOWED_KEYS},
            "window_days":          since_days,
            "target_brier":         float(getattr(config, "SELF_IMPROVE_TARGET_BRIER", 0.22)),
            "markouts_by_horizon":  [],
            "markouts_by_recommendation": [],
        }

        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE status IN ('settled','invalid')) AS settled_total, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled','invalid') AND realized_pnl_usd > 0) AS wins_total, "
                    "  COALESCE(SUM(realized_pnl_usd) FILTER (WHERE status IN ('settled','invalid')), 0) AS pnl_total, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled','invalid') "
                    "    AND settled_at >= NOW() - (:d || ' days')::interval) AS settled_window, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled','invalid') "
                    "    AND settled_at >= NOW() - (:d || ' days')::interval "
                    "    AND realized_pnl_usd > 0) AS wins_window, "
                    "  COALESCE(SUM(realized_pnl_usd) FILTER (WHERE status IN ('settled','invalid') "
                    "    AND settled_at >= NOW() - (:d || ' days')::interval), 0) AS pnl_window, "
                    "  COUNT(*) FILTER (WHERE created_at >= NOW() - (:d || ' days')::interval) AS entries_window, "
                    "  COALESCE(AVG(edge_bps) FILTER (WHERE created_at >= NOW() - (:d || ' days')::interval), 0) AS avg_edge_window, "
                    "  COALESCE(AVG(confidence) FILTER (WHERE created_at >= NOW() - (:d || ' days')::interval), 0) AS avg_conf_window "
                    "FROM pm_positions "
                    "WHERE mode = :mode"
                ), {"d": str(since_days), "mode": getattr(config, "PM_MODE", "shadow")}).fetchone()
                stats["settled_total"]       = int(row[0] or 0)
                stats["wins_total"]          = int(row[1] or 0)
                stats["realized_pnl_total"]  = float(row[2] or 0.0)
                stats["settled_window"]      = int(row[3] or 0)
                stats["wins_window"]         = int(row[4] or 0)
                stats["realized_pnl_window"] = float(row[5] or 0.0)
                stats["entries_window"]      = int(row[6] or 0)
                stats["avg_edge_bps"]        = float(row[7] or 0.0)
                stats["avg_confidence"]      = float(row[8] or 0.0)

                horizon_rows = conn.execute(text(
                    "SELECT m.hours_after, "
                    "       COUNT(*) AS n_all, "
                    "       AVG(CASE WHEN m.direction_correct THEN 1.0 ELSE 0.0 END) AS rate_all, "
                    "       COUNT(*) FILTER (WHERE me.recommendation <> 'SKIP') AS n_trade, "
                    "       AVG(CASE "
                    "             WHEN me.recommendation <> 'SKIP' AND m.direction_correct THEN 1.0 "
                    "             WHEN me.recommendation <> 'SKIP' THEN 0.0 "
                    "             ELSE NULL "
                    "           END) AS rate_trade "
                    "FROM markouts m "
                    "JOIN market_evaluations me ON me.id = m.evaluation_id "
                    "WHERE me.evaluated_at >= NOW() - (:d || ' days')::interval "
                    "GROUP BY m.hours_after "
                    "ORDER BY m.hours_after"
                ), {"d": str(since_days)}).fetchall()
                stats["markouts_by_horizon"] = [
                    {
                        "hours_after": int(r[0]),
                        "n_all": int(r[1] or 0),
                        "rate_all": float(r[2]) if r[2] is not None else None,
                        "n_trade": int(r[3] or 0),
                        "rate_trade": float(r[4]) if r[4] is not None else None,
                    }
                    for r in horizon_rows
                ]

                rec_rows = conn.execute(text(
                    "SELECT me.recommendation, "
                    "       COUNT(*) AS n, "
                    "       AVG(CASE WHEN m.direction_correct THEN 1.0 ELSE 0.0 END) AS rate "
                    "FROM markouts m "
                    "JOIN market_evaluations me ON me.id = m.evaluation_id "
                    "WHERE me.evaluated_at >= NOW() - (:d || ' days')::interval "
                    "GROUP BY me.recommendation "
                    "ORDER BY me.recommendation"
                ), {"d": str(since_days)}).fetchall()
                stats["markouts_by_recommendation"] = [
                    {
                        "recommendation": str(r[0] or "UNKNOWN"),
                        "n": int(r[1] or 0),
                        "rate": float(r[2]) if r[2] is not None else None,
                    }
                    for r in rec_rows
                ]

                # ── Win/loss asymmetry ──────────────────────────────────
                asym_row = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd >= 0) AS wins, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd < 0) AS losses, "
                    "  AVG(realized_pnl_usd) FILTER (WHERE realized_pnl_usd >= 0) AS avg_win, "
                    "  AVG(realized_pnl_usd) FILTER (WHERE realized_pnl_usd < 0) AS avg_loss, "
                    "  AVG(cost_usd) FILTER (WHERE realized_pnl_usd >= 0) AS avg_win_cost, "
                    "  AVG(cost_usd) FILTER (WHERE realized_pnl_usd < 0) AS avg_loss_cost "
                    "FROM pm_positions "
                    "WHERE mode = :mode AND status IN ('settled','invalid') "
                    "  AND settled_at >= NOW() - (:d || ' days')::interval"
                ), {"mode": getattr(config, "PM_MODE", "shadow"), "d": str(since_days)}).fetchone()
                stats["win_loss_asymmetry"] = {
                    "wins": int(asym_row[0] or 0),
                    "losses": int(asym_row[1] or 0),
                    "avg_win_pnl": float(asym_row[2]) if asym_row[2] is not None else 0,
                    "avg_loss_pnl": float(asym_row[3]) if asym_row[3] is not None else 0,
                    "avg_win_cost": float(asym_row[4]) if asym_row[4] is not None else 0,
                    "avg_loss_cost": float(asym_row[5]) if asym_row[5] is not None else 0,
                }

                # ── Side analysis (YES vs NO) ──────────────────────────────
                side_rows = conn.execute(text(
                    "SELECT side, "
                    "  COUNT(*) AS n, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd >= 0) AS wins, "
                    "  COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
                    "  AVG(cost_usd) AS avg_cost, "
                    "  AVG(entry_price) AS avg_entry, "
                    "  AVG(edge_bps) AS avg_edge, "
                    "  AVG(confidence) AS avg_conf "
                    "FROM pm_positions "
                    "WHERE mode = :mode AND status IN ('settled','invalid') "
                    "  AND settled_at >= NOW() - (:d || ' days')::interval "
                    "GROUP BY side"
                ), {"mode": getattr(config, "PM_MODE", "shadow"), "d": str(since_days)}).fetchall()
                stats["side_analysis"] = [
                    {
                        "side": str(r[0]),
                        "n": int(r[1] or 0),
                        "wins": int(r[2] or 0),
                        "pnl": float(r[3] or 0),
                        "avg_cost": float(r[4]) if r[4] is not None else 0,
                        "avg_entry": float(r[5]) if r[5] is not None else 0,
                        "avg_edge": float(r[6]) if r[6] is not None else 0,
                        "avg_conf": float(r[7]) if r[7] is not None else 0,
                    }
                    for r in side_rows
                ]

                # ── Edge bucket analysis ───────────────────────────────────
                edge_rows = conn.execute(text(
                    "SELECT "
                    "  CASE "
                    "    WHEN edge_bps < 500 THEN 'low (<500bps)' "
                    "    WHEN edge_bps < 1000 THEN 'medium (500-1000bps)' "
                    "    WHEN edge_bps < 2000 THEN 'high (1000-2000bps)' "
                    "    ELSE 'extreme (2000+bps)' "
                    "  END AS bucket, "
                    "  COUNT(*) AS n, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd >= 0) AS wins, "
                    "  COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
                    "  AVG(cost_usd) AS avg_cost "
                    "FROM pm_positions "
                    "WHERE mode = :mode AND status IN ('settled','invalid') "
                    "  AND settled_at >= NOW() - (:d || ' days')::interval "
                    "GROUP BY 1 ORDER BY 1"
                ), {"mode": getattr(config, "PM_MODE", "shadow"), "d": str(since_days)}).fetchall()
                stats["edge_buckets"] = [
                    {
                        "bucket": str(r[0]),
                        "n": int(r[1] or 0),
                        "wins": int(r[2] or 0),
                        "pnl": float(r[3] or 0),
                        "avg_cost": float(r[4]) if r[4] is not None else 0,
                    }
                    for r in edge_rows
                ]

                # ── Confidence bucket analysis ─────────────────────────────
                conf_rows = conn.execute(text(
                    "SELECT "
                    "  CASE "
                    "    WHEN confidence < 0.50 THEN 'low (<0.50)' "
                    "    WHEN confidence < 0.65 THEN 'medium (0.50-0.65)' "
                    "    WHEN confidence < 0.75 THEN 'high (0.65-0.75)' "
                    "    ELSE 'very_high (0.75+)' "
                    "  END AS bucket, "
                    "  COUNT(*) AS n, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd >= 0) AS wins, "
                    "  COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
                    "  AVG(cost_usd) AS avg_cost "
                    "FROM pm_positions "
                    "WHERE mode = :mode AND status IN ('settled','invalid') "
                    "  AND settled_at >= NOW() - (:d || ' days')::interval "
                    "GROUP BY 1 ORDER BY 1"
                ), {"mode": getattr(config, "PM_MODE", "shadow"), "d": str(since_days)}).fetchall()
                stats["confidence_buckets"] = [
                    {
                        "bucket": str(r[0]),
                        "n": int(r[1] or 0),
                        "wins": int(r[2] or 0),
                        "pnl": float(r[3] or 0),
                        "avg_cost": float(r[4]) if r[4] is not None else 0,
                    }
                    for r in conf_rows
                ]

                # ── Worst losses (top 5) ───────────────────────────────────
                worst_rows = conn.execute(text(
                    "SELECT id, question, side, entry_price, cost_usd, "
                    "       realized_pnl_usd, confidence, edge_bps, market_archetype "
                    "FROM pm_positions "
                    "WHERE mode = :mode AND status IN ('settled','invalid') "
                    "  AND realized_pnl_usd < 0 "
                    "  AND settled_at >= NOW() - (:d || ' days')::interval "
                    "ORDER BY realized_pnl_usd ASC LIMIT 5"
                ), {"mode": getattr(config, "PM_MODE", "shadow"), "d": str(since_days)}).fetchall()
                stats["worst_losses"] = [
                    {
                        "id": int(r[0]),
                        "question": str(r[1])[:100],
                        "side": str(r[2]),
                        "entry_price": float(r[3]) if r[3] is not None else 0,
                        "cost_usd": float(r[4]) if r[4] is not None else 0,
                        "pnl_usd": float(r[5]) if r[5] is not None else 0,
                        "confidence": float(r[6]) if r[6] is not None else 0,
                        "edge_bps": float(r[7]) if r[7] is not None else 0,
                        "archetype": str(r[8]) if r[8] else "unknown",
                    }
                    for r in worst_rows
                ]

                # ── Correlation analysis ───────────────────────────────────
                # Is edge/confidence predictive of winning?
                corr_row = conn.execute(text(
                    "SELECT "
                    "  CORR(edge_bps, CASE WHEN realized_pnl_usd >= 0 THEN 1.0 ELSE 0.0 END) AS edge_win_corr, "
                    "  CORR(confidence, CASE WHEN realized_pnl_usd >= 0 THEN 1.0 ELSE 0.0 END) AS conf_win_corr, "
                    "  CORR(cost_usd, realized_pnl_usd) AS cost_pnl_corr "
                    "FROM pm_positions "
                    "WHERE mode = :mode AND status IN ('settled','invalid') "
                    "  AND settled_at >= NOW() - (:d || ' days')::interval"
                ), {"mode": getattr(config, "PM_MODE", "shadow"), "d": str(since_days)}).fetchone()
                stats["correlations"] = {
                    "edge_win": float(corr_row[0]) if corr_row[0] is not None else None,
                    "confidence_win": float(corr_row[1]) if corr_row[1] is not None else None,
                    "cost_pnl": float(corr_row[2]) if corr_row[2] is not None else None,
                }

                # ── Entry price zone analysis ──────────────────────────────
                zone_rows = conn.execute(text(
                    "SELECT "
                    "  CASE "
                    "    WHEN entry_price < 0.15 OR entry_price > 0.85 THEN 'extreme' "
                    "    WHEN entry_price < 0.30 OR entry_price > 0.70 THEN 'moderate' "
                    "    ELSE 'balanced' "
                    "  END AS zone, "
                    "  side, "
                    "  COUNT(*) AS n, "
                    "  COUNT(*) FILTER (WHERE realized_pnl_usd >= 0) AS wins, "
                    "  COALESCE(SUM(realized_pnl_usd), 0) AS pnl "
                    "FROM pm_positions "
                    "WHERE mode = :mode AND status IN ('settled','invalid') "
                    "  AND settled_at >= NOW() - (:d || ' days')::interval "
                    "GROUP BY 1, 2 ORDER BY pnl ASC"
                ), {"mode": getattr(config, "PM_MODE", "shadow"), "d": str(since_days)}).fetchall()
                stats["entry_price_zones"] = [
                    {
                        "zone": str(r[0]),
                        "side": str(r[1]),
                        "n": int(r[2] or 0),
                        "wins": int(r[3] or 0),
                        "pnl": float(r[4] or 0),
                    }
                    for r in zone_rows
                ]

                # Per-archetype P&L from positions
                arch_pnl_rows = conn.execute(text(
                    "SELECT market_archetype, "
                    "       COUNT(*) AS n, "
                    "       COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
                    "       COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins, "
                    "       AVG(edge_bps) AS avg_edge, "
                    "       AVG(confidence) AS avg_conf "
                    "FROM pm_positions "
                    "WHERE mode = :mode "
                    "  AND status IN ('settled', 'invalid') "
                    "  AND market_archetype IS NOT NULL "
                    "  AND settled_at >= NOW() - (:d || ' days')::interval "
                    "GROUP BY market_archetype "
                    "ORDER BY n DESC"
                ), {"mode": getattr(config, "PM_MODE", "shadow"), "d": str(since_days)}).fetchall()
                stats["archetype_pnl"] = [
                    {
                        "archetype": str(r[0]),
                        "n": int(r[1] or 0),
                        "pnl": float(r[2] or 0),
                        "wins": int(r[3] or 0),
                        "avg_edge": float(r[4]) if r[4] is not None else None,
                        "avg_conf": float(r[5]) if r[5] is not None else None,
                    }
                    for r in arch_pnl_rows
                ]
        except Exception as exc:
            print(f"[self_improve] pm stats failed: {exc}", file=sys.stderr)
            stats["settled_total"] = stats["wins_total"] = 0
            stats["settled_window"] = stats["wins_window"] = stats["entries_window"] = 0
            stats["realized_pnl_total"] = stats["realized_pnl_window"] = 0.0
            stats["avg_edge_bps"] = stats["avg_confidence"] = 0.0
        return stats

    async def _ask_claude(self, stats: dict) -> Optional[dict]:
        if self._claude is None:
            return None
        prompt = self._build_prompt(stats)
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self._claude.messages.create(
                    model      = config.CLAUDE_MODEL,
                    max_tokens = 1200,
                    system     = SYSTEM_PROMPT,
                    messages   = [{"role": "user", "content": prompt}],
                ),
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                nl = raw.find("\n")
                if nl != -1:
                    raw = raw[nl + 1:]
                if raw.endswith("```"):
                    raw = raw[:-3]
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception as exc:
            print(f"[self_improve] Claude call failed: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _build_prompt(stats: dict) -> str:
        cats = stats.get("by_category") or []
        cat_lines = "\n".join(
            f"  {c['category']}: n={c['n']}, brier={c['brier']:.3f}, "
            f"mean_pred={c['mean_pred']:.2f}, mean_actual={c['mean_actual']:.2f}"
            for c in cats if c.get("brier") is not None
        ) or "  (no resolved data per category)"

        # Per-archetype breakdown (window + all-time)
        archetypes = stats.get("by_archetype") or []
        arch_lines = "\n".join(
            f"  {a['archetype']}: n={a['n']}, brier={a['brier']:.3f}, "
            f"mean_pred={a['mean_pred']:.2f}, mean_actual={a['mean_actual']:.2f}, "
            f"mean_conf={a.get('mean_conf', 0):.2f}"
            for a in archetypes if a.get("brier") is not None
        ) or "  (no resolved data per archetype)"
        archetypes_all = stats.get("by_archetype_alltime") or []
        arch_all_lines = "\n".join(
            f"  {a['archetype']}: n={a['n']}, brier={a['brier']:.3f}, "
            f"mean_pred={a['mean_pred']:.2f}, mean_actual={a['mean_actual']:.2f}"
            for a in archetypes_all if a.get("brier") is not None
        ) or "  (no all-time archetype data)"

        # Per-resolution-style breakdown
        res_styles = stats.get("by_resolution_style") or []
        res_style_lines = "\n".join(
            f"  {r['resolution_style']}: n={r['n']}, brier={r['brier']:.3f}, "
            f"mean_pred={r['mean_pred']:.2f}, mean_actual={r['mean_actual']:.2f}"
            for r in res_styles if r.get("brier") is not None
        ) or "  (no resolution style data)"

        bins_lines = "\n".join(
            f"  [{b['lo']:.1f}-{b['hi']:.1f}]: n={b['n']}, "
            f"mean_pred={b['mean_pred'] if b['mean_pred'] is not None else 0:.2f}, "
            f"mean_actual={b['mean_actual'] if b['mean_actual'] is not None else 0:.2f}"
            for b in (stats.get("bins") or [])
        ) or "  (no reliability data in window)"
        markout_lines = "\n".join(
            f"  T+{m['hours_after']}h: all n={m['n_all']}, "
            f"all_rate={m['rate_all'] if m['rate_all'] is not None else 0:.2f}, "
            f"traded n={m['n_trade']}, "
            f"traded_rate={m['rate_trade'] if m['rate_trade'] is not None else 0:.2f}"
            for m in (stats.get("markouts_by_horizon") or [])
        ) or "  (no markout data in window)"
        rec_markout_lines = "\n".join(
            f"  {m['recommendation']}: n={m['n']}, rate={m['rate'] if m['rate'] is not None else 0:.2f}"
            for m in (stats.get("markouts_by_recommendation") or [])
        ) or "  (no recommendation markout data in window)"
        cfg_lines = "\n".join(f"  {k} = {v}" for k, v in stats["current_config"].items())

        # Current archetype edge overrides and confidence dampening
        arch_edge = getattr(config, "PM_ARCHETYPE_EDGE_OVERRIDES", {})
        arch_edge_lines = "\n".join(
            f"  {k}: {v} bps" for k, v in arch_edge.items()
        ) if arch_edge else "  (none — using global defaults)"
        conf_dampen = getattr(config, "PM_CONFIDENCE_DAMPEN", {})
        conf_dampen_lines = "\n".join(
            f"  {k}: {v}" for k, v in conf_dampen.items()
        ) if conf_dampen else "  (none — no dampening)"

        # Per-archetype P&L from positions
        arch_pnl = stats.get("archetype_pnl") or []
        arch_pnl_lines = "\n".join(
            f"  {a['archetype']}: n={a['n']}, pnl=${a['pnl']:+.2f}, "
            f"wins={a['wins']}, avg_edge={a.get('avg_edge', 0) or 0:.0f}bps, "
            f"avg_conf={a.get('avg_conf', 0) or 0:.2f}"
            for a in arch_pnl
        ) or "  (no archetype P&L data)"

        # ── Win/loss asymmetry ─────────────────────────────────────────────
        wl = stats.get("win_loss_asymmetry") or {}
        wl_lines = (
            f"  Wins: {wl.get('wins', 0)} | Losses: {wl.get('losses', 0)}\n"
            f"  Avg win P&L:  ${wl.get('avg_win_pnl', 0):+.2f}  (avg cost: ${wl.get('avg_win_cost', 0):.2f})\n"
            f"  Avg loss P&L: ${wl.get('avg_loss_pnl', 0):+.2f}  (avg cost: ${wl.get('avg_loss_cost', 0):.2f})\n"
            f"  Loss/Win ratio: {abs(wl.get('avg_loss_pnl', 1) / wl.get('avg_win_pnl', 1)):.2f}x"
            if wl.get('avg_win_pnl') else "  (no data)"
        )

        # ── Side analysis ──────────────────────────────────────────────────
        side_data = stats.get("side_analysis") or []
        side_lines = "\n".join(
            f"  {s['side']}: n={s['n']}, wins={s['wins']} ({s['wins']/s['n']*100:.0f}%), "
            f"pnl=${s['pnl']:+.2f}, avg_cost=${s['avg_cost']:.2f}, "
            f"avg_edge={s['avg_edge']:.0f}bps, avg_conf={s['avg_conf']:.2f}"
            for s in side_data if s.get('n', 0) > 0
        ) or "  (no side data)"

        # ── Edge buckets ───────────────────────────────────────────────────
        edge_data = stats.get("edge_buckets") or []
        edge_lines = "\n".join(
            f"  {e['bucket']}: n={e['n']}, wins={e['wins']} ({e['wins']/e['n']*100:.0f}%), "
            f"pnl=${e['pnl']:+.2f}, avg_cost=${e['avg_cost']:.2f}"
            for e in edge_data if e.get('n', 0) > 0
        ) or "  (no edge data)"

        # ── Confidence buckets ─────────────────────────────────────────────
        conf_data = stats.get("confidence_buckets") or []
        conf_bucket_lines = "\n".join(
            f"  {c['bucket']}: n={c['n']}, wins={c['wins']} ({c['wins']/c['n']*100:.0f}%), "
            f"pnl=${c['pnl']:+.2f}, avg_cost=${c['avg_cost']:.2f}"
            for c in conf_data if c.get('n', 0) > 0
        ) or "  (no confidence data)"

        # ── Worst losses ───────────────────────────────────────────────────
        worst = stats.get("worst_losses") or []
        worst_lines = "\n".join(
            f"  #{w['id']} {w['question'][:65]}\n"
            f"    side={w['side']}, entry={w['entry_price']:.3f}, "
            f"cost=${w['cost_usd']:.2f}, pnl=${w['pnl_usd']:+.2f}, "
            f"edge={w['edge_bps']:.0f}bps, conf={w['confidence']:.2f}, "
            f"archetype={w['archetype']}"
            for w in worst
        ) or "  (no losses in window)"

        # ── Correlations ───────────────────────────────────────────────────
        corr = stats.get("correlations") or {}
        corr_lines = (
            f"  edge → win:       {corr.get('edge_win', 'n/a')}\n"
            f"  confidence → win: {corr.get('confidence_win', 'n/a')}\n"
            f"  cost → P&L:       {corr.get('cost_pnl', 'n/a')}"
        )

        # ── Entry price zones ──────────────────────────────────────────────
        zones = stats.get("entry_price_zones") or []
        zone_lines = "\n".join(
            f"  {z['zone']}/{z['side']}: n={z['n']}, wins={z['wins']}, pnl=${z['pnl']:+.2f}"
            for z in zones
        ) or "  (no zone data)"

        return (
            f"=== STRATEGY HEALTH CHECK (last {stats['window_days']} days) ===\n\n"

            f"SUMMARY:\n"
            f"  resolved_predictions   = {stats['resolved_window']}\n"
            f"  brier_score            = {stats['brier_window']}\n"
            f"  target_brier           = {stats['target_brier']}\n"
            f"  realized_pnl_usd       = ${stats['realized_pnl_window']:.2f}\n"
            f"  settled_positions      = {stats['settled_window']}\n"
            f"  wins                   = {stats['wins_window']}\n"
            f"  entries_opened         = {stats['entries_window']}\n"
            f"  avg_edge_bps           = {stats['avg_edge_bps']:.1f}\n"
            f"  avg_confidence         = {stats['avg_confidence']:.2f}\n"

            f"\nALL-TIME:\n"
            f"  resolved_total         = {stats['resolved_total']}\n"
            f"  brier_all              = {stats['brier']}\n"
            f"  realized_pnl_total     = ${stats['realized_pnl_total']:.2f}\n"

            f"\n=== CRITICAL: WIN/LOSS ASYMMETRY ===\n{wl_lines}\n"
            f"(If loss/win ratio > 2x, bot is winning small and losing big)\n"

            f"\n=== CRITICAL: SIDE ANALYSIS (YES vs NO) ===\n{side_lines}\n"

            f"\n=== CRITICAL: EDGE ACCURACY BY MAGNITUDE ===\n{edge_lines}\n"
            f"(If extreme edge has ≤50% win rate, edge is anti-predictive at high values)\n"

            f"\n=== CRITICAL: CONFIDENCE ACCURACY BY LEVEL ===\n{conf_bucket_lines}\n"
            f"(Confidence multiplies stake. If high conf loses more, it's catastrophic)\n"

            f"\n=== CRITICAL: CORRELATIONS ===\n{corr_lines}\n"
            f"(Negative = anti-predictive. Edge-win < 0 means bigger claimed edge = more losses)\n"

            f"\n=== WORST LOSSES ===\n{worst_lines}\n"
            f"(Look for patterns: extreme edge + high conf + NO side + sports)\n"

            f"\n=== ENTRY PRICE ZONES ===\n{zone_lines}\n"

            f"\nPer-category (window):\n{cat_lines}\n"
            f"\nPer-archetype Brier (window):\n{arch_lines}\n"
            f"\nPer-archetype Brier (all-time):\n{arch_all_lines}\n"
            f"\nPer-archetype P&L (window):\n{arch_pnl_lines}\n"
            f"\nPer-resolution-style (window):\n{res_style_lines}\n"
            f"\nReliability bins (window):\n{bins_lines}\n"
            f"\nMarkouts by horizon (window):\n{markout_lines}\n"
            f"\nMarkouts by recommendation (window):\n{rec_markout_lines}\n"
            f"\nCurrent config (whitelist):\n{cfg_lines}\n"
            f"\nArchetype edge overrides (PM_ARCHETYPE_EDGE_OVERRIDES):\n{arch_edge_lines}\n"
            f"\nConfidence dampening (PM_CONFIDENCE_DAMPEN):\n{conf_dampen_lines}\n"
            f"\n=== RESPOND WITH JSON ===\n"
            f"Address ALL critical sections above. Include risk_assessment field "
            f"covering win/loss asymmetry and anti-predictive signals. "
            f"Include archetype_lessons for any archetype with n>=3."
        )

    def _format_stats_telegram(self, stats: dict) -> str:
        bw = stats['brier_window']
        ball = stats['brier']
        bw_str = f"{bw:.3f}" if bw is not None else "n/a"
        ball_str = f"{ball:.3f}" if ball is not None else "n/a"

        # Win/loss asymmetry warning
        wl = stats.get("win_loss_asymmetry") or {}
        avg_win = wl.get("avg_win_pnl", 0) or 0.01
        avg_loss = wl.get("avg_loss_pnl", 0)
        ratio = abs(avg_loss / avg_win) if avg_win else 0
        asym_warning = ""
        if ratio > 2.0:
            asym_warning = (
                f"\n⚠️ <b>ASYMMETRY ALERT</b>: avg loss (${avg_loss:+.2f}) "
                f"is {ratio:.1f}x avg win (${avg_win:+.2f})"
            )

        # Worst loss
        worst = stats.get("worst_losses") or []
        worst_line = ""
        if worst:
            w = worst[0]
            worst_line = (
                f"\n💀 Worst loss: ${w['pnl_usd']:+.2f} on "
                f"{w['question'][:50]}... "
                f"(edge={w['edge_bps']:.0f}bps, conf={w['confidence']:.2f})"
            )

        # Correlation warnings
        corr = stats.get("correlations") or {}
        corr_warning = ""
        edge_corr = corr.get("edge_win")
        conf_corr = corr.get("confidence_win")
        if edge_corr is not None and edge_corr < -0.1:
            corr_warning += f"\n📉 Edge is anti-predictive ({edge_corr:+.2f} corr)"
        if conf_corr is not None and conf_corr < -0.1:
            corr_warning += f"\n📉 Confidence is anti-predictive ({conf_corr:+.2f} corr)"

        # Side analysis
        side_data = stats.get("side_analysis") or []
        side_lines = ""
        for s in side_data:
            if s.get("n", 0) > 0:
                wr = s['wins'] / s['n'] * 100
                side_lines += (
                    f"\n  {s['side']}: {s['n']} bets, {wr:.0f}% win, "
                    f"${s['pnl']:+.2f}"
                )
        if side_lines:
            side_lines = "\n<b>By side:</b>" + side_lines

        # Archetype performance summary (top 3 by count)
        archetypes = sorted(
            (a for a in (stats.get("by_archetype") or []) if a.get("brier") is not None),
            key=lambda a: a["n"],
            reverse=True,
        )[:3]
        arch_lines = ""
        if archetypes:
            arch_lines = "\n<b>Top archetypes:</b>\n" + "\n".join(
                f"  {a['archetype']}: n={a['n']}, brier={a['brier']:.3f}"
                for a in archetypes
            )

        return (
            f"🧪 <b>Weekly self-improvement</b>\n"
            f"Resolved (7d): {stats['resolved_window']} | "
            f"(all-time: {stats['resolved_total']})\n"
            f"Brier (7d): {bw_str} | (all-time: {ball_str})\n"
            f"Realized P&L (7d): "
            f"${stats['realized_pnl_window']:+.2f}\n"
            f"Settled bets (7d): {stats['settled_window']} | wins: {stats['wins_window']}"
            f"{asym_warning}"
            f"{worst_line}"
            f"{corr_warning}"
            f"{side_lines}"
            f"{arch_lines}"
        )

    # ── DB helpers ───────────────────────────────────────────────────────────
    def _store_pending_change(self, key: str, old, new, reason: str,
                              week_start: date) -> None:
        try:
            with get_engine().begin() as conn:
                conn.execute(text(
                    "UPDATE config_change_history "
                    "SET outcome = 'superseded' "
                    "WHERE outcome = 'pending' AND suggested_by = 'claude'"
                ))
                conn.execute(text(
                    "INSERT INTO config_change_history "
                    "(param_name, old_value, new_value, reason, suggested_by, "
                    " week_start, outcome) "
                    "VALUES (:k, :o, :n, :r, 'claude', :w, 'pending')"
                ), {"k": key, "o": str(old), "n": str(new),
                    "r": reason[:4000], "w": week_start})
        except Exception as exc:
            print(f"[self_improve] store_pending failed: {exc}", file=sys.stderr)

    def _latest_pending_change(self) -> Optional[dict]:
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT id, param_name, old_value, new_value, reason "
                    "FROM config_change_history "
                    "WHERE outcome = 'pending' AND suggested_by = 'claude' "
                    "ORDER BY changed_at DESC LIMIT 1"
                )).fetchone()
                if row is None:
                    return None
                return {
                    "id":        int(row[0]),
                    "param_name": row[1],
                    "old_value":  row[2],
                    "new_value":  row[3],
                    "reason":     row[4],
                }
        except Exception as exc:
            print(f"[self_improve] pending fetch failed: {exc}", file=sys.stderr)
            return None

    def _mark_change(self, change_id: int, outcome: str) -> None:
        try:
            with get_engine().begin() as conn:
                conn.execute(text(
                    "UPDATE config_change_history SET outcome = :o WHERE id = :id"
                ), {"o": outcome, "id": change_id})
        except Exception as exc:
            print(f"[self_improve] mark_change failed: {exc}", file=sys.stderr)

    def _validate_change(
        self,
        key: str,
        value,
        current_config: dict,
    ) -> tuple[Optional[object], Optional[str]]:
        if key not in ALLOWED_KEYS:
            return None, "key not in whitelist"

        try:
            cast_value = ALLOWED_KEYS[key](value)
        except (TypeError, ValueError):
            return None, "value is not castable to the expected type"

        low, high = CONFIG_BOUNDS.get(key, (None, None))
        if low is not None and cast_value < low:
            return None, f"value is below the safety floor of {low}"
        if high is not None and cast_value > high:
            return None, f"value is above the safety ceiling of {high}"

        current = current_config.get(key)
        if current is not None:
            try:
                current_num = float(current)
                proposed_num = float(cast_value)
                if current_num != 0:
                    rel_change = abs(proposed_num - current_num) / abs(current_num)
                    if rel_change > MAX_RELATIVE_CHANGE + 1e-9:
                        return None, (
                            f"change exceeds the {int(MAX_RELATIVE_CHANGE * 100)}% weekly limit "
                            f"({current} -> {cast_value})"
                        )
            except (TypeError, ValueError):
                pass

        proposed_config = dict(current_config)
        proposed_config[key] = cast_value
        if proposed_config.get("PM_MIN_TRADE_USD") is not None and proposed_config.get("PM_MAX_TRADE_USD") is not None:
            if float(proposed_config["PM_MIN_TRADE_USD"]) > float(proposed_config["PM_MAX_TRADE_USD"]):
                return None, "PM_MIN_TRADE_USD cannot exceed PM_MAX_TRADE_USD"
        if proposed_config.get("PM_SKIP_EXISTING_DAYS") is not None and proposed_config.get("PM_MAX_DAYS_TO_END") is not None:
            if int(proposed_config["PM_SKIP_EXISTING_DAYS"]) > int(proposed_config["PM_MAX_DAYS_TO_END"]):
                return None, "PM_SKIP_EXISTING_DAYS cannot exceed PM_MAX_DAYS_TO_END"

        return cast_value, None

    # ── Archetype self-improvement ──────────────────────────────────────────
    def _update_archetype_memory(self, archetype_lessons: dict) -> None:
        """Write archetype-specific lessons to the Obsidian vault."""
        if self._memory is None or not hasattr(self._memory, "write_archetype_lessons"):
            return
        try:
            self._memory.write_archetype_lessons(archetype_lessons)
        except Exception as exc:
            print(f"[self_improve] write archetype lessons failed: {exc}",
                  file=sys.stderr)

    def _apply_confidence_dampening(self, archetype_lessons: Optional[dict]) -> None:
        """
        If Claude suggests confidence dampening for specific archetypes,
        apply it to config.PM_CONFIDENCE_DAMPEN (in-memory only, not persisted
        to config.py — this is a soft tuning that resets on restart and gets
        re-learned each week).
        """
        if not isinstance(archetype_lessons, dict):
            return
        current = getattr(config, "PM_CONFIDENCE_DAMPEN", {})
        if not isinstance(current, dict):
            current = {}
        updated = dict(current)
        changed = False
        for archetype, info in archetype_lessons.items():
            if not isinstance(info, dict):
                continue
            dampen = info.get("confidence_dampen")
            if dampen is None:
                continue
            try:
                dampen = float(dampen)
                # Safety: dampen must be in [0.5, 1.0]
                dampen = max(0.5, min(1.0, dampen))
                updated[archetype] = dampen
                changed = True
            except (TypeError, ValueError):
                continue
        if changed:
            config.PM_CONFIDENCE_DAMPEN = updated
            print(f"[self_improve] updated confidence dampening: {updated}",
                  flush=True)

    # ── Notifier glue ────────────────────────────────────────────────────────
    async def _send(self, msg: str) -> None:
        if self._notifier is not None and hasattr(self._notifier, "send"):
            try:
                await self._notifier.send(msg)
            except Exception as exc:
                print(f"[self_improve] telegram send failed: {exc}",
                      file=sys.stderr)


# ── CLI (manual test) ───────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    async def _main():
        analyser = SelfImprovementAnalyser()
        await analyser.analyse_and_report()
    asyncio.run(_main())
