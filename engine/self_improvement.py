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


SYSTEM_PROMPT = (
    "You are the post-mortem analyst for a Polymarket prediction-market bot. "
    "Every Sunday you look at the last week of resolved predictions plus "
    "their outcomes and realized P&L, and propose at most one config change "
    "to improve calibration or risk-adjusted returns. "
    "Constraints: (a) propose AT MOST ONE change, (b) change must be in the "
    "provided whitelist, (c) change must be small (±20% of current value). "
    "If no change is warranted, return an empty suggestion. "
    "Also write a short update to 'what works' and 'what doesn't work' based "
    "on the measured results — be specific, cite numbers. "
    "Output STRICT JSON only (no markdown): "
    "{\"suggestion\": {\"key\": <str|null>, \"value\": <number|null>, "
    "\"reason\": <str>}, "
    "\"what_works\": <str, ≤200 words>, "
    "\"what_doesnt\": <str, ≤200 words>, "
    "\"current_thesis\": <str, ≤200 words>}"
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
            if stats["resolved_total"] < int(getattr(config, "SELF_IMPROVE_MIN_RESOLVED", 15)):
                await self._send(
                    f"📉 <b>Weekly review skipped</b>\n"
                    f"Only {stats['resolved_total']} resolved predictions — need "
                    f"{getattr(config, 'SELF_IMPROVE_MIN_RESOLVED', 15)} for meaningful analysis."
                )
                return

            suggestion = await self._ask_claude(stats)
            if suggestion is None:
                await self._send("⚠️ Claude unavailable — no weekly review generated.")
                return

            # Update Obsidian memory immediately with what_works / what_doesnt / thesis.
            try:
                if self._memory is not None:
                    self._memory.update_strategy_memory(
                        what_works   = suggestion.get("what_works", "")[:4000],
                        what_doesnt  = suggestion.get("what_doesnt", "")[:4000],
                        current_thesis = suggestion.get("current_thesis", "")[:4000],
                    )
            except Exception as exc:
                print(f"[self_improve] memory update failed: {exc}", file=sys.stderr)

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

            try:
                val_cast = ALLOWED_KEYS[key](val)
            except (TypeError, ValueError):
                await self._send(f"{msg_head}\n\n⚠️ Claude proposed {key}={val} which is not castable.")
                return

            current = getattr(config, key, None)
            self._store_pending_change(key, current, val_cast, reason, week_start=date.today())

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
        pending = self._latest_pending_change()
        if not pending:
            await self._send("No pending suggestion to apply.")
            return
        key, val = pending["param_name"], pending["new_value"]
        if key not in ALLOWED_KEYS:
            await self._send(f"⚠️ Ignoring out-of-whitelist key: <code>{key}</code>")
            self._mark_change(pending["id"], "rejected")
            return
        try:
            cast_val = ALLOWED_KEYS[key](val)
            persist_config_value(key, cast_val)
            import importlib
            importlib.reload(config)
            self._mark_change(pending["id"], "applied")
            await self._send(
                f"✅ Applied <code>{key}</code> → {cast_val} "
                f"(was {pending.get('old_value')})"
            )
        except Exception as exc:
            await self._send(f"⚠️ Apply failed: {exc}")
            self._mark_change(pending["id"], "error")

    async def skip_suggestions(self) -> None:
        """Handler for /skip — discards the most recent pending change."""
        pending = self._latest_pending_change()
        if not pending:
            await self._send("No pending suggestion to skip.")
            return
        self._mark_change(pending["id"], "rejected")
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
            brier_30 = stats_30['brier']
            brier_line = f"\nBrier (30d): {brier_30:.3f}" if brier_30 is not None else ""
            body = (
                f"📅 <b>Monthly PM report</b>\n"
                f"Resolved (30d): {stats_30['resolved_total']}"
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
            "realized_pnl_total":   report_all.get("realized_pnl_usd") or 0.0,
            "realized_pnl_window":  report_win.get("realized_pnl_usd") or 0.0,
            "by_category":          report_win.get("by_category") or [],
            "bins":                 report_win.get("bins") or [],
            "current_config":       {k: getattr(config, k, None) for k in ALLOWED_KEYS},
            "window_days":          since_days,
        }

        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE status IN ('settled','invalid')) AS settled, "
                    "  COUNT(*) FILTER (WHERE status IN ('settled','invalid') AND realized_pnl_usd > 0) AS wins, "
                    "  COALESCE(SUM(realized_pnl_usd) FILTER (WHERE status IN ('settled','invalid') "
                    "    AND settled_at >= NOW() - (:d || ' days')::interval), 0) AS pnl_win, "
                    "  COALESCE(AVG(edge_bps), 0) AS avg_edge, "
                    "  COALESCE(AVG(confidence), 0) AS avg_conf "
                    "FROM pm_positions "
                    "WHERE mode = :mode"
                ), {"d": str(since_days), "mode": getattr(config, "PM_MODE", "shadow")}).fetchone()
                stats["settled_total"]     = int(row[0] or 0)
                stats["wins_total"]        = int(row[1] or 0)
                stats["realized_pnl_window_pm"] = float(row[2] or 0)
                stats["avg_edge_bps"]      = float(row[3] or 0)
                stats["avg_confidence"]    = float(row[4] or 0)
        except Exception as exc:
            print(f"[self_improve] pm stats failed: {exc}", file=sys.stderr)
            stats["settled_total"] = stats["wins_total"] = 0
            stats["realized_pnl_window_pm"] = stats["avg_edge_bps"] = stats["avg_confidence"] = 0.0
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
        bins_lines = "\n".join(
            f"  [{b['lo']:.1f}-{b['hi']:.1f}]: n={b['n']}, "
            f"mean_pred={b['mean_pred'] if b['mean_pred'] is not None else 0:.2f}, "
            f"mean_actual={b['mean_actual'] if b['mean_actual'] is not None else 0:.2f}"
            for b in (stats.get("bins") or [])
        )
        cfg_lines = "\n".join(f"  {k} = {v}" for k, v in stats["current_config"].items())
        return (
            f"Last {stats['window_days']} days:\n"
            f"  resolved_predictions   = {stats['resolved_window']}\n"
            f"  brier_score            = {stats['brier_window']}\n"
            f"  realized_pnl_usd       = {stats['realized_pnl_window']:.2f}\n"
            f"  settled_positions      = {stats['settled_total']}\n"
            f"  wins                   = {stats['wins_total']}\n"
            f"  avg_edge_bps           = {stats['avg_edge_bps']:.1f}\n"
            f"  avg_confidence         = {stats['avg_confidence']:.2f}\n"
            f"\nAll-time:\n"
            f"  resolved_total         = {stats['resolved_total']}\n"
            f"  brier_all              = {stats['brier']}\n"
            f"  realized_pnl_total     = {stats['realized_pnl_total']:.2f}\n"
            f"\nPer-category (window):\n{cat_lines}\n"
            f"\nReliability bins (window):\n{bins_lines}\n"
            f"\nCurrent config (whitelist):\n{cfg_lines}\n"
            f"\nRespond with your strict JSON suggestion."
        )

    def _format_stats_telegram(self, stats: dict) -> str:
        bw = stats['brier_window']
        ball = stats['brier']
        bw_str = f"{bw:.3f}" if bw is not None else "n/a"
        ball_str = f"{ball:.3f}" if ball is not None else "n/a"
        return (
            f"🧪 <b>Weekly self-improvement</b>\n"
            f"Resolved (7d): {stats['resolved_window']} | "
            f"(all-time: {stats['resolved_total']})\n"
            f"Brier (7d): {bw_str} | (all-time: {ball_str})\n"
            f"Realized P&L (7d): "
            f"${stats['realized_pnl_window']:+.2f}\n"
            f"Avg edge: {stats['avg_edge_bps']:.0f} bps | "
            f"avg conf: {stats['avg_confidence']:.2f}"
        )

    # ── DB helpers ───────────────────────────────────────────────────────────
    def _store_pending_change(self, key: str, old, new, reason: str,
                              week_start: date) -> None:
        try:
            with get_engine().begin() as conn:
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
