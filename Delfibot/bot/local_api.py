"""
Local HTTP API for the Delfi sidecar.

The Tauri desktop shell launches the Python sidecar as a subprocess and
loads a webview pointing at a Vite-built React UI. The React UI talks to
this server over HTTP on 127.0.0.1:<port>.

Trust boundary
==============
We bind to 127.0.0.1 only. There is no auth header, no API key, no
token. The threat model assumes that any process running on the user's
machine could already read the SQLite DB and the OS keychain entries
directly, so adding a static secret on top of loopback would be theatre.
If we ever expose this beyond loopback (we won't, but if), this whole
file needs an auth layer first.

CORS is permissive (`Access-Control-Allow-Origin: *`) because in dev the
Vite server runs on 127.0.0.1:1420 and the API runs on a different
port, and in production the webview origin is `tauri://localhost` (or
similar custom scheme). Loopback-only binding makes this safe.

Surface
=======
GET  /api/health          process health (uptime, last-ok per job)
GET  /api/state           bot state summary (mode, started_at, etc.)
GET  /api/config          current user_config (no secrets)
PUT  /api/config          partial update of user_config (validated)
GET  /api/credentials     which credentials are present (booleans + wallet)
PUT  /api/credentials     write Polymarket / Anthropic creds to OS keychain
GET  /api/positions       open + recent pm_positions rows
GET  /api/events          recent event_log rows
POST /api/bot/start       set mode=live (requires creds + wallet)
POST /api/bot/stop        set mode=simulation
POST /api/scan            kick a one-off scan job

Errors are returned as `{"error": "<message>"}` with an appropriate
4xx/5xx status. Successful responses are JSON (no envelope).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Optional

from aiohttp import web
from sqlalchemy import desc, select

from db.engine import get_engine
from db.models import event_log, pm_positions
from engine.user_config import (
    KEYRING_ANTHROPIC_KEY,
    KEYRING_POLYMARKET_KEY,
    _keyring_get,
    get_anthropic_api_key,
    get_user_config,
    set_anthropic_api_key,
    set_user_polymarket_creds,
    update_user_config,
    validated_update_payload,
)
from process_health import health as proc_health


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for datetime / set / bytes."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def _ok(payload: Any, status: int = 200) -> web.Response:
    body = json.dumps(payload, default=_json_default)
    return web.Response(
        text=body,
        status=status,
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _err(message: str, status: int = 400) -> web.Response:
    return _ok({"error": message}, status=status)


def _config_to_dict(cfg) -> dict:
    """Strip out keychain-only / non-serializable bits before sending to UI."""
    raw = cfg.__dict__.copy() if hasattr(cfg, "__dict__") else {}
    # Tuples → lists for JSON.
    for k, v in list(raw.items()):
        if isinstance(v, tuple):
            raw[k] = list(v)
    # Never echo secrets back through GET /api/config. Keychain values are
    # surfaced through GET /api/credentials as booleans only.
    raw.pop("polymarket_api_key", None)
    return raw


class LocalAPI:
    """aiohttp HTTP server bound to 127.0.0.1.

    Constructed by main.py with a PMAnalyst and a port (0 = OS picks).
    Call `await start()` to bind the socket and run the server in the
    background; `start()` returns the actual bound port. Call
    `set_scheduler(scheduler)` after APScheduler is ready so handlers
    can reach in and trigger jobs on demand. Call `await stop()` on
    shutdown.
    """

    def __init__(self, analyst, host: str = "127.0.0.1", port: int = 0) -> None:
        self._analyst = analyst
        self._host = host
        self._requested_port = port
        self._scheduler = None
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._bound_port: int = 0

    def set_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    async def start(self) -> int:
        """Bind the socket and start serving. Returns the actual bound port."""
        self._app = web.Application()
        self._wire_routes(self._app)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._requested_port)
        await self._site.start()

        # Resolve the actual bound port (the caller may have passed 0).
        # aiohttp doesn't expose this directly so we walk the underlying
        # socket on the runner's server.
        bound = self._requested_port
        try:
            server = self._runner.server  # type: ignore[attr-defined]
            sockets = getattr(server, "sockets", None)
            if not sockets and self._site is not None:
                sockets = getattr(self._site, "_server", None)
                sockets = getattr(sockets, "sockets", None) if sockets else None
            if sockets:
                bound = sockets[0].getsockname()[1]
        except Exception:
            pass
        # Fallback: peek at site internals.
        if not bound:
            try:
                srv = getattr(self._site, "_server", None)
                if srv and srv.sockets:
                    bound = srv.sockets[0].getsockname()[1]
            except Exception:
                pass
        self._bound_port = int(bound or self._requested_port or 0)
        return self._bound_port

    async def stop(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        self._app = None
        self._runner = None
        self._site = None

    # ── Routes ──────────────────────────────────────────────────────────────
    def _wire_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/health",      self._health)
        app.router.add_get("/api/state",       self._state)
        app.router.add_get("/api/config",      self._get_config)
        app.router.add_put("/api/config",      self._put_config)
        app.router.add_get("/api/credentials", self._get_credentials)
        app.router.add_put("/api/credentials", self._put_credentials)
        app.router.add_get("/api/positions",   self._get_positions)
        app.router.add_get("/api/events",      self._get_events)
        app.router.add_post("/api/bot/start",  self._bot_start)
        app.router.add_post("/api/bot/stop",   self._bot_stop)
        app.router.add_post("/api/scan",       self._scan)

        # Permissive CORS preflight for the Vite dev server.
        async def _options(_req: web.Request) -> web.Response:
            return web.Response(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, PUT, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                },
            )
        app.router.add_route("OPTIONS", "/{tail:.*}", _options)

    # ── Handlers ────────────────────────────────────────────────────────────
    async def _health(self, _req: web.Request) -> web.Response:
        return _ok(proc_health.snapshot())

    async def _state(self, _req: web.Request) -> web.Response:
        cfg = get_user_config()
        return _ok({
            "mode": cfg.mode,
            "starting_cash": cfg.starting_cash,
            "wallet_address": cfg.wallet_address,
            "is_onboarded": cfg.is_onboarded,
            "can_trade_live": cfg.can_trade_live,
            "uptime_s": proc_health.uptime_seconds,
            "started_at": (proc_health.start_time.isoformat()
                           if proc_health.start_time else None),
            "error_count": proc_health.error_count,
        })

    async def _get_config(self, _req: web.Request) -> web.Response:
        cfg = get_user_config()
        return _ok(_config_to_dict(cfg))

    async def _put_config(self, req: web.Request) -> web.Response:
        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)
        try:
            changes = validated_update_payload(payload)
        except ValueError as exc:
            return _err(str(exc), 400)
        if not changes:
            return _ok(_config_to_dict(get_user_config()))
        try:
            cfg = update_user_config(**changes)
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            # SQLAlchemy / sqlite errors land here. Don't let aiohttp's
            # default handler return a text/plain 500: the React UI
            # expects JSON for every response.
            return _err(f"update failed: {exc}", 500)
        return _ok(_config_to_dict(cfg))

    async def _get_credentials(self, _req: web.Request) -> web.Response:
        cfg = get_user_config()
        return _ok({
            "wallet_address": cfg.wallet_address,
            "has_polymarket_key": _keyring_get(KEYRING_POLYMARKET_KEY) is not None,
            "has_anthropic_key": get_anthropic_api_key() is not None,
        })

    async def _put_credentials(self, req: web.Request) -> web.Response:
        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)

        wrote: list[str] = []

        # Polymarket private key + wallet address. The DB only stores the
        # wallet (the public 0x address); the private key is keychain-only.
        pm_key = payload.get("polymarket_private_key")
        wallet = payload.get("wallet_address")
        if pm_key is not None or wallet is not None:
            try:
                set_user_polymarket_creds(
                    private_key=pm_key,
                    wallet_address=wallet,
                )
                if pm_key is not None:
                    wrote.append("polymarket_private_key")
                if wallet is not None:
                    wrote.append("wallet_address")
            except Exception as exc:
                return _err(f"failed to write polymarket creds: {exc}", 500)

        # Anthropic API key (keychain only).
        anthro = payload.get("anthropic_api_key")
        if anthro is not None:
            try:
                set_anthropic_api_key(anthro)
                wrote.append("anthropic_api_key")
            except Exception as exc:
                return _err(f"failed to write anthropic key: {exc}", 500)

        cfg = get_user_config()
        return _ok({
            "wrote": wrote,
            "wallet_address": cfg.wallet_address,
            "has_polymarket_key": _keyring_get(KEYRING_POLYMARKET_KEY) is not None,
            "has_anthropic_key": get_anthropic_api_key() is not None,
        })

    async def _get_positions(self, req: web.Request) -> web.Response:
        try:
            limit = int(req.query.get("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))

        try:
            engine = get_engine()
            with engine.connect() as conn:
                stmt = (
                    select(pm_positions)
                    .order_by(desc(pm_positions.c.created_at))
                    .limit(limit)
                )
                rows = [dict(r._mapping) for r in conn.execute(stmt)]
        except Exception as exc:
            return _err(f"failed to read positions: {exc}", 500)
        return _ok({"positions": rows})

    async def _get_events(self, req: web.Request) -> web.Response:
        try:
            limit = int(req.query.get("limit", "200"))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 1000))

        try:
            engine = get_engine()
            with engine.connect() as conn:
                stmt = (
                    select(event_log)
                    .order_by(desc(event_log.c.timestamp))
                    .limit(limit)
                )
                rows = [dict(r._mapping) for r in conn.execute(stmt)]
        except Exception as exc:
            return _err(f"failed to read events: {exc}", 500)
        return _ok({"events": rows})

    async def _bot_start(self, _req: web.Request) -> web.Response:
        cfg = get_user_config()
        if not cfg.wallet_address:
            return _err("wallet_address is not set", 400)
        if _keyring_get(KEYRING_POLYMARKET_KEY) is None:
            return _err("polymarket private key is not in the keychain", 400)
        if get_anthropic_api_key() is None:
            return _err("anthropic api key is not in the keychain", 400)
        try:
            cfg = update_user_config(mode="live")
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            return _err(f"update failed: {exc}", 500)
        return _ok({"mode": cfg.mode})

    async def _bot_stop(self, _req: web.Request) -> web.Response:
        try:
            cfg = update_user_config(mode="simulation")
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            return _err(f"update failed: {exc}", 500)
        return _ok({"mode": cfg.mode})

    async def _scan(self, _req: web.Request) -> web.Response:
        """Trigger pm_scan once, immediately, without waiting for it to finish."""
        if self._scheduler is None:
            return _err("scheduler not ready", 503)
        job = self._scheduler.get_job("pm_scan")
        if job is None:
            return _err("pm_scan job is not registered", 500)
        try:
            # next_run_time=now causes APScheduler to fire on the next tick.
            job.modify(next_run_time=datetime.utcnow())
        except Exception as exc:
            return _err(f"failed to schedule scan: {exc}", 500)
        return _ok({"queued": True})


# Helpful when poking at the API from the dev shell:
#   python -c "import asyncio, local_api; asyncio.run(local_api._dev())"
async def _dev() -> None:  # pragma: no cover
    from engine.pm_analyst import PMAnalyst
    api = LocalAPI(analyst=PMAnalyst(notifier=None, news_feed=None), host="127.0.0.1", port=0)
    port = await api.start()
    print(f"DELFI_LOCAL_API_READY {port}")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await api.stop()
