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
    POST /api/ask-claude          — ad-hoc Claude question about a market
    POST /api/research            — preview the research bundle for a question
    POST /api/update-config       — two-phase config change with Telegram confirm

Config flow:
    PUT /api/update-config → writes _pending_config → notifier sends Telegram
    → operator replies /confirm-config → apply_pending_config() is called by
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

import anthropic
from sqlalchemy import text

import calibration
import config
from engine import diagnostics
from config_utils import ALLOWED_CONFIG_KEYS, persist_config_value
from db.engine import get_engine
from engine.user_config import (
    DEFAULT_USER_ID,
    USER_CONFIG_BOUNDS,
    USER_CONFIG_DESCRIPTIONS,
    get_user_config,
    update_user_config,
)
from engine.learning_cadence import (
    apply_suggestion,
    list_pending_suggestions,
    skip_suggestion,
    snooze_suggestion,
)
from process_health import health as proc_health


BOT_API_HOST = os.environ.get("BOT_API_HOST", "127.0.0.1")
BOT_API_PORT = int(os.environ.get("PORT") or os.environ.get("BOT_API_PORT") or 8765)


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

        # Dedicated thread pool so API queries never get starved when the
        # default executor is saturated by scan/research/Claude calls.
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="api")

        try:
            self._claude = anthropic.Anthropic()
        except Exception as exc:
            print(f"[bot_api] Anthropic client init failed: {exc}", file=sys.stderr)
            self._claude = None

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
            "mode":            getattr(self._executor, "mode", "simulation"),
            "started_at":      ph["started_at"],
            "uptime_s":        round(ph["uptime_s"]),
            "error_count":     ph["error_count"],
            "jobs":            ph["jobs"],
            "degraded_feeds":  degraded,
        })

    async def _handle_summary(self, _request: web.Request) -> web.Response:
        stats = self._executor.get_portfolio_stats() if self._executor else {}
        brier_report = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: calibration.get_report(source="polymarket")
        )
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
            "brier":      brier_report.get("brier"),
            "resolved_predictions": brier_report.get("resolved"),
            "total_predictions":    brier_report.get("total"),
            "test_end":   getattr(config, "PM_TEST_END", None),
        })

    async def _handle_positions(self, _request: web.Request) -> web.Response:
        open_rows = self._executor.get_open_positions() if self._executor else []
        mode      = getattr(self._executor, "mode", "simulation")
        try:
            def _q():
                with get_engine().begin() as conn:
                    return conn.execute(text(
                        "SELECT id, market_id, question, category, side, shares, "
                        "       entry_price, cost_usd, claude_probability, "
                        "       ev_bps, confidence, settlement_outcome, "
                        "       settlement_price, realized_pnl_usd, created_at, "
                        "       settled_at, slug "
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
                "ev_bps":         float(r[9]) if r[9] is not None else None,
                "confidence":     float(r[10]) if r[10] is not None else None,
                "settlement_outcome": r[11],
                "settlement_price":   float(r[12]) if r[12] is not None else None,
                "realized_pnl_usd":   float(r[13]) if r[13] is not None else None,
                "created_at":     r[14].isoformat() if r[14] else None,
                "settled_at":     r[15].isoformat() if r[15] else None,
                "slug":           r[16],
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
                        "       ev_bps, recommendation, reasoning, pm_position_id, "
                        "       slug, research_sources "
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
                "ev_bps":         float(r[8]) if r[8] is not None else None,
                "recommendation": r[9],
                "reasoning":      r[10],
                "pm_position_id": r[11],
                "slug":           r[12],
                "research_sources": _parse_json_field(r[13]),
            }
            for r in rows
        ]
        return web.json_response({"evaluations": evals})

    async def _handle_brier_trend(self, request: web.Request) -> web.Response:
        source = request.query.get("source") or "polymarket"
        try:
            def _query():
                with get_engine().begin() as conn:
                    src_filter = "AND source = :src" if source != "all" else ""
                    rows = conn.execute(text(
                        f"SELECT resolved_at, probability, resolved_outcome "
                        f"FROM predictions "
                        f"WHERE resolved_at IS NOT NULL "
                        f"  AND resolved_outcome IS NOT NULL "
                        f"  {src_filter} "
                        f"ORDER BY resolved_at ASC"
                    ), {"src": source} if source != "all" else {}).fetchall()
                return rows
            rows = await asyncio.get_running_loop().run_in_executor(self._pool, _query)
        except Exception as exc:
            print(f"[bot_api] brier-trend query failed: {exc}", file=sys.stderr)
            return web.json_response({"points": []})

        points = []
        running_sum = 0.0
        for i, r in enumerate(rows, 1):
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
        report = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: calibration.get_report(
                source=None if source == "all" else source,
                since_days=since_int,
            )
        )
        return web.json_response(report)

    async def _handle_diagnostics(self, request: web.Request) -> web.Response:
        """
        Read-only diagnostic report for the dashboard + learning cadence.
        `scope` ∈ {all, traded, skipped}. 5-min cache inside engine.diagnostics.
        """
        scope = (request.query.get("scope") or "all").lower()
        if scope not in ("all", "traded", "skipped"):
            scope = "all"
        report = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: diagnostics.full_report(scope)
        )
        return web.json_response(report)

    async def _handle_config(self, _request: web.Request) -> web.Response:
        snapshot = {k: getattr(config, k, None) for k in ALLOWED_CONFIG_KEYS}
        active_mode = self._executor.mode if self._executor else "simulation"
        configured_mode = self._disk_mode or getattr(config, "PM_MODE", "simulation")
        snapshot["PM_MODE"] = active_mode
        restart_pending = active_mode != configured_mode
        return web.json_response({"config": snapshot,
                                   "active_mode": active_mode,
                                   "configured_mode": configured_mode,
                                   "restart_pending": restart_pending,
                                   "allowed_keys": list(ALLOWED_CONFIG_KEYS),
                                   "pending": self._pending_config})

    async def _handle_get_user_config(self, request: web.Request) -> web.Response:
        """Return the user's risk config with bounds + descriptions."""
        user_id = request.query.get("user_id") or DEFAULT_USER_ID
        loop = asyncio.get_running_loop()
        cfg = await loop.run_in_executor(self._pool, get_user_config, user_id)
        return web.json_response({
            "user_id":      user_id,
            "config":       cfg.to_dict(),
            "bounds":       {k: {"min": lo, "max": hi}
                             for k, (lo, hi) in USER_CONFIG_BOUNDS.items()},
            "descriptions": USER_CONFIG_DESCRIPTIONS,
        })

    async def _handle_list_suggestions(self, request: web.Request) -> web.Response:
        user_id = request.query.get("user_id") or DEFAULT_USER_ID
        include_snoozed = request.query.get("include_snoozed", "1") != "0"
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            self._pool,
            lambda: list_pending_suggestions(user_id, include_snoozed),
        )
        return web.json_response({"user_id": user_id, "suggestions": rows})

    async def _handle_apply_suggestion(self, request: web.Request) -> web.Response:
        return await self._suggestion_action(request, apply_suggestion)

    async def _handle_skip_suggestion(self, request: web.Request) -> web.Response:
        return await self._suggestion_action(request, skip_suggestion)

    async def _handle_snooze_suggestion(self, request: web.Request) -> web.Response:
        return await self._suggestion_action(request, snooze_suggestion)

    async def _suggestion_action(self, request: web.Request, fn) -> web.Response:
        try:
            suggestion_id = int(request.match_info.get("suggestion_id", "0"))
        except ValueError:
            return web.json_response({"error": "invalid suggestion id"}, status=400)

        user_id = DEFAULT_USER_ID
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get("user_id"):
            user_id = str(body["user_id"])

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._pool,
                lambda: fn(suggestion_id, user_id=user_id, resolved_by="user"),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)
        return web.json_response(result)

    async def _handle_update_user_config(self, request: web.Request) -> web.Response:
        """Validate and apply user_config changes. Takes effect immediately."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "body must be an object"}, status=400)

        user_id = str(data.pop("user_id", DEFAULT_USER_ID))
        # Allow either flat {key: value} or {"changes": {key: value}} shape.
        changes = data.get("changes") if "changes" in data else data
        if not isinstance(changes, dict) or not changes:
            return web.json_response({"error": "no changes supplied"}, status=400)

        loop = asyncio.get_running_loop()
        try:
            cfg = await loop.run_in_executor(
                self._pool,
                lambda: update_user_config(user_id, **changes),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)

        return web.json_response({
            "status":  "applied",
            "user_id": user_id,
            "config":  cfg.to_dict(),
        })

    # ── Action handlers ──────────────────────────────────────────────────────
    async def _handle_scan_now(self, _request: web.Request) -> web.Response:
        if self._analyst is None:
            return web.json_response({"error": "analyst not available"}, status=503)
        from polymarket_runner import scan_and_analyze
        async def _runner():
            try:
                summary = await scan_and_analyze(
                    limit=int(getattr(config, "PM_SCAN_LIMIT", 20)),
                    min_volume_24h=float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
                    analyst=self._analyst,
                )
                print(f"[bot_api] manual scan complete: {summary}", flush=True)
            except Exception as exc:
                print(f"[bot_api] manual scan failed: {exc}", file=sys.stderr)
        asyncio.create_task(_runner())
        return web.json_response({"status": "triggered",
                                   "triggered_at": datetime.now(timezone.utc).isoformat()})

    async def _handle_resolve_now(self, _request: web.Request) -> web.Response:
        from polymarket_runner import resolve_positions
        async def _runner():
            try:
                await resolve_positions(
                    notifier=self._notifier,
                    executor=self._executor,
                )
            except Exception as exc:
                print(f"[bot_api] manual resolve failed: {exc}", file=sys.stderr)
        asyncio.create_task(_runner())
        return web.json_response({"status": "triggered",
                                   "triggered_at": datetime.now(timezone.utc).isoformat()})

    async def _handle_ask_claude(self, request: web.Request) -> web.Response:
        if self._claude is None:
            return web.json_response({"error": "Claude client not configured"}, status=503)
        data = await request.json()
        question = str(data.get("question") or "").strip()
        market_id = data.get("market_id")
        if not question:
            return web.json_response({"error": "question required"}, status=400)
        if len(question) > 2000:
            return web.json_response({"error": "question too long (2000 char max)"}, status=400)

        context_block = ""
        if market_id:
            try:
                def _q():
                    with get_engine().begin() as conn:
                        return conn.execute(text(
                            "SELECT question, category, market_price_yes, "
                            "       claude_probability, confidence, ev_bps, "
                            "       recommendation, reasoning "
                            "FROM market_evaluations "
                            "WHERE market_id = :mid "
                            "ORDER BY evaluated_at DESC LIMIT 1"
                        ), {"mid": str(market_id)}).fetchone()
                row = await asyncio.get_running_loop().run_in_executor(self._pool, _q)
                if row:
                    ev_bps = float(row[5]) if row[5] is not None else 0.0
                    context_block = (
                        f"Market: {row[0]}\n"
                        f"Category: {row[1]}\n"
                        f"Market price YES: {row[2]:.3f}\n"
                        f"Claude p(YES): {row[3]:.3f}\n"
                        f"Confidence: {row[4]:.2f}\n"
                        f"EV (after costs): {ev_bps/100.0:+.2f}%\n"
                        f"Last call: {row[6]}\n"
                        f"Reasoning: {(row[7] or '')[:800]}\n"
                    )
            except Exception as exc:
                print(f"[bot_api] market context lookup failed: {exc}",
                      file=sys.stderr)

        system = (
            "You are a calibrated forecaster answering questions about a "
            "Polymarket prediction market. Be direct, factual, and brief. "
            "If you don't know, say so. Under 200 words."
        )
        user = (f"{context_block}\n\nQuestion: {question}"
                 if context_block else f"Question: {question}")
        try:
            answer = await self._call_claude(system, user, max_tokens=700)
            return web.json_response({"answer": answer})
        except Exception as exc:
            return web.json_response({"error": f"Claude API error: {exc}"}, status=502)

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

    async def _handle_reset_test(self, _request: web.Request) -> web.Response:
        current_mode = getattr(self._executor, "mode", "simulation") if self._executor else "simulation"
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
        if mode not in ("simulation", "live"):
            return web.json_response(
                {"error": "mode must be 'simulation' or 'live'"}, status=400)
        current = self._disk_mode or getattr(config, "PM_MODE", "simulation")
        if mode == current:
            return web.json_response({"status": "no_change", "mode": current})

        # Dashboard changes apply immediately — no Telegram confirmation round-trip.
        try:
            persist_config_value("PM_MODE", mode)
            self._disk_mode = mode
            _audit_config_change("PM_MODE", current, mode, "dashboard")
        except Exception as exc:
            return web.json_response(
                {"status": "error", "reason": str(exc)}, status=500)

        return web.json_response({
            "status": "applied",
            "key": "PM_MODE",
            "previous": current,
            "value": mode,
            "restart_required": True,
            "message": f"PM_MODE set to '{mode}'. Restart required: ./bot.sh restart",
        })

    async def _handle_update_config(self, request: web.Request) -> web.Response:
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

        # Dashboard changes apply immediately — no Telegram confirmation round-trip.
        try:
            persist_config_value(key, value)
            importlib.reload(config)
            _audit_config_change(key, current, value, "dashboard")
        except Exception as exc:
            return web.json_response(
                {"status": "error", "reason": str(exc)}, status=500)

        return web.json_response({
            "status": "applied",
            "key": key,
            "previous": current,
            "value": value,
        })

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

    # ── Helpers ──────────────────────────────────────────────────────────────
    async def _call_claude(self, system: str, user: str, max_tokens: int = 700) -> str:
        loop = asyncio.get_running_loop()
        assert self._claude is not None
        response = await loop.run_in_executor(
            self._pool,
            lambda: self._claude.messages.create(
                model=getattr(config, "CLAUDE_MODEL", "claude-sonnet-4-20250514"),
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            ),
        )
        return response.content[0].text

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
        app.router.add_get ("/api/diagnostics", self._handle_diagnostics)
        app.router.add_get ("/api/brier-trend", self._handle_brier_trend)
        app.router.add_get ("/api/config",      self._handle_config)
        app.router.add_get ("/api/user-config", self._handle_get_user_config)
        app.router.add_put ("/api/user-config", self._handle_update_user_config)
        app.router.add_get ("/api/suggestions", self._handle_list_suggestions)
        app.router.add_post("/api/suggestions/{suggestion_id}/apply",
                            self._handle_apply_suggestion)
        app.router.add_post("/api/suggestions/{suggestion_id}/skip",
                            self._handle_skip_suggestion)
        app.router.add_post("/api/suggestions/{suggestion_id}/snooze",
                            self._handle_snooze_suggestion)
        app.router.add_get ("/api/markouts",    self._handle_markouts)
        app.router.add_post("/api/scan-now",    self._handle_scan_now)
        app.router.add_post("/api/resolve-now", self._handle_resolve_now)
        app.router.add_post("/api/ask-claude",  self._handle_ask_claude)
        app.router.add_post("/api/research",    self._handle_research)
        app.router.add_post("/api/reset-test",    self._handle_reset_test)
        app.router.add_post("/api/switch-mode",   self._handle_switch_mode)
        app.router.add_post("/api/update-config", self._handle_update_config)
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
