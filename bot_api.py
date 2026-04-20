"""
Lightweight HTTP API for the dashboard — Polymarket edition.

Bound to 127.0.0.1 only. Every request must carry an X-Bot-Secret header
matching BOT_API_SECRET from the environment. If the env var is missing the
server refuses to start.

Routes:
    GET  /api/health              — liveness + mode + uptime
    GET  /api/summary             — bankroll, open count, Brier, realised P&L
    GET  /api/positions           — open + recently settled PM positions
    GET  /api/evaluations         — recent market evaluations (trade + skip)
    GET  /api/calibration         — delegates to calibration.get_report
    GET  /api/config              — current PM config values
    POST /api/scan-now            — trigger a market scan immediately
    POST /api/resolve-now         — trigger settlement sweep immediately
    POST /api/research            — preview the research bundle for a question
    POST /api/update-config       — two-phase config change with Telegram confirm

Config flow:
    PUT /api/update-config → writes _pending_config → notifier sends Telegram
    → operator replies /confirm → apply_pending_config() is called by
    the Telegram polling thread → config.py rewritten on disk + module reloaded
    → audit row in config_change_history.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from aiohttp import web

from sqlalchemy import text

import calibration
import config
from config_utils import ALLOWED_CONFIG_KEYS, persist_config_value
from db.engine import get_engine
from engine.analytics import AnalyticsEngine
from engine.user_controls import UserControls
from process_health import health as proc_health


BOT_API_HOST = "127.0.0.1"
BOT_API_PORT = 8765


def _parse_json_field(val) -> list | dict | None:
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return None


class BotAPI:
    def __init__(
        self,
        analyst,
        executor,
        notifier=None,
    ):
        self._analyst  = analyst
        self._executor = executor
        self._notifier = notifier
        self._secret   = os.environ.get("BOT_API_SECRET") or ""
        self._runner: Optional[web.AppRunner] = None
        self._started_at: Optional[datetime] = None

        self._pending_config: Optional[dict] = None
        self._disk_mode: Optional[str] = None
        self._analytics = AnalyticsEngine()
        self._controls = UserControls()

        # Dedicated thread pool so API queries never get starved when the
        # default executor is saturated by scan/research/Claude calls.
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="api")

    # ── Auth ─────────────────────────────────────────────────────────────────
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        provided = request.headers.get("X-Bot-Secret", "")
        if not self._secret or provided != self._secret:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    # ── Read handlers ────────────────────────────────────────────────────────
    async def _handle_health(self, _request: web.Request) -> web.Response:
        from feeds.feed_health_monitor import monitor as feed_monitor
        degraded = feed_monitor.get_degraded_feeds()
        ph = proc_health.snapshot()
        return web.json_response({
            "status":          "degraded" if degraded else "ok",
            "mode":            getattr(self._executor, "mode", "shadow"),
            "started_at":      ph["started_at"],
            "uptime_s":        round(ph["uptime_s"]),
            "error_count":     ph["error_count"],
            "jobs":            ph["jobs"],
            "degraded_feeds":  degraded,
        })

    async def _handle_summary(self, _request: web.Request) -> web.Response:
        stats = (await asyncio.get_running_loop().run_in_executor(
            self._pool, self._executor.get_portfolio_stats
        )) if self._executor else {}
        brier_report = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: calibration.get_report(source="polymarket")
        )
        # Compute cost-aware P&L gate: P&L must exceed estimated execution costs
        settled_n = stats.get("settled_total", 0) or 0
        avg_stake = stats.get("open_cost", 0) / max(stats.get("open_positions", 1), 1)
        cost_per_trade_bps = float(getattr(config, "GO_LIVE_COST_PER_TRADE_BPS", 150))
        estimated_costs = settled_n * avg_stake * cost_per_trade_bps / 10_000.0
        realized_pnl = stats.get("realized_pnl", 0) or 0
        pnl_after_costs = realized_pnl - estimated_costs

        return web.json_response({
            "mode":       stats.get("mode"),
            "bankroll":   stats.get("bankroll"),
            "equity":     stats.get("equity"),
            "starting_cash": stats.get("starting_cash"),
            "open_positions": stats.get("open_positions"),
            "open_cost":  stats.get("open_cost"),
            "settled_total": stats.get("settled_total"),
            "settled_wins":  stats.get("settled_wins"),
            "win_rate":   stats.get("win_rate"),
            "realized_pnl": stats.get("realized_pnl"),
            "pnl_after_costs": pnl_after_costs,
            "estimated_exec_costs": estimated_costs,
            "brier":      brier_report.get("brier"),
            "resolved_predictions": brier_report.get("resolved"),
            "total_predictions":    brier_report.get("total"),
            "test_end":   getattr(config, "PM_TEST_END", None),
        })

    async def _handle_positions(self, _request: web.Request) -> web.Response:
        open_rows = (await asyncio.get_running_loop().run_in_executor(
            self._pool, self._executor.get_open_positions
        )) if self._executor else []
        mode      = getattr(self._executor, "mode", "shadow")
        try:
            def _q():
                with get_engine().begin() as conn:
                    return conn.execute(text(
                        "SELECT id, market_id, question, category, side, shares, "
                        "       entry_price, cost_usd, claude_probability, "
                        "       edge_bps, confidence, settlement_outcome, "
                        "       settlement_price, realized_pnl_usd, created_at, "
                        "       settled_at, slug, event_slug "
                        "FROM pm_positions "
                        "WHERE mode = :m AND status IN ('settled', 'invalid') "
                        "ORDER BY settled_at DESC NULLS LAST "
                        "LIMIT 50"
                    ), {"m": mode}).fetchall()
            settled_rows = await asyncio.get_running_loop().run_in_executor(self._pool, _q)
        except Exception as exc:
            print(f"[bot_api] positions query failed: {exc}", file=sys.stderr)
            settled_rows = []
        settled = [
            {
                "id":             r[0],
                "market_id":      r[1],
                "question":       r[2],
                "category":       r[3],
                "side":           r[4],
                "shares":         float(r[5]),
                "entry_price":    float(r[6]),
                "cost_usd":       float(r[7]),
                "claude_probability": float(r[8]) if r[8] is not None else None,
                "edge_bps":       float(r[9]) if r[9] is not None else None,
                "confidence":     float(r[10]) if r[10] is not None else None,
                "settlement_outcome": r[11],
                "settlement_price":   float(r[12]) if r[12] is not None else None,
                "realized_pnl_usd":   float(r[13]) if r[13] is not None else None,
                "created_at":     r[14].isoformat() if r[14] else None,
                "settled_at":     r[15].isoformat() if r[15] else None,
                "slug":           r[16],
                "event_slug":     r[17],
            }
            for r in settled_rows
        ]
        return web.json_response({"open": open_rows, "settled": settled})

    async def _handle_evaluations(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "50"))
        try:
            def _q():
                with get_engine().begin() as conn:
                    return conn.execute(text(
                        "SELECT id, evaluated_at, market_id, question, category, "
                        "       market_price_yes, claude_probability, confidence, "
                        "       edge_bps, recommendation, reasoning, pm_position_id, "
                        "       slug, research_sources, event_slug, skip_reason "
                        "FROM market_evaluations "
                        "ORDER BY evaluated_at DESC "
                        "LIMIT :lim"
                    ), {"lim": limit}).fetchall()
            rows = await asyncio.get_running_loop().run_in_executor(self._pool, _q)
        except Exception as exc:
            print(f"[bot_api] evaluations query failed: {exc}", file=sys.stderr)
            rows = []
        evals = [
            {
                "id":             r[0],
                "evaluated_at":   r[1].isoformat() if r[1] else None,
                "market_id":      r[2],
                "question":       r[3],
                "category":       r[4],
                "market_price_yes": float(r[5]) if r[5] is not None else None,
                "claude_probability": float(r[6]) if r[6] is not None else None,
                "confidence":     float(r[7]) if r[7] is not None else None,
                "edge_bps":       float(r[8]) if r[8] is not None else None,
                "recommendation": r[9],
                "reasoning":      r[10],
                "pm_position_id": r[11],
                "slug":           r[12],
                "research_sources": _parse_json_field(r[13]),
                "event_slug":     r[14],
                "skip_reason":    r[15],
            }
            for r in rows
        ]
        return web.json_response({"evaluations": evals})

    async def _handle_brier_trend(self, request: web.Request) -> web.Response:
        source = request.query.get("source") or "polymarket"
        try:
            def _query():
                with get_engine().begin() as conn:
                    if source != "all":
                        snap_rows = conn.execute(text(
                            "SELECT captured_at, resolved, brier "
                            "FROM calibration_snapshots "
                            "WHERE source = :src "
                            "ORDER BY captured_at ASC"
                        ), {"src": source}).fetchall()
                    else:
                        snap_rows = conn.execute(text(
                            "SELECT captured_at, resolved, brier "
                            "FROM calibration_snapshots "
                            "ORDER BY captured_at ASC"
                        )).fetchall()

                    if len(snap_rows) >= 2:
                        return {"mode": "snapshots", "rows": snap_rows}

                    src_filter = "AND source = :src" if source != "all" else ""
                    pred_rows = conn.execute(text(
                        f"SELECT resolved_at, probability, resolved_outcome "
                        f"FROM predictions "
                        f"WHERE resolved_at IS NOT NULL "
                        f"  AND resolved_outcome IS NOT NULL "
                        f"  {src_filter} "
                        f"ORDER BY resolved_at ASC"
                    ), {"src": source} if source != "all" else {}).fetchall()
                return {"mode": "predictions", "rows": pred_rows}
            result = await asyncio.get_running_loop().run_in_executor(self._pool, _query)
        except Exception as exc:
            print(f"[bot_api] brier-trend query failed: {exc}", file=sys.stderr)
            return web.json_response({"points": []})

        points = []
        if result["mode"] == "snapshots":
            for r in result["rows"]:
                if r[2] is None:
                    continue
                points.append({
                    "date": r[0].isoformat() if r[0] else None,
                    "brier": round(float(r[2]), 4),
                    "n": int(r[1] or 0),
                })
        else:
            running_sum = 0.0
            for i, r in enumerate(result["rows"], 1):
                p = float(r[1])
                o = int(r[2])
                running_sum += (p - o) ** 2
                points.append({
                    "date": r[0].isoformat() if r[0] else None,
                    "brier": round(running_sum / i, 4),
                    "n": i,
                })
        return web.json_response({"points": points})

    async def _handle_calibration(self, request: web.Request) -> web.Response:
        source = request.query.get("source") or "polymarket"
        since  = request.query.get("since_days")
        since_int = int(since) if since and since.isdigit() else None
        enhanced = request.query.get("enhanced", "").lower() in ("1", "true", "yes")
        if enhanced:
            report = await asyncio.get_running_loop().run_in_executor(
                self._pool, lambda: calibration.get_enhanced_report(
                    source=None if source == "all" else source,
                )
            )
        else:
            report = await asyncio.get_running_loop().run_in_executor(
                self._pool, lambda: calibration.get_report(
                    source=None if source == "all" else source,
                    since_days=since_int,
                )
            )
        return web.json_response(report)

    async def _handle_config(self, _request: web.Request) -> web.Response:
        snapshot = {k: getattr(config, k, None) for k in ALLOWED_CONFIG_KEYS}
        active_mode = self._executor.mode if self._executor else "shadow"
        configured_mode = self._disk_mode or getattr(config, "PM_MODE", "shadow")
        snapshot["PM_MODE"] = active_mode
        restart_pending = active_mode != configured_mode
        return web.json_response({"config": snapshot,
                                   "active_mode": active_mode,
                                   "configured_mode": configured_mode,
                                   "restart_pending": restart_pending,
                                   "allowed_keys": list(ALLOWED_CONFIG_KEYS),
                                   "pending": self._pending_config})

    # ── Action handlers ──────────────────────────────────────────────────────
    async def _handle_scan_now(self, _request: web.Request) -> web.Response:
        if self._analyst is None:
            return web.json_response({"error": "analyst not available"}, status=503)
        from polymarket_runner import scan_via_subprocess
        async def _runner():
            try:
                summary = await scan_via_subprocess(
                    limit=int(getattr(config, "PM_SCAN_LIMIT", 20)),
                    min_volume_24h=float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
                )
                print(f"[bot_api] manual scan complete: {summary}", flush=True)
                # Send per-position Telegram notifications.
                if self._notifier and summary.get("opened", 0) > 0:
                    mode = getattr(self._executor, "mode", "shadow")
                    for oc in summary.get("outcomes", []):
                        if oc.get("status") != "OPENED":
                            continue
                        t = oc.get("trade", {})
                        if not t:
                            continue
                        try:
                            side = t["side"]
                            entry_c = t["entry_price"] * 100
                            prob_c = t["probability"] * 100
                            msg = (
                                f"🎯 <b>New PM position</b> [{mode}]\n"
                                f"<b>{oc['question'][:140]}</b>\n"
                                f"Bet: buy {side} at {entry_c:.1f}c\n"
                                f"Stake: ${t['stake_usd']:.2f} | "
                                f"Edge: {t['edge_bps']:.0f}bps\n"
                                f"Position: #{t['position_id']}"
                            )
                            await self._notifier.send(msg)
                        except Exception:
                            pass
            except Exception as exc:
                print(f"[bot_api] manual scan failed: {exc}", file=sys.stderr)
        asyncio.create_task(_runner())
        return web.json_response({"status": "triggered",
                                   "triggered_at": datetime.now(timezone.utc).isoformat()})

    async def _handle_resolve_now(self, _request: web.Request) -> web.Response:
        from polymarket_runner import resolve_positions
        async def _runner():
            try:
                risk_mgr = getattr(self._analyst, "risk_mgr", None) if self._analyst else None
                await resolve_positions(
                    notifier=self._notifier,
                    executor=self._executor,
                    risk_mgr=risk_mgr,
                )
            except Exception as exc:
                print(f"[bot_api] manual resolve failed: {exc}", file=sys.stderr)
        asyncio.create_task(_runner())
        return web.json_response({"status": "triggered",
                                   "triggered_at": datetime.now(timezone.utc).isoformat()})

    async def _handle_research(self, request: web.Request) -> web.Response:
        from research.fetcher import fetch_research
        data = await request.json()
        question = str(data.get("question") or "").strip()
        category = data.get("category")
        if not question:
            return web.json_response({"error": "question required"}, status=400)
        try:
            bundle = await fetch_research(question, category)
        except Exception as exc:
            return web.json_response({"error": f"research fetch failed: {exc}"}, status=502)
        return web.json_response({
            "question":    bundle.question,
            "keywords":    bundle.keywords,
            "sources":     bundle.sources,
            "prompt_block": bundle.to_prompt_block(),
        })

    async def _handle_markouts(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "50"))
        try:
            def _q():
                with get_engine().begin() as conn:
                    rows = conn.execute(text(
                        "SELECT m.id, m.evaluation_id, m.market_id, "
                        "       m.checked_at, m.hours_after, "
                        "       m.price_yes_at_check, m.price_yes_at_eval, "
                        "       m.claude_probability, m.direction_correct, "
                        "       me.question "
                        "FROM markouts m "
                        "LEFT JOIN market_evaluations me ON me.id = m.evaluation_id "
                        "ORDER BY m.checked_at DESC "
                        "LIMIT :lim"
                    ), {"lim": limit}).fetchall()

                    # Direction accuracy stats per hours_after bucket.
                    stats_rows = conn.execute(text(
                        "SELECT hours_after, "
                        "       COUNT(*) AS total, "
                        "       SUM(CASE WHEN direction_correct THEN 1 ELSE 0 END) AS correct "
                        "FROM markouts "
                        "GROUP BY hours_after "
                        "ORDER BY hours_after"
                    )).fetchall()
                    return rows, stats_rows
            rows, stats_rows = await asyncio.get_running_loop().run_in_executor(self._pool, _q)
        except Exception as exc:
            print(f"[bot_api] markouts query failed: {exc}", file=sys.stderr)
            rows, stats_rows = [], []

        markouts = [
            {
                "id":                r[0],
                "evaluation_id":     r[1],
                "market_id":         r[2],
                "checked_at":        r[3].isoformat() if r[3] else None,
                "hours_after":       r[4],
                "price_yes_at_check": float(r[5]) if r[5] is not None else None,
                "price_yes_at_eval":  float(r[6]) if r[6] is not None else None,
                "claude_probability": float(r[7]) if r[7] is not None else None,
                "direction_correct":  bool(r[8]) if r[8] is not None else None,
                "question":           r[9],
            }
            for r in rows
        ]

        accuracy = {}
        for sr in stats_rows:
            h = str(sr[0])
            accuracy[f"{h}h"] = {
                "total":   int(sr[1]),
                "correct": int(sr[2]),
                "rate":    round(int(sr[2]) / int(sr[1]), 4) if int(sr[1]) > 0 else None,
            }

        return web.json_response({"markouts": markouts, "accuracy": accuracy})

    async def _handle_risk(self, _request: web.Request) -> web.Response:
        """Return current portfolio risk state for dashboard display."""
        try:
            risk_mgr = getattr(self._analyst, "risk_mgr", None)
            if risk_mgr is None:
                return web.json_response({"error": "risk manager not available"}, status=503)
            state = await asyncio.get_running_loop().run_in_executor(
                self._pool, risk_mgr.get_risk_state
            )
            return web.json_response(state)
        except Exception as exc:
            print(f"[bot_api] risk query failed: {exc}", file=sys.stderr)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_reset_test(self, _request: web.Request) -> web.Response:
        current_mode = getattr(self._executor, "mode", "shadow") if self._executor else "shadow"
        if current_mode == "live":
            return web.json_response(
                {"error": "reset-test is disabled in live mode"}, status=403)
        def _wipe():
            with get_engine().begin() as conn:
                for tbl in ("markouts", "market_evaluations", "pm_positions", "predictions"):
                    conn.execute(text(f"DELETE FROM {tbl}"))
        await asyncio.get_running_loop().run_in_executor(self._pool, _wipe)
        return web.json_response({
            "status": "ok",
            "message": "All test data cleared (predictions, positions, evaluations, markouts).",
        })

    async def _handle_switch_mode(self, request: web.Request) -> web.Response:
        data = await request.json()
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in ("shadow", "live"):
            return web.json_response(
                {"error": "mode must be 'shadow' or 'live'"}, status=400)
        current = self._disk_mode or getattr(config, "PM_MODE", "shadow")
        if mode == current:
            return web.json_response({"status": "no_change", "mode": current})

        # Guard: live mode is not yet implemented
        if mode == "live":
            return web.json_response({
                "error": "Live mode is not yet implemented. "
                         "Complete go-live checklist first.",
            }, status=400)

        # Persist to disk but do NOT reload config — mode changes require
        # a full restart to ensure executor, thresholds, and sizing all
        # pick up the new mode consistently.
        try:
            persist_config_value("PM_MODE", mode)
            self._disk_mode = mode
            _audit_config_change("PM_MODE", current, mode, "dashboard")
        except Exception as exc:
            return web.json_response({
                "status": "error",
                "reason": str(exc),
            }, status=500)

        # Notify Telegram as audit trail (best-effort)
        if self._notifier is not None and hasattr(self._notifier, "send"):
            try:
                await self._notifier.send(
                    f"⚙️ <b>Mode switch requested via dashboard</b>\n"
                    f"<code>PM_MODE</code>: {current} → {mode}\n"
                    f"⚠️ Restart required: <code>./bot.sh restart</code>"
                )
            except Exception as exc:
                print(f"[bot_api] Telegram notify failed: {exc}", file=sys.stderr)

        return web.json_response({
            "status": "pending_restart",
            "previous": current,
            "mode": mode,
            "note": "Restart bot (./bot.sh restart) to apply mode change.",
            "message": "Mode switched. Restart required to apply (./bot.sh restart).",
        })

    async def _handle_update_config(self, request: web.Request) -> web.Response:
        """Apply a config change immediately and notify Telegram as audit trail.

        Dashboard users are already authenticated — they don't need to
        confirm their own actions on Telegram.  The /confirm flow is
        reserved for bot-initiated suggestions (self-improvement).
        """
        data = await request.json()
        key = str(data.get("key") or "")
        raw_val = data.get("value")

        if key not in ALLOWED_CONFIG_KEYS:
            return web.json_response({
                "status": "error",
                "reason": f"key '{key}' not in allowed list "
                          f"({', '.join(ALLOWED_CONFIG_KEYS)})",
            }, status=400)
        caster = ALLOWED_CONFIG_KEYS[key]
        try:
            value = caster(raw_val)
        except (TypeError, ValueError):
            return web.json_response({
                "status": "error",
                "reason": f"value must be {caster.__name__}",
            }, status=400)

        current = getattr(config, key, None)

        try:
            persist_config_value(key, value)
            importlib.reload(config)
            _audit_config_change(key, current, value, "dashboard")
        except Exception as exc:
            return web.json_response({
                "status": "error",
                "reason": str(exc),
            }, status=500)

        # Notify Telegram as audit trail (best-effort)
        if self._notifier is not None and hasattr(self._notifier, "send"):
            try:
                await self._notifier.send(
                    f"⚙️ <b>Config changed via dashboard</b>\n"
                    f"<code>{key}</code>: {current} → {value}"
                )
            except Exception:
                pass

        return web.json_response({
            "status": "applied",
            "key": key,
            "previous": current,
            "value": value,
        })

    # ── Analytics handlers ─────────────────────────────────────────────────
    async def _handle_analytics_summary(self, request: web.Request) -> web.Response:
        days = request.query.get("days")
        days_int = int(days) if days and days.isdigit() else None
        mode = getattr(self._executor, "mode", "shadow") if self._executor else "shadow"
        result = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._analytics.get_performance_summary(mode, days_int)
        )
        return web.json_response(result)

    async def _handle_analytics_attribution(self, request: web.Request) -> web.Response:
        days = request.query.get("days")
        days_int = int(days) if days and days.isdigit() else None
        mode = getattr(self._executor, "mode", "shadow") if self._executor else "shadow"
        result = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._analytics.get_pnl_attribution(mode, days_int)
        )
        return web.json_response(result)

    async def _handle_analytics_rolling(self, request: web.Request) -> web.Response:
        days = request.query.get("days")
        days_int = int(days) if days and days.isdigit() else 30
        mode = getattr(self._executor, "mode", "shadow") if self._executor else "shadow"
        result = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._analytics.get_rolling_stats(mode, days_int)
        )
        return web.json_response(result)

    async def _handle_analytics_benchmark(self, request: web.Request) -> web.Response:
        mode = getattr(self._executor, "mode", "shadow") if self._executor else "shadow"
        result = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._analytics.get_benchmark_comparison(mode)
        )
        return web.json_response(result)

    async def _handle_analytics_best_trades(self, request: web.Request) -> web.Response:
        limit = request.query.get("limit")
        limit_int = int(limit) if limit and limit.isdigit() else 10
        mode = getattr(self._executor, "mode", "shadow") if self._executor else "shadow"
        result = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._analytics.get_best_trades(mode, limit_int)
        )
        return web.json_response(result)

    async def _handle_analytics_worst_trades(self, request: web.Request) -> web.Response:
        limit = request.query.get("limit")
        limit_int = int(limit) if limit and limit.isdigit() else 10
        mode = getattr(self._executor, "mode", "shadow") if self._executor else "shadow"
        result = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._analytics.get_worst_trades(mode, limit_int)
        )
        return web.json_response(result)

    async def _handle_analytics_streaks(self, request: web.Request) -> web.Response:
        mode = getattr(self._executor, "mode", "shadow") if self._executor else "shadow"
        result = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._analytics.get_streak_analysis(mode)
        )
        return web.json_response(result)

    # ── Controls handlers ───────────────────────────────────────────────────

    async def _handle_controls_status(self, _request: web.Request) -> web.Response:
        paused, reason = await asyncio.get_running_loop().run_in_executor(
            self._pool, self._controls.is_paused)
        paused_archetypes = await asyncio.get_running_loop().run_in_executor(
            self._pool, self._controls.get_paused_archetypes)
        return web.json_response({
            "paused": paused, "pause_reason": reason,
            "paused_archetypes": paused_archetypes,
        })

    async def _handle_controls_pause(self, request: web.Request) -> web.Response:
        body = await request.json()
        reason = body.get("reason", "paused via dashboard")
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.pause_trading(reason))
        return web.json_response({"status": "paused", "reason": reason})

    async def _handle_controls_resume(self, _request: web.Request) -> web.Response:
        await asyncio.get_running_loop().run_in_executor(
            self._pool, self._controls.resume_trading)
        return web.json_response({"status": "resumed"})

    async def _handle_controls_blocked(self, _request: web.Request) -> web.Response:
        markets = await asyncio.get_running_loop().run_in_executor(
            self._pool, self._controls.get_blocked_markets)
        return web.json_response({"blocked_markets": markets})

    async def _handle_controls_block(self, request: web.Request) -> web.Response:
        body = await request.json()
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.block_market(
                body.get("market_id", ""), body.get("question", ""),
                body.get("reason", "")))
        return web.json_response({"status": "blocked"})

    async def _handle_controls_unblock(self, request: web.Request) -> web.Response:
        market_id = request.query.get("market_id", "").strip()
        if not market_id:
            return web.json_response({"error": "market_id required"}, status=400)
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.unblock_market(market_id))
        return web.json_response({"status": "unblocked", "market_id": market_id})

    async def _handle_controls_watchlist_get(self, _request: web.Request) -> web.Response:
        items = await asyncio.get_running_loop().run_in_executor(
            self._pool, self._controls.get_watchlist)
        return web.json_response({"watchlist": items})

    async def _handle_controls_watchlist_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.add_to_watchlist(
                body.get("market_id", ""), body.get("question", ""),
                body.get("notes", "")))
        return web.json_response({"status": "added"})

    async def _handle_controls_watchlist_remove(self, request: web.Request) -> web.Response:
        market_id = request.query.get("market_id", "").strip()
        if not market_id:
            return web.json_response({"error": "market_id required"}, status=400)
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.remove_from_watchlist(market_id))
        return web.json_response({"status": "removed", "market_id": market_id})

    async def _handle_controls_pause_archetype(self, request: web.Request) -> web.Response:
        body = await request.json()
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.pause_archetype(
                body.get("archetype", ""), body.get("reason", "")))
        return web.json_response({"status": "paused"})

    async def _handle_controls_resume_archetype(self, request: web.Request) -> web.Response:
        body = await request.json()
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.resume_archetype(body.get("archetype", "")))
        return web.json_response({"status": "resumed"})

    async def _handle_controls_priority_get(self, _request: web.Request) -> web.Response:
        items = await asyncio.get_running_loop().run_in_executor(
            self._pool, self._controls.get_priority_markets)
        return web.json_response({"priority_markets": items})

    async def _handle_controls_priority_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.add_priority_market(
                body.get("market_id", ""), body.get("question", ""),
                body.get("reason", "")))
        return web.json_response({"status": "added"})

    async def _handle_controls_priority_remove(self, request: web.Request) -> web.Response:
        market_id = request.query.get("market_id", "").strip()
        if not market_id:
            return web.json_response({"error": "market_id required"}, status=400)
        await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: self._controls.remove_priority_market(market_id))
        return web.json_response({"status": "removed", "market_id": market_id})

    # ── Backtesting handler ─────────────────────────────────────────────────

    async def _handle_backtest(self, request: web.Request) -> web.Response:
        """Run a backtest with custom parameters against historical evaluations."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            from backtester.pm_backtest import BacktestConfig, load_evaluations, run_backtest
            cfg = BacktestConfig(
                min_edge_bps=float(body.get("min_edge_bps", 500)),
                min_confidence=float(body.get("min_confidence", 0.55)),
                kelly_fraction=float(body.get("kelly_fraction", 0.25)),
                max_position_pct=float(body.get("max_position_pct", 0.05)),
                min_trade_usd=float(body.get("min_trade_usd", 2.0)),
                max_trade_usd=float(body.get("max_trade_usd", 25.0)),
                starting_cash=float(body.get("starting_cash", 1000.0)),
                fee_pct=float(body.get("fee_pct", 0.02)),
            )
            evals = await asyncio.get_running_loop().run_in_executor(
                self._pool, load_evaluations)
            result = await asyncio.get_running_loop().run_in_executor(
                self._pool, lambda: run_backtest(evals, cfg))
            # Serialize trades for JSON
            trades_json = [
                {
                    "question": t.question[:80],
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "cost_usd": t.cost_usd,
                    "edge": t.edge,
                    "confidence": t.confidence,
                    "resolved": t.resolved,
                    "pnl_usd": t.pnl_usd,
                }
                for t in result.get("trades", [])
            ]
            result["trades"] = trades_json
            return web.json_response(result)
        except Exception as exc:
            print(f"[bot_api] backtest failed: {exc}", file=sys.stderr)
            return web.json_response({"error": str(exc)}, status=500)

    def apply_pending_config(self) -> dict:
        if not self._pending_config:
            return {"status": "none", "reason": "no pending config change"}
        pc = self._pending_config
        key, value, previous = pc["key"], pc["value"], pc["previous"]
        try:
            persist_config_value(key, value)
            if key == "PM_MODE":
                self._disk_mode = value
                _audit_config_change(key, previous, value, "dashboard")
                self._pending_config = None
                return {
                    "status": "applied",
                    "key": key,
                    "previous": previous,
                    "value": value,
                    "restart_required": True,
                    "message": f"PM_MODE written to config.py ({previous} → {value}). "
                               f"Restart required: ./bot.sh restart",
                }
            importlib.reload(config)
            _audit_config_change(key, previous, value, "dashboard")
        except Exception as exc:
            self._pending_config = None
            return {"status": "error", "reason": str(exc)}
        self._pending_config = None
        return {"status": "applied", "key": key, "previous": previous, "value": value}

    def reject_pending_config(self) -> dict:
        if not self._pending_config:
            return {"status": "none", "reason": "no pending config change"}
        pc = self._pending_config
        self._pending_config = None
        return {"status": "rejected", "key": pc["key"], "value": pc["value"]}

    # ── Skip-decision analysis ──────────────────────────────────────────────
    async def _handle_skip_analysis(self, request: web.Request) -> web.Response:
        """
        Analyse resolved predictions that were skipped (not traded).
        Shows hypothetical P&L by skip reason category — answers the question:
        "would we have made money if we'd taken the trades we skipped?"
        """
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text("""
                    SELECT
                        me.skip_reason,
                        me.claude_probability,
                        me.market_price_yes,
                        me.edge_bps,
                        me.market_archetype,
                        me.confidence,
                        p.resolved_outcome,
                        p.probability AS pred_probability
                    FROM market_evaluations me
                    JOIN predictions p
                      ON p.id = me.prediction_id
                    WHERE me.recommendation = 'SKIP'
                      AND p.resolved_outcome IS NOT NULL
                      AND me.skip_reason IS NOT NULL
                    ORDER BY me.evaluated_at DESC
                """)).fetchall()

            if not rows:
                return web.json_response({
                    "total_skipped_resolved": 0,
                    "message": "No resolved skipped predictions yet — "
                               "wait for markets to settle.",
                })

            # Categorise skip reasons by first keyword.
            buckets: dict[str, list] = {}
            for r in rows:
                reason = r[0] or "unknown"
                # Normalise reason to a category
                if "edge" in reason and "max" in reason:
                    cat = "edge_too_high"
                elif "edge" in reason and ("min" in reason or "<" in reason):
                    cat = "edge_too_low"
                elif "confidence" in reason:
                    cat = "confidence_too_low"
                elif "stake" in reason or "min $" in reason:
                    cat = "stake_below_min"
                elif "variance" in reason or "uncertainty" in reason or "beta" in reason:
                    cat = "uncertainty_kills_edge"
                elif "resolution quality" in reason:
                    cat = "resolution_quality"
                elif "cheap NO" in reason or "NO entry" in reason:
                    cat = "cheap_no"
                elif "justification" in reason:
                    cat = "extreme_edge_unjustified"
                else:
                    cat = "other"

                claude_p = float(r[1])
                market_p = float(r[2])
                outcome = float(r[6])  # 1.0 = YES resolved, 0.0 = NO

                # Hypothetical P&L: what if we'd bet $5 at the market price?
                hyp_stake = 5.0
                if claude_p > market_p:
                    # Would have bought YES
                    entry = market_p
                    if entry > 0 and entry < 1:
                        shares = hyp_stake / entry
                        pnl = shares * (outcome - entry)
                    else:
                        pnl = 0.0
                else:
                    # Would have bought NO
                    entry = 1.0 - market_p
                    if entry > 0 and entry < 1:
                        shares = hyp_stake / entry
                        pnl = shares * ((1.0 - outcome) - entry)
                    else:
                        pnl = 0.0

                buckets.setdefault(cat, []).append({
                    "pnl": pnl,
                    "won": pnl > 0,
                    "archetype": r[4],
                })

            # Summarise each bucket.
            result: dict[str, dict] = {}
            total_hyp_pnl = 0.0
            total_resolved = 0
            for cat, trades in sorted(buckets.items()):
                wins = sum(1 for t in trades if t["won"])
                total_pnl = sum(t["pnl"] for t in trades)
                total_hyp_pnl += total_pnl
                total_resolved += len(trades)
                result[cat] = {
                    "count": len(trades),
                    "wins": wins,
                    "win_rate": round(wins / len(trades), 3) if trades else 0,
                    "hypothetical_pnl": round(total_pnl, 2),
                    "avg_pnl_per_trade": round(total_pnl / len(trades), 2) if trades else 0,
                }

            return web.json_response({
                "total_skipped_resolved": total_resolved,
                "total_hypothetical_pnl": round(total_hyp_pnl, 2),
                "verdict": (
                    "EDGE CEILING COSTS MONEY — consider loosening"
                    if result.get("edge_too_high", {}).get("hypothetical_pnl", 0) > 0
                    else "Edge ceiling is EARNING its keep — extreme edges are correctly rejected"
                ),
                "by_reason": result,
            })

        except Exception as exc:
            return web.json_response(
                {"error": f"skip analysis failed: {exc}"}, status=500,
            )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self._secret:
            raise RuntimeError(
                "BOT_API_SECRET is not set. Add it to .env (32+ random chars) "
                "and matching dashboard/.env.local before starting the bot."
            )
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get ("/api/health",      self._handle_health)
        app.router.add_get ("/api/summary",     self._handle_summary)
        app.router.add_get ("/api/positions",   self._handle_positions)
        app.router.add_get ("/api/evaluations", self._handle_evaluations)
        app.router.add_get ("/api/calibration", self._handle_calibration)
        app.router.add_get ("/api/brier-trend", self._handle_brier_trend)
        app.router.add_get ("/api/config",      self._handle_config)
        app.router.add_get ("/api/markouts",    self._handle_markouts)
        app.router.add_get ("/api/risk",        self._handle_risk)
        app.router.add_get ("/api/analytics/summary",      self._handle_analytics_summary)
        app.router.add_get ("/api/analytics/attribution",   self._handle_analytics_attribution)
        app.router.add_get ("/api/analytics/rolling",       self._handle_analytics_rolling)
        app.router.add_get ("/api/analytics/benchmark",     self._handle_analytics_benchmark)
        app.router.add_get ("/api/analytics/best-trades",   self._handle_analytics_best_trades)
        app.router.add_get ("/api/analytics/worst-trades",  self._handle_analytics_worst_trades)
        app.router.add_get ("/api/analytics/streaks",       self._handle_analytics_streaks)
        app.router.add_post("/api/scan-now",    self._handle_scan_now)
        app.router.add_post("/api/resolve-now", self._handle_resolve_now)
        app.router.add_post("/api/research",    self._handle_research)
        app.router.add_post("/api/reset-test",    self._handle_reset_test)
        app.router.add_post("/api/switch-mode",   self._handle_switch_mode)
        app.router.add_post("/api/update-config", self._handle_update_config)
        # Controls
        app.router.add_post  ("/api/controls/pause",            self._handle_controls_pause)
        app.router.add_post  ("/api/controls/resume",           self._handle_controls_resume)
        app.router.add_get   ("/api/controls/status",           self._handle_controls_status)
        app.router.add_post  ("/api/controls/block-market",     self._handle_controls_block)
        app.router.add_delete("/api/controls/block-market",     self._handle_controls_unblock)
        app.router.add_get   ("/api/controls/blocked-markets",  self._handle_controls_blocked)
        app.router.add_post  ("/api/controls/pause-archetype",  self._handle_controls_pause_archetype)
        app.router.add_post  ("/api/controls/resume-archetype", self._handle_controls_resume_archetype)
        app.router.add_post  ("/api/controls/watchlist",        self._handle_controls_watchlist_add)
        app.router.add_delete("/api/controls/watchlist",        self._handle_controls_watchlist_remove)
        app.router.add_get   ("/api/controls/watchlist",        self._handle_controls_watchlist_get)
        app.router.add_post  ("/api/controls/priority",         self._handle_controls_priority_add)
        app.router.add_delete("/api/controls/priority",         self._handle_controls_priority_remove)
        app.router.add_get   ("/api/controls/priority",         self._handle_controls_priority_get)
        # Backtesting
        app.router.add_post("/api/backtest",                self._handle_backtest)
        # Analysis
        app.router.add_get ("/api/skip-analysis",           self._handle_skip_analysis)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, BOT_API_HOST, BOT_API_PORT)
        await site.start()
        self._started_at = datetime.now(timezone.utc)
        print(
            f"[bot_api] Listening on http://{BOT_API_HOST}:{BOT_API_PORT} "
            f"(authenticated via X-Bot-Secret; allowed config keys: "
            f"{', '.join(ALLOWED_CONFIG_KEYS)})",
            flush=True,
        )
        while True:
            await asyncio.sleep(3600)



def _audit_config_change(key: str, old, new, source: str) -> None:
    try:
        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO config_change_history "
                "(param_name, old_value, new_value, reason, suggested_by, outcome) "
                "VALUES (:k, :o, :n, :r, :s, 'applied')"
            ), {
                "k": key, "o": str(old), "n": str(new),
                "r": "dashboard /api/update-config", "s": source,
            })
    except Exception as exc:
        print(f"[bot_api] audit log write failed: {exc}", file=sys.stderr)
