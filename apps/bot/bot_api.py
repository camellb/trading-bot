"""
Lightweight HTTP API for the dashboard - Polymarket edition.

Bound to 127.0.0.1 only. Every request must carry an X-Bot-Secret header
matching BOT_API_SECRET from the environment. If the env var is missing the
server refuses to start.

Routes:
    GET  /api/health              - liveness + mode + uptime
    GET  /api/summary             - bankroll, open count, Brier, realised P&L
    GET  /api/positions           - open + recently settled PM positions
    GET  /api/evaluations         - recent market evaluations (trade + skip)
    GET  /api/calibration         - delegates to calibration.get_report
    GET  /api/config              - current PM config values
    POST /api/scan-now            - trigger a market scan immediately
    POST /api/resolve-now         - trigger settlement sweep immediately
    POST /api/update-config       - two-phase config change with Telegram confirm

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
    USER_CONFIG_BOUNDS,
    USER_CONFIG_DESCRIPTIONS,
    get_user_config,
    get_user_polymarket_creds,
    get_user_telegram_creds,
    is_admin as _user_is_admin,
    set_user_polymarket_creds,
    set_user_telegram_creds,
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
        notifier=None,
    ):
        # Multi-tenant API: every user-scoped route constructs a per-request
        # PMExecutor from the X-User-Id header. No process-global executor.
        self._analyst  = analyst
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
    # Paths exempt from X-Bot-Secret - platform healthchecks (Railway, k8s)
    # cannot send a custom header, and the /api/health body carries no
    # sensitive data.
    _AUTH_EXEMPT_PATHS = frozenset({"/api/health"})

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if request.path in self._AUTH_EXEMPT_PATHS:
            return await handler(request)
        provided = request.headers.get("X-Bot-Secret", "")
        if not self._secret or provided != self._secret:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    # ── Per-user executor (SaaS multi-tenancy) ───────────────────────────────
    def _user_id_from(self, request: web.Request) -> Optional[str]:
        """
        Pull the caller's user_id out of the X-User-Id header. Returns None
        when the header is missing or blank - handlers decide whether that
        is a 401 (user-scoped endpoint) or a legacy fallback.
        """
        uid = (request.headers.get("X-User-Id") or "").strip()
        return uid or None

    def _user_executor(self, request: web.Request):
        """
        Construct a per-user PMExecutor from the request's X-User-Id.
        Returns None if the header is missing - the caller is expected to
        respond with 401 in that case.
        """
        from execution.pm_executor import PMExecutor
        uid = self._user_id_from(request)
        if not uid:
            return None
        try:
            return PMExecutor(uid)
        except Exception as exc:
            print(f"[bot_api] PMExecutor({uid}) failed: {exc}", file=sys.stderr)
            return None

    # ── Read handlers ────────────────────────────────────────────────────────
    async def _handle_health(self, _request: web.Request) -> web.Response:
        from feeds.feed_health_monitor import monitor as feed_monitor
        degraded = feed_monitor.get_degraded_feeds()
        ph = proc_health.snapshot()
        return web.json_response({
            # /health is process-scoped - report the configured disk mode.
            # Per-user mode is surfaced via /api/summary, not /api/health.
            "status":          "degraded" if degraded else "ok",
            "mode":            self._disk_mode or getattr(config, "PM_MODE", "simulation"),
            "started_at":      ph["started_at"],
            "uptime_s":        round(ph["uptime_s"]),
            "error_count":     ph["error_count"],
            "jobs":            ph["jobs"],
            "degraded_feeds":  degraded,
        })

    async def _handle_summary(self, request: web.Request) -> web.Response:
        executor = self._user_executor(request)
        if executor is None:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        stats = executor.get_portfolio_stats()
        uid = executor.user_id
        brier_report = await asyncio.get_running_loop().run_in_executor(
            self._pool,
            lambda: calibration.get_report(source="polymarket", user_id=uid),
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

    async def _handle_positions(self, request: web.Request) -> web.Response:
        executor = self._user_executor(request)
        if executor is None:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        if not executor.ready:
            # Not-yet-onboarded users see an empty portfolio, never another
            # user's positions leaked through.
            return web.json_response({"open": [], "settled": []})
        open_rows = executor.get_open_positions()
        mode      = executor.mode
        uid       = executor.user_id
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
                        "WHERE user_id = :uid AND mode = :m "
                        "  AND status IN ('settled', 'invalid') "
                        "ORDER BY settled_at DESC NULLS LAST "
                        "LIMIT 50"
                    ), {"uid": uid, "m": mode}).fetchall()
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
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        # Evaluations are shared work (one Claude call per market) but users
        # must only see rows produced after they joined - see SaaS doctrine.
        from engine.user_config import get_user_join_time
        loop = asyncio.get_running_loop()
        join_time = await loop.run_in_executor(self._pool, get_user_join_time, user_id)
        try:
            def _q():
                with get_engine().begin() as conn:
                    return conn.execute(text(
                        "SELECT id, evaluated_at, market_id, question, category, "
                        "       market_price_yes, claude_probability, confidence, "
                        "       ev_bps, recommendation, reasoning, pm_position_id, "
                        "       slug, research_sources, reasoning_short "
                        "FROM market_evaluations "
                        "WHERE (:since IS NULL OR evaluated_at >= :since) "
                        "ORDER BY evaluated_at DESC "
                        "LIMIT :lim"
                    ), {"lim": limit, "since": join_time}).fetchall()
            rows = await loop.run_in_executor(self._pool, _q)
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
                "reasoning_short": r[14],
            }
            for r in rows
        ]
        return web.json_response({"evaluations": evals})

    async def _handle_brier_trend(self, request: web.Request) -> web.Response:
        # User-scoped running Brier on this user's settled positions. Brier is
        # computed on the chosen side so it matches the Brier everywhere else
        # in the app (calibration card, learning cycles).
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        try:
            def _query():
                with get_engine().begin() as conn:
                    return conn.execute(text(
                        "SELECT settled_at, claude_probability, side, "
                        "       CASE WHEN settlement_outcome = side THEN 1 "
                        "            ELSE 0 END AS correct "
                        "FROM pm_positions "
                        "WHERE user_id = :uid "
                        "  AND settled_at IS NOT NULL "
                        "  AND claude_probability IS NOT NULL "
                        "  AND settlement_outcome IN ('YES', 'NO') "
                        "ORDER BY settled_at ASC"
                    ), {"uid": user_id}).fetchall()
            rows = await asyncio.get_running_loop().run_in_executor(self._pool, _query)
        except Exception as exc:
            print(f"[bot_api] brier-trend query failed: {exc}", file=sys.stderr)
            return web.json_response({"points": []})

        points = []
        running_sum = 0.0
        for i, r in enumerate(rows, 1):
            p_yes = float(r[1])
            side = (r[2] or "YES").upper()
            # Brier is computed on the chosen side: p_yes if we bet YES, else 1-p_yes.
            p = p_yes if side == "YES" else (1.0 - p_yes)
            o = int(r[3])
            running_sum += (p - o) ** 2
            points.append({
                "date": r[0].isoformat() if r[0] else None,
                "brier": round(running_sum / i, 4),
                "n": i,
            })
        return web.json_response({"points": points})

    async def _handle_calibration(self, request: web.Request) -> web.Response:
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        source = request.query.get("source") or "polymarket"
        since  = request.query.get("since_days")
        since_int = int(since) if since and since.isdigit() else None
        report = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: calibration.get_report(
                source=None if source == "all" else source,
                since_days=since_int,
                user_id=user_id,
            )
        )
        return web.json_response(report)

    async def _handle_diagnostics(self, request: web.Request) -> web.Response:
        """
        Read-only diagnostic report for the dashboard + learning cadence.
        `scope` ∈ {all, traded, skipped}. 5-min cache inside engine.diagnostics.

        The dashboard caller must supply X-User-Id so the bankroll series is
        scoped to that user. Forecaster-level metrics remain global (they are
        admin-only) but the user-facing dashboard only consumes the
        user-scoped slice.
        """
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        scope = (request.query.get("scope") or "all").lower()
        if scope not in ("all", "traded", "skipped"):
            scope = "all"
        report = await asyncio.get_running_loop().run_in_executor(
            self._pool,
            lambda: diagnostics.full_report(scope, user_id=user_id),
        )
        return web.json_response(report)

    async def _handle_config(self, request: web.Request) -> web.Response:
        snapshot = {k: getattr(config, k, None) for k in ALLOWED_CONFIG_KEYS}
        # Per-user mode comes from user_config when X-User-Id is present;
        # the legacy scheduler mode is the fallback for internal tooling.
        user_id = self._user_id_from(request)
        active_mode: Optional[str] = None
        if user_id:
            try:
                user_cfg = get_user_config(user_id)
                active_mode = user_cfg.mode
            except Exception:
                active_mode = None
        configured_mode = self._disk_mode or getattr(config, "PM_MODE", "simulation")
        if not active_mode:
            active_mode = configured_mode
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
        user_id = self._user_id_from(request) or request.query.get("user_id")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
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
        user_id = self._user_id_from(request) or request.query.get("user_id")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        include_snoozed = request.query.get("include_snoozed", "1") != "0"
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            self._pool,
            lambda: list_pending_suggestions(user_id, include_snoozed),
        )
        return web.json_response({"user_id": user_id, "suggestions": rows})

    async def _handle_list_learning_reports(self,
                                            request: web.Request) -> web.Response:
        caller_id = self._user_id_from(request)
        if not caller_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        try:
            limit = int(request.query.get("limit", "10"))
        except ValueError:
            limit = 10
        # include_admin=1 flips the reasoning-bearing variant on, but only for
        # verified admins. Non-admins asking for it silently get the user view.
        want_admin = request.query.get("include_admin", "0") == "1"
        loop = asyncio.get_running_loop()
        is_admin = bool(await loop.run_in_executor(
            self._pool, _user_is_admin, caller_id,
        ))
        include_admin = bool(want_admin and is_admin)

        # Admins can inspect any user's reports via ?user_id=... ; non-admins
        # are silently pinned to their own id regardless of query param.
        target_user_id = request.query.get("user_id") or ""
        if target_user_id and is_admin:
            user_id = target_user_id
        else:
            user_id = caller_id

        from engine.review_report import list_learning_reports
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            self._pool,
            lambda: list_learning_reports(
                user_id=user_id, limit=limit, include_admin=include_admin,
            ),
        )
        return web.json_response({
            "user_id":       user_id,
            "include_admin": include_admin,
            "reports":       rows,
        })

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

        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id = self._user_id_from(request)
        if not user_id and isinstance(body, dict) and body.get("user_id"):
            user_id = str(body["user_id"])
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)

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

        user_id = self._user_id_from(request) or str(data.pop("user_id", "") or "")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        data.pop("user_id", None)
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

    async def _handle_get_telegram_config(self, request: web.Request) -> web.Response:
        """Return whether the user has Telegram creds configured. Never echoes
        the token or chat_id back - the dashboard only needs the boolean."""
        user_id = self._user_id_from(request) or request.query.get("user_id")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        loop = asyncio.get_running_loop()
        creds = await loop.run_in_executor(
            self._pool, get_user_telegram_creds, user_id,
        )
        return web.json_response({
            "user_id":   user_id,
            "configured": creds is not None,
        })

    async def _handle_put_telegram_config(self, request: web.Request) -> web.Response:
        """Persist per-user Telegram bot_token + chat_id. Empty strings clear."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "body must be an object"}, status=400)

        user_id   = self._user_id_from(request) or str(data.get("user_id") or "")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        bot_token = data.get("bot_token")
        chat_id   = data.get("chat_id")
        if bot_token is not None and not isinstance(bot_token, str):
            return web.json_response({"error": "bot_token must be string or null"}, status=400)
        if chat_id is not None and not isinstance(chat_id, str):
            return web.json_response({"error": "chat_id must be string or null"}, status=400)

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                self._pool,
                lambda: set_user_telegram_creds(user_id, bot_token, chat_id),
            )
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)

        if self._notifier is not None and hasattr(self._notifier, "invalidate_creds"):
            try:
                self._notifier.invalidate_creds(user_id)
            except Exception as exc:
                print(f"[bot_api] notifier invalidate_creds failed: {exc}",
                      file=sys.stderr)

        creds = get_user_telegram_creds(user_id)
        return web.json_response({
            "status":     "applied",
            "user_id":    user_id,
            "configured": creds is not None,
        })

    async def _handle_reveal_telegram_config(self, request: web.Request) -> web.Response:
        """Return the user's saved bot_token and chat_id so the settings page can
        prefill the inputs when the user clicks 'Reveal'. Gated by the same
        session-auth the PUT endpoint uses - reading back creds the user just
        saved is no additional exposure beyond what they've already provided."""
        user_id = self._user_id_from(request) or request.query.get("user_id")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        loop = asyncio.get_running_loop()
        creds = await loop.run_in_executor(
            self._pool, get_user_telegram_creds, user_id,
        )
        if creds is None:
            return web.json_response({
                "configured": False,
                "bot_token":  "",
                "chat_id":    "",
            })
        return web.json_response({
            "configured": True,
            "bot_token":  creds[0] or "",
            "chat_id":    creds[1] or "",
        })

    async def _handle_telegram_test(self, request: web.Request) -> web.Response:
        """Send a one-off test message so the user can verify Telegram hookup."""
        user_id = self._user_id_from(request) or request.query.get("user_id")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        if self._notifier is None:
            return web.json_response({"error": "notifier not initialised"},
                                      status=503)
        creds = get_user_telegram_creds(user_id)
        if creds is None:
            return web.json_response(
                {"error": "Save your Telegram bot token and chat ID first."},
                status=400,
            )
        message = (
            "<b>Delfi test message</b>\nYou're connected. "
            "You'll receive positions, resolutions, and summaries here."
        )
        if hasattr(self._notifier, "send_checked"):
            ok, detail = await self._notifier.send_checked(user_id, message)
            if not ok:
                return web.json_response({"error": detail}, status=502)
            return web.json_response({"status": "sent", "user_id": user_id})
        # Legacy fallback - old notifier without checked send
        try:
            await self._notifier.send(user_id, message)
        except Exception as exc:
            return web.json_response(
                {"error": f"send failed: {exc}"}, status=502,
            )
        return web.json_response({"status": "sent", "user_id": user_id})

    async def _handle_get_polymarket_config(self, request: web.Request) -> web.Response:
        """Return which Polymarket credential fields the user has filled.
        Never echoes api_key/api_secret/passphrase back - the dashboard only
        needs the boolean flags + wallet_address (non-sensitive)."""
        user_id = self._user_id_from(request) or request.query.get("user_id")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        loop = asyncio.get_running_loop()
        creds = await loop.run_in_executor(
            self._pool, get_user_polymarket_creds, user_id,
        )
        required_ok = bool(creds.get("api_key")
                           and creds.get("api_secret")
                           and creds.get("wallet_address"))
        return web.json_response({
            "user_id":           user_id,
            "api_key_set":       bool(creds.get("api_key")),
            "api_secret_set":    bool(creds.get("api_secret")),
            "passphrase_set":    bool(creds.get("passphrase")),
            "wallet_address":    creds.get("wallet_address"),
            "ready_for_live":    required_ok,
        })

    async def _handle_put_polymarket_config(self, request: web.Request) -> web.Response:
        """Persist per-user Polymarket credentials. Empty string → NULL;
        missing key → untouched. api_key/api_secret/wallet_address are
        required for live mode; passphrase is optional."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "body must be an object"}, status=400)

        user_id = self._user_id_from(request) or str(data.get("user_id") or "")
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)

        def _str_or_none(key: str):
            v = data.get(key, None)
            if v is None:
                return None
            if not isinstance(v, str):
                return ValueError(f"{key} must be string or null")
            return v

        api_key        = _str_or_none("api_key")
        api_secret     = _str_or_none("api_secret")
        passphrase     = _str_or_none("passphrase")
        wallet_address = _str_or_none("wallet_address")
        for v, name in ((api_key, "api_key"), (api_secret, "api_secret"),
                         (passphrase, "passphrase"), (wallet_address, "wallet_address")):
            if isinstance(v, ValueError):
                return web.json_response({"error": str(v)}, status=400)

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                self._pool,
                lambda: set_user_polymarket_creds(
                    user_id,
                    api_key=api_key,
                    api_secret=api_secret,
                    passphrase=passphrase,
                    wallet_address=wallet_address,
                ),
            )
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)

        creds = get_user_polymarket_creds(user_id)
        required_ok = bool(creds.get("api_key")
                           and creds.get("api_secret")
                           and creds.get("wallet_address"))
        return web.json_response({
            "status":         "applied",
            "user_id":        user_id,
            "api_key_set":    bool(creds.get("api_key")),
            "api_secret_set": bool(creds.get("api_secret")),
            "passphrase_set": bool(creds.get("passphrase")),
            "wallet_address": creds.get("wallet_address"),
            "ready_for_live": required_ok,
        })

    # ── Admin handlers ───────────────────────────────────────────────────────
    async def _require_admin(self, request: web.Request) -> Optional[str]:
        """Return caller user_id if they're flagged admin, else None.
        Handlers should 401 / 403 on a None return."""
        uid = self._user_id_from(request)
        if not uid:
            return None
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(self._pool, _user_is_admin, uid)
        return uid if ok else None

    async def _handle_admin_users(self, request: web.Request) -> web.Response:
        """List every user with basic onboarding + activity stats. Admin only."""
        admin_uid = await self._require_admin(request)
        if not admin_uid:
            return web.json_response({"error": "admin access required"},
                                      status=403)
        loop = asyncio.get_running_loop()
        try:
            def _q():
                with get_engine().begin() as conn:
                    return conn.execute(text(
                        "SELECT uc.user_id, uc.display_name, uc.mode, "
                        "       uc.starting_cash, uc.onboarded_at, uc.is_admin, "
                        "       uc.bot_enabled, "
                        "       uc.subscription_status, uc.subscription_plan, "
                        "       au.email, au.created_at, "
                        "       (SELECT COUNT(*) FROM pm_positions p "
                        "          WHERE p.user_id = uc.user_id) AS total_positions, "
                        "       (SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                        "          FROM pm_positions p "
                        "          WHERE p.user_id = uc.user_id "
                        "            AND p.status = 'settled') AS realized_pnl "
                        "FROM user_config uc "
                        "LEFT JOIN auth.users au ON au.id::text = uc.user_id "
                        "ORDER BY au.created_at DESC NULLS LAST "
                        "LIMIT 500"
                    )).fetchall()
            rows = await loop.run_in_executor(self._pool, _q)
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)
        users = [
            {
                "user_id":             str(r[0]),
                "display_name":        r[1],
                "mode":                r[2],
                "starting_cash":       float(r[3]) if r[3] is not None else None,
                "onboarded_at":        r[4].isoformat() if r[4] else None,
                "is_admin":            bool(r[5]),
                "bot_enabled":         bool(r[6]),
                "subscription_status": r[7],
                "subscription_plan":   r[8],
                "email":               r[9],
                "created_at":          r[10].isoformat() if r[10] else None,
                "total_positions":     int(r[11] or 0),
                "realized_pnl":        float(r[12] or 0.0),
            }
            for r in rows
        ]
        return web.json_response({"users": users})

    async def _handle_admin_overview(self, request: web.Request) -> web.Response:
        """Aggregate cross-user stats, recent alerts, and activity feed."""
        admin_uid = await self._require_admin(request)
        if not admin_uid:
            return web.json_response({"error": "admin access required"},
                                      status=403)
        loop = asyncio.get_running_loop()
        try:
            def _q():
                with get_engine().begin() as conn:
                    totals = conn.execute(text(
                        "SELECT "
                        "  (SELECT COUNT(*) FROM user_config "
                        "     WHERE onboarded_at IS NOT NULL) AS onboarded, "
                        "  (SELECT COUNT(*) FROM user_config) AS total, "
                        "  (SELECT COUNT(*) FROM user_config "
                        "     WHERE subscription_status = 'active') AS active_subs, "
                        "  (SELECT COALESCE(SUM(starting_cash), 0) FROM user_config "
                        "     WHERE subscription_status = 'active' "
                        "       AND bot_enabled = TRUE) AS bankroll_um, "
                        "  (SELECT COUNT(*) FROM pm_positions "
                        "     WHERE status = 'open') AS open_positions, "
                        "  (SELECT COUNT(*) FROM pm_positions "
                        "     WHERE created_at >= NOW() - INTERVAL '24 hours') "
                        "    AS trades_24h, "
                        "  (SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                        "     FROM pm_positions WHERE status = 'settled') "
                        "    AS realized_pnl, "
                        "  (SELECT COUNT(*) FROM market_evaluations) "
                        "    AS total_evaluations, "
                        "  (SELECT COUNT(*) FROM user_config "
                        "     WHERE subscription_status = 'active' "
                        "       AND subscription_plan = 'monthly') AS plan_monthly, "
                        "  (SELECT COUNT(*) FROM user_config "
                        "     WHERE subscription_status = 'active' "
                        "       AND subscription_plan = 'annual') AS plan_annual, "
                        "  (SELECT COUNT(*) FROM user_config "
                        "     WHERE subscription_status = 'past_due') AS past_due, "
                        "  (SELECT COUNT(*) FROM user_config "
                        "     WHERE subscription_status = 'canceled') AS canceled, "
                        "  (SELECT COUNT(*) FROM auth.users "
                        "     WHERE created_at >= NOW() - INTERVAL '7 days') "
                        "    AS new_signups_7d, "
                        "  (SELECT COUNT(*) FROM user_config "
                        "     WHERE subscription_status = 'active' "
                        "       AND subscription_started_at "
                        "           >= NOW() - INTERVAL '7 days') "
                        "    AS new_subs_7d, "
                        "  (SELECT COUNT(*) FROM pm_positions "
                        "     WHERE status = 'settled' "
                        "       AND settled_at >= NOW() - INTERVAL '24 hours') "
                        "    AS settles_24h, "
                        "  (SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                        "     FROM pm_positions "
                        "     WHERE status = 'settled' "
                        "       AND settled_at >= NOW() - INTERVAL '24 hours') "
                        "    AS realized_24h"
                    )).fetchone()

                    alert_rows = conn.execute(text(
                        "SELECT timestamp, feed_name, state, detail "
                        "FROM feed_health_log "
                        "WHERE state IN ('down', 'degraded') "
                        "  AND timestamp >= NOW() - INTERVAL '1 hour' "
                        "ORDER BY timestamp DESC "
                        "LIMIT 10"
                    )).fetchall()

                    signup_rows = conn.execute(text(
                        "SELECT au.created_at, au.email "
                        "FROM auth.users au "
                        "WHERE au.created_at >= NOW() - INTERVAL '24 hours' "
                        "ORDER BY au.created_at DESC "
                        "LIMIT 15"
                    )).fetchall()

                    settle_rows = conn.execute(text(
                        "SELECT p.settled_at, p.question, p.realized_pnl_usd, p.user_id "
                        "FROM pm_positions p "
                        "WHERE p.status = 'settled' "
                        "  AND p.settled_at >= NOW() - INTERVAL '24 hours' "
                        "ORDER BY p.settled_at DESC "
                        "LIMIT 15"
                    )).fetchall()

                    event_rows = conn.execute(text(
                        "SELECT timestamp, event_type, description, source "
                        "FROM event_log "
                        "WHERE timestamp >= NOW() - INTERVAL '24 hours' "
                        "  AND severity >= 2 "
                        "ORDER BY timestamp DESC "
                        "LIMIT 15"
                    )).fetchall()

                    return (totals, alert_rows, signup_rows, settle_rows, event_rows)

            totals, alert_rows, signup_rows, settle_rows, event_rows = (
                await loop.run_in_executor(self._pool, _q)
            )
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)

        alerts = [
            {
                "level":     "warn" if r[2] == "down" else "info",
                "title":     f"{r[1]} · {r[2]}",
                "detail":    r[3] or "",
                "timestamp": r[0].isoformat() if r[0] else None,
            }
            for r in alert_rows
        ]

        activity: list[dict] = []
        for r in signup_rows:
            activity.append({
                "timestamp":   r[0].isoformat() if r[0] else None,
                "kind":        "signup",
                "description": f"New signup - {r[1] or 'unknown'}",
            })
        for r in settle_rows:
            pnl = float(r[2] or 0.0)
            sign = "+" if pnl > 0 else ""
            activity.append({
                "timestamp":   r[0].isoformat() if r[0] else None,
                "kind":        "settle",
                "description": f"Settled: {(r[1] or 'market')[:60]} ({sign}${pnl:.2f})",
            })
        for r in event_rows:
            activity.append({
                "timestamp":   r[0].isoformat() if r[0] else None,
                "kind":        r[1] or "event",
                "description": r[2] or "",
            })
        activity.sort(key=lambda x: x["timestamp"] or "", reverse=True)
        activity = activity[:30]

        plan_monthly = int(totals[8] or 0)
        plan_annual  = int(totals[9] or 0)
        mrr = plan_monthly * 69.99 + plan_annual * 52.50
        arr = mrr * 12.0

        return web.json_response({
            "stats": {
                "onboarded_users":    int(totals[0] or 0),
                "total_users":        int(totals[1] or 0),
                "active_subscribers": int(totals[2] or 0),
                "bankroll_under_mgmt": float(totals[3] or 0.0),
                "open_positions":     int(totals[4] or 0),
                "trades_24h":         int(totals[5] or 0),
                "total_realized":     float(totals[6] or 0.0),
                "total_evaluations":  int(totals[7] or 0),
                "plan_monthly":       plan_monthly,
                "plan_annual":        plan_annual,
                "past_due":           int(totals[10] or 0),
                "canceled":           int(totals[11] or 0),
                "new_signups_7d":     int(totals[12] or 0),
                "new_subs_7d":        int(totals[13] or 0),
                "settles_24h":        int(totals[14] or 0),
                "realized_24h":       float(totals[15] or 0.0),
                "mrr":                round(mrr, 2),
                "arr":                round(arr, 2),
            },
            "alerts":   alerts,
            "activity": activity,
        })

    async def _handle_admin_trades(self, request: web.Request) -> web.Response:
        """Paginated, filtered list of every trade across every user."""
        admin_uid = await self._require_admin(request)
        if not admin_uid:
            return web.json_response({"error": "admin access required"},
                                      status=403)

        mode   = (request.query.get("mode")   or "all").lower()
        status = (request.query.get("status") or "all").lower()
        q_raw  = (request.query.get("q")      or "").strip()
        try:
            limit = max(1, min(200, int(request.query.get("limit")  or "50")))
            offset = max(0, int(request.query.get("offset") or "0"))
        except ValueError:
            return web.json_response({"error": "invalid limit/offset"}, status=400)

        filters: list[str] = []
        params: dict[str, object] = {"lim": limit, "off": offset}

        if mode in ("live", "simulation"):
            filters.append("p.mode = :mode")
            params["mode"] = mode

        if status == "open":
            filters.append("p.status = 'open'")
        elif status == "settled":
            filters.append("p.status = 'settled'")
        elif status == "won":
            filters.append("p.status = 'settled' AND COALESCE(p.realized_pnl_usd, 0) > 0")
        elif status == "lost":
            filters.append("p.status = 'settled' AND COALESCE(p.realized_pnl_usd, 0) < 0")

        if q_raw:
            filters.append(
                "(LOWER(COALESCE(au.email, '')) LIKE :q "
                " OR LOWER(COALESCE(uc.display_name, '')) LIKE :q "
                " OR LOWER(p.user_id) LIKE :q)"
            )
            params["q"] = f"%{q_raw.lower()}%"

        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        loop = asyncio.get_running_loop()
        try:
            def _q():
                with get_engine().begin() as conn:
                    rows = conn.execute(text(
                        "SELECT p.id, p.created_at, p.user_id, au.email, "
                        "       uc.display_name, p.mode, p.market_id, p.slug, "
                        "       p.question, p.category, p.market_archetype, "
                        "       p.side, p.cost_usd, p.entry_price, "
                        "       p.claude_probability, p.status, "
                        "       p.realized_pnl_usd, p.settled_at "
                        "FROM pm_positions p "
                        "LEFT JOIN auth.users au ON au.id::text = p.user_id "
                        "LEFT JOIN user_config uc ON uc.user_id = p.user_id "
                        f"{where} "
                        "ORDER BY p.created_at DESC "
                        "LIMIT :lim OFFSET :off"
                    ), params).fetchall()

                    total = conn.execute(text(
                        f"SELECT COUNT(*) FROM pm_positions p "
                        "LEFT JOIN auth.users au ON au.id::text = p.user_id "
                        "LEFT JOIN user_config uc ON uc.user_id = p.user_id "
                        f"{where}"
                    ), params).scalar()
                    return rows, total
            rows, total = await loop.run_in_executor(self._pool, _q)
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)

        trades = [
            {
                "id":                 int(r[0]),
                "created_at":         r[1].isoformat() if r[1] else None,
                "user_id":            str(r[2]),
                "email":              r[3],
                "display_name":       r[4],
                "mode":               r[5],
                "market_id":          r[6],
                "slug":               r[7],
                "question":           r[8],
                "category":           r[9],
                "market_archetype":   r[10],
                "side":               r[11],
                "cost_usd":           float(r[12]) if r[12] is not None else None,
                "entry_price":        float(r[13]) if r[13] is not None else None,
                "claude_probability": float(r[14]) if r[14] is not None else None,
                "status":             r[15],
                "realized_pnl_usd":   float(r[16]) if r[16] is not None else None,
                "settled_at":         r[17].isoformat() if r[17] else None,
            }
            for r in rows
        ]

        return web.json_response({
            "trades": trades,
            "total":  int(total or 0),
            "limit":  limit,
            "offset": offset,
        })

    async def _handle_admin_user_detail(self, request: web.Request) -> web.Response:
        """Detail view for a single user (admin only)."""
        admin_uid = await self._require_admin(request)
        if not admin_uid:
            return web.json_response({"error": "admin access required"},
                                      status=403)
        target_uid = request.match_info.get("user_id", "").strip()
        if not target_uid:
            return web.json_response({"error": "user_id required"}, status=400)

        loop = asyncio.get_running_loop()
        try:
            def _q():
                with get_engine().begin() as conn:
                    user_row = conn.execute(text(
                        "SELECT uc.user_id, uc.display_name, uc.mode, "
                        "       uc.starting_cash, uc.onboarded_at, uc.is_admin, "
                        "       uc.bot_enabled, "
                        "       uc.subscription_status, uc.subscription_plan, "
                        "       uc.subscription_started_at, "
                        "       au.email, au.created_at, "
                        "       uc.telegram_chat_id, "
                        "       uc.polymarket_wallet_address "
                        "FROM user_config uc "
                        "LEFT JOIN auth.users au ON au.id::text = uc.user_id "
                        "WHERE uc.user_id = :uid"
                    ), {"uid": target_uid}).fetchone()

                    summary = conn.execute(text(
                        "SELECT "
                        "  COUNT(*) FILTER (WHERE status = 'open') AS open_n, "
                        "  COUNT(*) FILTER (WHERE status = 'settled') AS settled_n, "
                        "  COUNT(*) FILTER (WHERE status = 'settled' "
                        "                    AND COALESCE(realized_pnl_usd, 0) > 0) "
                        "    AS wins, "
                        "  COUNT(*) FILTER (WHERE status = 'settled' "
                        "                    AND COALESCE(realized_pnl_usd, 0) < 0) "
                        "    AS losses, "
                        "  COALESCE(SUM(realized_pnl_usd) "
                        "           FILTER (WHERE status = 'settled'), 0) "
                        "    AS realized_pnl, "
                        "  COALESCE(SUM(cost_usd) "
                        "           FILTER (WHERE status = 'open'), 0) "
                        "    AS open_cost "
                        "FROM pm_positions WHERE user_id = :uid"
                    ), {"uid": target_uid}).fetchone()

                    position_rows = conn.execute(text(
                        "SELECT id, created_at, market_id, slug, question, "
                        "       category, market_archetype, side, cost_usd, "
                        "       entry_price, claude_probability, status, "
                        "       realized_pnl_usd, settled_at "
                        "FROM pm_positions "
                        "WHERE user_id = :uid "
                        "ORDER BY created_at DESC "
                        "LIMIT 25"
                    ), {"uid": target_uid}).fetchall()

                    event_rows = conn.execute(text(
                        "SELECT timestamp, event_type, description, "
                        "       severity, source "
                        "FROM event_log "
                        "WHERE user_id = :uid "
                        "  AND timestamp >= NOW() - INTERVAL '7 days' "
                        "ORDER BY timestamp DESC "
                        "LIMIT 25"
                    ), {"uid": target_uid}).fetchall()

                    return user_row, summary, position_rows, event_rows
            user_row, summary, position_rows, event_rows = (
                await loop.run_in_executor(self._pool, _q)
            )
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)

        if not user_row:
            return web.json_response({"error": "user not found"}, status=404)

        settled = int(summary[1] or 0)
        wins = int(summary[2] or 0)
        losses = int(summary[3] or 0)
        denom = wins + losses
        win_rate = (wins / denom) if denom > 0 else 0.0

        return web.json_response({
            "user": {
                "user_id":                 str(user_row[0]),
                "display_name":            user_row[1],
                "mode":                    user_row[2],
                "starting_cash":           float(user_row[3]) if user_row[3] is not None else None,
                "onboarded_at":            user_row[4].isoformat() if user_row[4] else None,
                "is_admin":                bool(user_row[5]),
                "bot_enabled":             bool(user_row[6]),
                "subscription_status":     user_row[7],
                "subscription_plan":       user_row[8],
                "subscription_started_at": user_row[9].isoformat() if user_row[9] else None,
                "email":                   user_row[10],
                "created_at":              user_row[11].isoformat() if user_row[11] else None,
                "has_telegram":            bool(user_row[12]),
                "has_polymarket":          bool(user_row[13]),
            },
            "summary": {
                "open_positions": int(summary[0] or 0),
                "settled":        settled,
                "wins":           wins,
                "losses":         losses,
                "win_rate":       round(win_rate, 4),
                "realized_pnl":   float(summary[4] or 0.0),
                "open_cost":      float(summary[5] or 0.0),
            },
            "positions": [
                {
                    "id":                 int(r[0]),
                    "created_at":         r[1].isoformat() if r[1] else None,
                    "market_id":          r[2],
                    "slug":               r[3],
                    "question":           r[4],
                    "category":           r[5],
                    "market_archetype":   r[6],
                    "side":               r[7],
                    "cost_usd":           float(r[8]) if r[8] is not None else None,
                    "entry_price":        float(r[9]) if r[9] is not None else None,
                    "claude_probability": float(r[10]) if r[10] is not None else None,
                    "status":             r[11],
                    "realized_pnl_usd":   float(r[12]) if r[12] is not None else None,
                    "settled_at":         r[13].isoformat() if r[13] else None,
                }
                for r in position_rows
            ],
            "events": [
                {
                    "timestamp":   r[0].isoformat() if r[0] else None,
                    "event_type":  r[1],
                    "description": r[2],
                    "severity":    int(r[3] or 0),
                    "source":      r[4],
                }
                for r in event_rows
            ],
        })

    async def _handle_admin_user_action(self, request: web.Request) -> web.Response:
        """Admin write: pause/resume a user's bot, grant/revoke admin."""
        admin_uid = await self._require_admin(request)
        if not admin_uid:
            return web.json_response({"error": "admin access required"},
                                      status=403)
        target_uid = request.match_info.get("user_id", "").strip()
        if not target_uid:
            return web.json_response({"error": "user_id required"}, status=400)
        if target_uid == admin_uid:
            return web.json_response(
                {"error": "cannot modify your own account via admin action"},
                status=400,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        action = (body.get("action") or "").strip()
        if action not in {"pause_bot", "resume_bot",
                          "grant_admin", "revoke_admin"}:
            return web.json_response(
                {"error": f"unknown action: {action!r}"},
                status=400,
            )

        loop = asyncio.get_running_loop()
        try:
            def _q():
                with get_engine().begin() as conn:
                    exists = conn.execute(text(
                        "SELECT 1 FROM user_config WHERE user_id = :uid"
                    ), {"uid": target_uid}).scalar()
                    if not exists:
                        return False, None

                    if action == "pause_bot":
                        conn.execute(text(
                            "UPDATE user_config SET bot_enabled = FALSE "
                            "WHERE user_id = :uid"
                        ), {"uid": target_uid})
                        new_state = {"bot_enabled": False}
                    elif action == "resume_bot":
                        conn.execute(text(
                            "UPDATE user_config SET bot_enabled = TRUE "
                            "WHERE user_id = :uid"
                        ), {"uid": target_uid})
                        new_state = {"bot_enabled": True}
                    elif action == "grant_admin":
                        conn.execute(text(
                            "UPDATE user_config SET is_admin = TRUE "
                            "WHERE user_id = :uid"
                        ), {"uid": target_uid})
                        new_state = {"is_admin": True}
                    else:  # revoke_admin
                        conn.execute(text(
                            "UPDATE user_config SET is_admin = FALSE "
                            "WHERE user_id = :uid"
                        ), {"uid": target_uid})
                        new_state = {"is_admin": False}

                    conn.execute(text(
                        "INSERT INTO event_log "
                        "  (user_id, timestamp, event_type, severity, "
                        "   description, source) "
                        "VALUES (:uid, NOW(), :etype, 2, :desc, :src)"
                    ), {
                        "uid":   target_uid,
                        "etype": "admin_action",
                        "desc":  f"admin {admin_uid} performed {action} "
                                 f"on {target_uid}",
                        "src":   "admin_api",
                    })
                    return True, new_state
            found, new_state = await loop.run_in_executor(self._pool, _q)
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)

        if not found:
            return web.json_response({"error": "user not found"}, status=404)

        return web.json_response({
            "status":    "applied",
            "action":    action,
            "user_id":   target_uid,
            "new_state": new_state,
        })

    async def _handle_admin_forecaster_health(self,
                                              request: web.Request) -> web.Response:
        """Forecaster health: skip rate, ROI by category, feed status."""
        admin_uid = await self._require_admin(request)
        if not admin_uid:
            return web.json_response({"error": "admin access required"},
                                      status=403)
        try:
            days = max(1, min(90, int(request.query.get("days") or "7")))
        except ValueError:
            return web.json_response({"error": "invalid days"}, status=400)

        loop = asyncio.get_running_loop()
        try:
            def _q():
                with get_engine().begin() as conn:
                    totals = conn.execute(text(
                        f"SELECT "
                        f"  COUNT(*) AS evaluated, "
                        f"  COUNT(*) FILTER (WHERE skipped IS TRUE) AS skipped "
                        f"FROM market_evaluations "
                        f"WHERE created_at >= NOW() - INTERVAL '{days} days'"
                    )).fetchone()

                    by_cat = conn.execute(text(
                        f"SELECT "
                        f"  COALESCE(category, 'uncategorized') AS cat, "
                        f"  COUNT(*) FILTER (WHERE status = 'settled') AS settled, "
                        f"  COUNT(*) FILTER (WHERE status = 'settled' "
                        f"    AND COALESCE(realized_pnl_usd, 0) > 0) AS wins, "
                        f"  COALESCE(SUM(realized_pnl_usd) "
                        f"    FILTER (WHERE status = 'settled'), 0) AS realized "
                        f"FROM pm_positions "
                        f"WHERE created_at >= NOW() - INTERVAL '{days} days' "
                        f"GROUP BY cat "
                        f"ORDER BY settled DESC "
                        f"LIMIT 20"
                    )).fetchall()

                    feeds = conn.execute(text(
                        "SELECT feed_name, state, detail, timestamp "
                        "FROM feed_health_log "
                        "WHERE timestamp >= NOW() - INTERVAL '1 hour' "
                        "ORDER BY timestamp DESC "
                        "LIMIT 40"
                    )).fetchall()
                    return totals, by_cat, feeds
            totals, by_cat, feeds = (
                await loop.run_in_executor(self._pool, _q)
            )
        except Exception as exc:
            return web.json_response({"error": f"db error: {exc}"}, status=500)

        evaluated = int(totals[0] or 0)
        skipped = int(totals[1] or 0)
        skip_rate = (skipped / evaluated) if evaluated > 0 else 0.0

        seen: dict[str, dict] = {}
        for r in feeds:
            name = r[0]
            if name in seen:
                continue
            seen[name] = {
                "feed_name": name,
                "state":     r[1],
                "detail":    r[2] or "",
                "timestamp": r[3].isoformat() if r[3] else None,
            }

        return web.json_response({
            "window_days": days,
            "totals": {
                "evaluated": evaluated,
                "skipped":   skipped,
                "skip_rate": round(skip_rate, 4),
            },
            "by_category": [
                {
                    "category": r[0],
                    "settled":  int(r[1] or 0),
                    "wins":     int(r[2] or 0),
                    "win_rate": round((int(r[2] or 0) / int(r[1] or 1)), 4)
                                if int(r[1] or 0) > 0 else 0.0,
                    "realized": float(r[3] or 0.0),
                }
                for r in by_cat
            ],
            "feeds": sorted(seen.values(), key=lambda x: x["feed_name"]),
        })

    # ── Action handlers ──────────────────────────────────────────────────────
    async def _handle_bot_toggle(self, request: web.Request) -> web.Response:
        """
        Called by the dashboard after it flips user_config.bot_enabled in
        Supabase. Fires the Telegram "Delfi is online" (or "Trading paused")
        message for that user. This endpoint does NOT touch user_config - the
        web action is the authoritative writer. It only handles the side
        effects the bot owns (Telegram).
        """
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "X-User-Id header required"},
                                     status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        enabled = bool(body.get("enabled"))

        if self._notifier is None:
            return web.json_response({
                "status":  "skipped",
                "reason":  "notifier not initialised",
                "enabled": enabled,
            })

        async def _fire():
            try:
                if enabled:
                    await self._notifier.notify_startup(user_id)
                else:
                    from feeds import telegram_messages as tm
                    await self._notifier.send(user_id, tm.paused())
            except Exception as exc:
                print(f"[bot_api] bot-toggle telegram fire failed: {exc}",
                      file=sys.stderr)

        asyncio.create_task(_fire())
        return web.json_response({
            "status":  "notified",
            "enabled": enabled,
            "user_id": user_id,
        })

    async def _handle_scan_now(self, _request: web.Request) -> web.Response:
        if self._analyst is None:
            return web.json_response({"error": "analyst not available"}, status=503)
        from polymarket_runner import scan_and_analyze
        async def _runner():
            try:
                summary = await scan_and_analyze(
                    limit=int(getattr(config, "PM_SCAN_LIMIT", 100)),
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
                await resolve_positions(notifier=self._notifier)
            except Exception as exc:
                print(f"[bot_api] manual resolve failed: {exc}", file=sys.stderr)
        asyncio.create_task(_runner())
        return web.json_response({"status": "triggered",
                                   "triggered_at": datetime.now(timezone.utc).isoformat()})

    async def _handle_markouts(self, request: web.Request) -> web.Response:
        # Markouts are shared forecaster diagnostics (evaluations are shared
        # across tenants). Admin-only: non-admins cannot inspect this stream.
        caller_id = self._user_id_from(request)
        if not caller_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        loop = asyncio.get_running_loop()
        is_admin = bool(await loop.run_in_executor(
            self._pool, _user_is_admin, caller_id,
        ))
        if not is_admin:
            return web.json_response({"error": "admin only"}, status=403)

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

    async def _handle_reset_test(self, request: web.Request) -> web.Response:
        current_mode = self._disk_mode or getattr(config, "PM_MODE", "simulation")
        if current_mode == "live":
            return web.json_response(
                {"error": "reset-test is disabled in live mode"}, status=403)

        caller_id = self._user_id_from(request)
        if not caller_id:
            return web.json_response({"error": "X-User-Id header required"},
                                      status=401)
        loop = asyncio.get_running_loop()
        is_admin = bool(await loop.run_in_executor(
            self._pool, _user_is_admin, caller_id,
        ))

        # Admins may pass ?all=1 to wipe shared evaluation/markout tables
        # (cross-tenant reset). Non-admins always scope to their own rows -
        # pm_positions + predictions - and never touch shared data.
        wipe_all = is_admin and request.query.get("all") == "1"

        def _wipe():
            with get_engine().begin() as conn:
                # User-scoped tables: only the caller's rows.
                conn.execute(text(
                    "DELETE FROM pm_positions WHERE user_id = :uid"
                ), {"uid": caller_id})
                conn.execute(text(
                    "DELETE FROM predictions WHERE trade_id IN "
                    "(SELECT id FROM pm_positions WHERE user_id = :uid)"
                ), {"uid": caller_id})
                # Shared tables: admins with ?all=1 only.
                if wipe_all:
                    for tbl in ("markouts", "market_evaluations"):
                        conn.execute(text(f"DELETE FROM {tbl}"))

        await loop.run_in_executor(self._pool, _wipe)
        msg = ("All test data cleared for this user (positions + predictions)."
               + (" Shared evaluations + markouts also wiped." if wipe_all else ""))
        return web.json_response({"status": "ok", "message": msg})

    async def _handle_switch_mode(self, request: web.Request) -> web.Response:
        data = await request.json()
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in ("simulation", "live"):
            return web.json_response(
                {"error": "mode must be 'simulation' or 'live'"}, status=400)
        current = self._disk_mode or getattr(config, "PM_MODE", "simulation")
        if mode == current:
            return web.json_response({"status": "no_change", "mode": current})

        caller_id = self._user_id_from(request)

        # Dashboard changes apply immediately - no Telegram confirmation round-trip.
        try:
            persist_config_value("PM_MODE", mode)
            self._disk_mode = mode
            _audit_config_change("PM_MODE", current, mode, "dashboard",
                                 user_id=caller_id)
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
        caller_id = self._user_id_from(request)

        # Dashboard changes apply immediately - no Telegram confirmation round-trip.
        try:
            persist_config_value(key, value)
            importlib.reload(config)
            _audit_config_change(key, current, value, "dashboard",
                                 user_id=caller_id)
        except Exception as exc:
            return web.json_response(
                {"status": "error", "reason": str(exc)}, status=500)

        return web.json_response({
            "status": "applied",
            "key": key,
            "previous": current,
            "value": value,
        })

    def apply_pending_config(self, user_id: Optional[str] = None) -> dict:
        if not self._pending_config:
            return {"status": "none", "reason": "no pending config change"}
        pc = self._pending_config
        key, value, previous = pc["key"], pc["value"], pc["previous"]
        try:
            persist_config_value(key, value)
            if key == "PM_MODE":
                self._disk_mode = value
                _audit_config_change(key, previous, value, "dashboard",
                                     user_id=user_id)
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
            _audit_config_change(key, previous, value, "dashboard",
                                 user_id=user_id)
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
        app.router.add_get ("/api/config/telegram", self._handle_get_telegram_config)
        app.router.add_put ("/api/config/telegram", self._handle_put_telegram_config)
        app.router.add_get ("/api/config/telegram/reveal", self._handle_reveal_telegram_config)
        app.router.add_post("/api/config/telegram/test", self._handle_telegram_test)
        app.router.add_get ("/api/config/polymarket", self._handle_get_polymarket_config)
        app.router.add_put ("/api/config/polymarket", self._handle_put_polymarket_config)
        app.router.add_get ("/api/suggestions", self._handle_list_suggestions)
        app.router.add_get ("/api/learning-reports",
                            self._handle_list_learning_reports)
        app.router.add_post("/api/suggestions/{suggestion_id}/apply",
                            self._handle_apply_suggestion)
        app.router.add_post("/api/suggestions/{suggestion_id}/skip",
                            self._handle_skip_suggestion)
        app.router.add_post("/api/suggestions/{suggestion_id}/snooze",
                            self._handle_snooze_suggestion)
        app.router.add_get ("/api/markouts",    self._handle_markouts)
        app.router.add_post("/api/bot-toggle",  self._handle_bot_toggle)
        app.router.add_post("/api/scan-now",    self._handle_scan_now)
        app.router.add_post("/api/resolve-now", self._handle_resolve_now)
        app.router.add_post("/api/reset-test",    self._handle_reset_test)
        app.router.add_post("/api/switch-mode",   self._handle_switch_mode)
        app.router.add_post("/api/update-config", self._handle_update_config)
        app.router.add_get ("/api/admin/users",    self._handle_admin_users)
        app.router.add_get ("/api/admin/overview", self._handle_admin_overview)
        app.router.add_get ("/api/admin/trades",   self._handle_admin_trades)
        app.router.add_get ("/api/admin/users/{user_id}",
                            self._handle_admin_user_detail)
        app.router.add_post("/api/admin/users/{user_id}/action",
                            self._handle_admin_user_action)
        app.router.add_get ("/api/admin/forecaster",
                            self._handle_admin_forecaster_health)
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



def _audit_config_change(key: str, old, new, source: str,
                         user_id: Optional[str] = None) -> None:
    try:
        if not user_id:
            from engine.user_config import DEFAULT_USER_ID
            user_id = DEFAULT_USER_ID
        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO config_change_history "
                "(user_id, param_name, old_value, new_value, reason, suggested_by, outcome) "
                "VALUES (:user_id, :k, :o, :n, :r, :s, 'applied')"
            ), {
                "user_id": user_id,
                "k": key, "o": str(old), "n": str(new),
                "r": "dashboard /api/update-config", "s": source,
            })
    except Exception as exc:
        print(f"[bot_api] audit log write failed: {exc}", file=sys.stderr)
