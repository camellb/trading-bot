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
GET  /api/health                    process health (uptime, last-ok per job)
GET  /api/state                     bot state summary (mode, started_at, etc.)
GET  /api/config                    current user_config (no secrets)
PUT  /api/config                    partial update of user_config (validated)
GET  /api/credentials               which credentials are present (booleans + wallet)
PUT  /api/credentials               write Polymarket / Anthropic creds to OS keychain
GET  /api/positions                 open + recent pm_positions rows
GET  /api/events                    recent event_log rows
POST /api/bot/start                 set mode=live (requires creds + wallet)
POST /api/bot/stop                  set mode=simulation
POST /api/scan                      kick a one-off scan job

Performance + Learning surface
==============================
GET  /api/summary                   bankroll, equity, win rate, ROI, Brier
GET  /api/calibration               full calibration report (?source, ?since_days)
GET  /api/brier-trend               running Brier on settled positions over time
GET  /api/suggestions               pending V1-multiplier proposals from the
                                    learning cadence (apply / skip / snooze)
POST /api/suggestions/{id}/apply    apply a proposal to user_config
POST /api/suggestions/{id}/skip     reject a proposal
POST /api/suggestions/{id}/snooze   snooze a proposal until N more settled trades
GET  /api/learning-reports          50-trade narrative reviews (?limit)

Archetypes + Notifications + Reset
==================================
GET  /api/archetypes                canonical archetype catalogue with the
                                    user's current per-archetype skip flag
                                    and stake multiplier
GET  /api/evaluations               recent market_evaluations rows
GET  /api/config/notifications      per-category notification prefs
PUT  /api/config/notifications      replace per-category notification prefs
POST /api/reset-simulation          wipe simulation-mode positions

Errors are returned as `{"error": "<message>"}` with an appropriate
4xx/5xx status. Successful responses are JSON (no envelope).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Optional

from aiohttp import web
from sqlalchemy import desc, select, text

import calibration
from db.engine import get_engine
from db.models import event_log, market_evaluations, pm_positions
from engine.archetype_classifier import ARCHETYPES
from engine.learning_cadence import (
    apply_suggestion,
    list_pending_suggestions,
    skip_suggestion,
    snooze_suggestion,
)
from engine.review_report import list_learning_reports
from engine.user_config import (
    ARCHETYPE_MULTIPLIER_BOUNDS,
    DEFAULT_USER_ID,
    KEYRING_ANTHROPIC_KEY,
    KEYRING_POLYMARKET_KEY,
    NOTIFICATION_CATEGORIES,
    V1_DEFAULT_ARCHETYPE_SKIP_LIST,
    V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS,
    _keyring_get,
    get_anthropic_api_key,
    get_cryptopanic_key,
    get_license_key,
    get_license_meta,
    get_llm_backup_key,
    get_newsapi_key,
    get_user_config,
    set_anthropic_api_key,
    set_cryptopanic_key,
    set_license_key,
    set_license_meta,
    set_llm_backup_key,
    set_newsapi_key,
    set_user_polymarket_creds,
    update_user_config,
    validated_update_payload,
)
from engine.license import (
    LICENSE_OFFLINE_GRACE_DAYS,
    deactivate_license,
    validate_license,
)
from execution.pm_executor import PMExecutor
from process_health import health as proc_health


# ── Static metadata: archetype labels + descriptions ────────────────────
# Human-readable text that drives the per-archetype settings UI. Kept in
# this module rather than the classifier so editing copy doesn't churn
# the engine. Description copy mirrors the SaaS Risk page.
ARCHETYPE_META: dict[str, dict[str, str]] = {
    "tennis":             {"label": "Tennis",
                           "description": "Singles and doubles matches across all tours and qualifiers."},
    "basketball":         {"label": "Basketball",
                           "description": "NBA, EuroLeague, college, props, and full-game lines."},
    "baseball":           {"label": "Baseball",
                           "description": "MLB, KBO, NPB, and full-game props."},
    "football":           {"label": "Football",
                           "description": "American football: NFL, college, props, and full-game lines."},
    "hockey":             {"label": "Hockey",
                           "description": "NHL, KHL, and other ice hockey markets."},
    "cricket":            {"label": "Cricket",
                           "description": "ODI, T20, IPL, and Test matches."},
    "esports":            {"label": "Esports",
                           "description": "CS, Dota, LoL, Valorant - any pro-tier match."},
    "soccer":             {"label": "Soccer",
                           "description": "Domestic leagues, internationals, props, full-time results."},
    "sports_other":       {"label": "Other sports",
                           "description": "Anything sport-shaped not listed above (boxing, MMA, golf, motorsport, etc.)."},
    "price_threshold":    {"label": "Price threshold",
                           "description": "Will an asset cross a price by a date (BTC, equities, FX, commodities)?"},
    "activity_count":     {"label": "Activity count",
                           "description": "Counts of public activity (executive orders, posts, hires, layoffs) by date."},
    "geopolitical_event": {"label": "Geopolitical event",
                           "description": "Wars, sanctions, ceasefires, elections, and other state-level outcomes."},
    "binary_event":       {"label": "Other event",
                           "description": "Yes/no markets that don't fit the categories above."},
}


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


def _validate_polymarket_private_key(private_key: str, wallet_address: Optional[str]) -> Optional[str]:
    """
    Returns an error string if the private key is malformed or doesn't
    derive to the supplied wallet address. None on success.

    Local-only check (no chain RPC). The key+wallet relationship is
    deterministic so this catches typos and mismatched paste before the
    user discovers it the hard way (an order signed by the wrong
    wallet, rejected at fill time).
    """
    if not isinstance(private_key, str) or not private_key.strip():
        return "Polymarket private key is empty."
    raw = private_key.strip()
    # Accept both 0x-prefixed and bare hex.
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        return "Polymarket private key must be 64 hex characters (32 bytes)."
    try:
        from eth_account import Account
        derived = Account.from_key(bytes.fromhex(raw)).address
    except Exception as exc:
        return f"Polymarket private key did not parse: {exc}"
    if wallet_address and wallet_address.strip():
        # Compare normalised lower-case to dodge checksum casing issues.
        if derived.lower() != wallet_address.strip().lower():
            return (
                f"Wallet address {wallet_address} does not match the "
                f"address {derived} derived from the private key. "
                f"Re-paste one of them."
            )
    return None


def _validate_llm_key_shape(key: str, label: str = "LLM key") -> Optional[str]:
    """
    Cheap shape check on an LLM API key. Catches the obvious paste
    mistakes (whitespace, wrong tab paste, key truncated). No network
    call: an actual provider auth check costs latency we don't want on
    every save, and can false-negative on transient provider 5xx.
    """
    if not isinstance(key, str) or not key.strip():
        return f"{label} is empty."
    raw = key.strip()
    if " " in raw or "\n" in raw or "\t" in raw:
        return f"{label} contains whitespace; re-paste without the surrounding text."
    if len(raw) < 20:
        return f"{label} looks too short ({len(raw)} chars); did the paste cut off?"
    return None


def _config_to_dict(cfg) -> dict:
    """Strip out keychain-only / non-serializable bits before sending to UI."""
    raw = cfg.__dict__.copy() if hasattr(cfg, "__dict__") else {}
    # Tuples → lists for JSON.
    for k, v in list(raw.items()):
        if isinstance(v, tuple):
            raw[k] = list(v)
    # Defensive: Polymarket private key + LLM keys never live on the
    # dataclass (they're keychain-only), but pop them anyway in case a
    # legacy dataclass shape still carries them. Boolean presence is
    # surfaced via GET /api/credentials.
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

        # Performance + learning
        app.router.add_get("/api/summary",          self._get_summary)
        app.router.add_get("/api/calibration",      self._get_calibration)
        app.router.add_get("/api/brier-trend",      self._get_brier_trend)
        app.router.add_get("/api/suggestions",      self._get_suggestions)
        app.router.add_post("/api/suggestions/{suggestion_id}/apply",
                            self._apply_suggestion)
        app.router.add_post("/api/suggestions/{suggestion_id}/skip",
                            self._skip_suggestion)
        app.router.add_post("/api/suggestions/{suggestion_id}/snooze",
                            self._snooze_suggestion)
        app.router.add_get("/api/learning-reports", self._get_learning_reports)

        # Archetypes (the per-archetype Settings UX)
        app.router.add_get("/api/archetypes",       self._get_archetypes)

        # Recent market evaluations (feeds Intelligence + Positions detail)
        app.router.add_get("/api/evaluations",      self._get_evaluations)

        # Notification prefs (per-category toggles, in-app only)
        app.router.add_get("/api/config/notifications",   self._get_notifications)
        app.router.add_put("/api/config/notifications",   self._put_notifications)

        # License (Lemon Squeezy hard gate). The React shell calls
        # /api/license/status on every mount and shows the
        # LicenseGate until status reports valid=true.
        app.router.add_get("/api/license/status",         self._get_license_status)
        app.router.add_post("/api/license/activate",      self._post_license_activate)
        app.router.add_post("/api/license/deactivate",    self._post_license_deactivate)

        # Simulation reset (zeros out simulation positions + bankroll)
        app.router.add_post("/api/reset-simulation",      self._reset_simulation)

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
            "bot_enabled": bool(getattr(cfg, "bot_enabled", False)),
            "ready_to_trade": bool(getattr(cfg, "ready_to_trade", False)),
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
        has_llm = get_anthropic_api_key() is not None
        return _ok({
            "wallet_address":         cfg.wallet_address,
            "has_polymarket_key":     _keyring_get(KEYRING_POLYMARKET_KEY) is not None,
            # Both names returned: `has_anthropic_key` for back-compat,
            # `has_llm_key` for the new vendor-neutral UI.
            "has_anthropic_key":      has_llm,
            "has_llm_key":            has_llm,
            "has_llm_backup_key":     get_llm_backup_key() is not None,
            "has_newsapi_key":        get_newsapi_key() is not None,
            "has_cryptopanic_key":    get_cryptopanic_key() is not None,
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
        # Validate locally before persisting so a typo can't end up in
        # the keychain unnoticed.
        pm_key = payload.get("polymarket_private_key")
        wallet = payload.get("wallet_address")
        # Treat empty string as "clear this slot" — only run the
        # derivation check when the user actually pasted something.
        if pm_key is not None and pm_key.strip():
            err = _validate_polymarket_private_key(pm_key, wallet)
            if err:
                return _err(err, 400)
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

        # Primary LLM key (keychain only). Accepts both the new
        # `llm_api_key` field and the legacy `anthropic_api_key` field;
        # the keychain entry name itself stays `anthropic_api_key` so
        # existing installs don't lose their stored key.
        llm = payload.get("llm_api_key")
        if llm is None:
            llm = payload.get("anthropic_api_key")
        if llm is not None:
            if llm.strip():
                err = _validate_llm_key_shape(llm, "LLM API key")
                if err:
                    return _err(err, 400)
            try:
                set_anthropic_api_key(llm)
                wrote.append("llm_api_key")
            except Exception as exc:
                return _err(f"failed to write llm key: {exc}", 500)

        # Optional secondary LLM (failover / hedge against rate limits).
        llm_backup = payload.get("llm_backup_key")
        if llm_backup is not None:
            if llm_backup.strip():
                err = _validate_llm_key_shape(llm_backup, "Backup LLM key")
                if err:
                    return _err(err, 400)
            try:
                set_llm_backup_key(llm_backup)
                wrote.append("llm_backup_key")
            except Exception as exc:
                return _err(f"failed to write llm backup key: {exc}", 500)

        # Optional NewsAPI key (breaking news context).
        newsapi = payload.get("newsapi_key")
        if newsapi is not None:
            if newsapi.strip():
                err = _validate_llm_key_shape(newsapi, "NewsAPI key")
                if err:
                    return _err(err, 400)
            try:
                set_newsapi_key(newsapi)
                wrote.append("newsapi_key")
            except Exception as exc:
                return _err(f"failed to write newsapi key: {exc}", 500)

        # Optional CryptoPanic key (crypto-specific news).
        cryptopanic = payload.get("cryptopanic_key")
        if cryptopanic is not None:
            if cryptopanic.strip():
                err = _validate_llm_key_shape(cryptopanic, "CryptoPanic key")
                if err:
                    return _err(err, 400)
            try:
                set_cryptopanic_key(cryptopanic)
                wrote.append("cryptopanic_key")
            except Exception as exc:
                return _err(f"failed to write cryptopanic key: {exc}", 500)

        cfg = get_user_config()
        has_llm = get_anthropic_api_key() is not None
        return _ok({
            "wrote":                  wrote,
            "wallet_address":         cfg.wallet_address,
            "has_polymarket_key":     _keyring_get(KEYRING_POLYMARKET_KEY) is not None,
            "has_anthropic_key":      has_llm,
            "has_llm_key":            has_llm,
            "has_llm_backup_key":     get_llm_backup_key() is not None,
            "has_newsapi_key":        get_newsapi_key() is not None,
            "has_cryptopanic_key":    get_cryptopanic_key() is not None,
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
        """Enable the bot (set user_config.bot_enabled = True).

        Validates that the user has the credentials they actually need
        for their current mode before flipping the switch. Live mode
        requires a wallet, a Polymarket private key, and an LLM API
        key. Simulation only needs the LLM key (Delfi still forecasts
        each market, just doesn't fund the trade). Mode-switching
        itself is a separate operation: PUT /api/config with
        `{"mode": "live"}` or `{"mode": "simulation"}`.
        """
        # License is a hard gate — any non-valid status here means
        # the LicenseGate should be re-shown by the React shell. We
        # still return a clear error string so curl users see the
        # reason.
        license_status = self._license_status_payload()
        if not license_status.get("valid"):
            reason = license_status.get("reason") or "license is not valid"
            return _err(f"license check failed: {reason}", 403)

        cfg = get_user_config()
        if get_anthropic_api_key() is None:
            return _err("LLM API key is not set", 400)
        if cfg.mode == "live":
            if not cfg.wallet_address:
                return _err("wallet_address is not set", 400)
            if _keyring_get(KEYRING_POLYMARKET_KEY) is None:
                return _err("polymarket private key is not in the keychain", 400)
        try:
            cfg = update_user_config(bot_enabled=True)
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            return _err(f"update failed: {exc}", 500)
        return _ok({"bot_enabled": True, "mode": cfg.mode})

    async def _bot_stop(self, _req: web.Request) -> web.Response:
        """Disable the bot (set user_config.bot_enabled = False).

        The scheduler keeps running scan/resolve jobs but the executor
        refuses to open new positions while bot_enabled is False (see
        UserConfig.ready_to_trade). Existing positions still settle.
        """
        try:
            cfg = update_user_config(bot_enabled=False)
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            return _err(f"update failed: {exc}", 500)
        return _ok({"bot_enabled": False, "mode": cfg.mode})

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

    # ── Performance + Learning ────────────────────────────────────────────────
    async def _get_summary(self, _req: web.Request) -> web.Response:
        """
        Headline performance numbers: bankroll, equity, win rate, ROI, Brier.
        Single-user; no X-User-Id needed. Mode is always taken from
        user_config (no view-mode override in v1).
        """
        try:
            executor = PMExecutor(DEFAULT_USER_ID)
            stats = executor.get_portfolio_stats()
        except Exception as exc:
            return _err(f"failed to compute portfolio stats: {exc}", 500)

        # Brier on this user's settled positions. Source filter is
        # 'polymarket' to match the bucket that drives sizing decisions.
        try:
            brier = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: calibration.get_report(
                    source="polymarket", user_id=DEFAULT_USER_ID,
                ),
            )
        except Exception:
            brier = {"brier": None, "resolved": 0, "total": 0}

        starting = float(stats.get("starting_cash") or 0.0)
        realized = float(stats.get("realized_pnl") or 0.0)
        roi = (realized / starting) if starting > 0 else None

        return _ok({
            "mode":           stats.get("mode"),
            "bankroll":       stats.get("bankroll"),
            "equity":         stats.get("equity"),
            "starting_cash":  stats.get("starting_cash"),
            "open_positions": stats.get("open_positions"),
            "open_cost":      stats.get("open_cost"),
            "settled_total":  stats.get("settled_total"),
            "settled_wins":   stats.get("settled_wins"),
            "win_rate":       stats.get("win_rate"),
            "realized_pnl":   stats.get("realized_pnl"),
            "roi":            roi,
            "brier":          brier.get("brier"),
            "resolved_predictions": brier.get("resolved"),
            "total_predictions":    brier.get("total"),
        })

    async def _get_calibration(self, req: web.Request) -> web.Response:
        """Full calibration report (Brier + reliability buckets)."""
        source = req.query.get("source") or "polymarket"
        since_raw = req.query.get("since_days")
        since_int = int(since_raw) if since_raw and since_raw.isdigit() else None
        try:
            report = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: calibration.get_report(
                    source=None if source == "all" else source,
                    since_days=since_int,
                    user_id=DEFAULT_USER_ID,
                ),
            )
        except Exception as exc:
            return _err(f"calibration query failed: {exc}", 500)
        return _ok(report)

    async def _get_brier_trend(self, _req: web.Request) -> web.Response:
        """
        Running Brier over settled positions, in chronological order.
        Used to show whether the forecaster is getting more or less
        calibrated as more data accumulates.
        """
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text(
                    "SELECT settled_at, claude_probability, side, "
                    "       CASE WHEN settlement_outcome = side THEN 1 ELSE 0 END "
                    "FROM pm_positions "
                    "WHERE user_id = :uid "
                    "  AND settled_at IS NOT NULL "
                    "  AND claude_probability IS NOT NULL "
                    "  AND settlement_outcome IN ('YES', 'NO') "
                    "ORDER BY settled_at ASC"
                ), {"uid": DEFAULT_USER_ID}).fetchall()
        except Exception as exc:
            return _err(f"brier-trend query failed: {exc}", 500)

        points = []
        running = 0.0
        for i, r in enumerate(rows, 1):
            p_yes = float(r[1])
            side = (r[2] or "YES").upper()
            # Brier is computed on the chosen side: p_yes if YES, else 1-p_yes.
            p = p_yes if side == "YES" else (1.0 - p_yes)
            o = int(r[3])
            running += (p - o) ** 2
            points.append({
                "date":  r[0].isoformat() if r[0] else None,
                "brier": round(running / i, 4),
                "n":     i,
            })
        return _ok({"points": points})

    async def _get_suggestions(self, req: web.Request) -> web.Response:
        """List pending V1-multiplier proposals from the learning cadence."""
        include_snoozed = req.query.get("include_snoozed", "1") != "0"
        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: list_pending_suggestions(
                    DEFAULT_USER_ID, include_snoozed=include_snoozed,
                ),
            )
        except Exception as exc:
            return _err(f"failed to list suggestions: {exc}", 500)
        return _ok({"suggestions": rows})

    async def _suggestion_action(self, req: web.Request, fn) -> web.Response:
        try:
            sid = int(req.match_info.get("suggestion_id", "0"))
        except ValueError:
            return _err("invalid suggestion id", 400)
        if sid <= 0:
            return _err("invalid suggestion id", 400)
        try:
            payload = await req.json() if req.has_body else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        kwargs = {"user_id": DEFAULT_USER_ID, "resolved_by": "user"}
        # snooze takes an optional `wait_trades` knob; tolerate it for
        # apply/skip too (those just ignore it).
        if "wait_trades" in payload:
            try:
                kwargs["wait_trades"] = int(payload["wait_trades"])
            except (TypeError, ValueError):
                pass

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, lambda: fn(sid, **kwargs))
        except TypeError:
            # Older signatures may not accept wait_trades; retry without.
            kwargs.pop("wait_trades", None)
            try:
                result = await loop.run_in_executor(None, lambda: fn(sid, **kwargs))
            except Exception as exc:
                return _err(f"suggestion action failed: {exc}", 500)
        except Exception as exc:
            return _err(f"suggestion action failed: {exc}", 500)
        return _ok(result if isinstance(result, dict) else {"result": result})

    async def _apply_suggestion(self, req: web.Request) -> web.Response:
        return await self._suggestion_action(req, apply_suggestion)

    async def _skip_suggestion(self, req: web.Request) -> web.Response:
        return await self._suggestion_action(req, skip_suggestion)

    async def _snooze_suggestion(self, req: web.Request) -> web.Response:
        return await self._suggestion_action(req, snooze_suggestion)

    async def _get_learning_reports(self, req: web.Request) -> web.Response:
        """50-trade narrative reviews. Single-user, so no admin gate."""
        try:
            limit = int(req.query.get("limit", "10"))
        except ValueError:
            limit = 10
        limit = max(1, min(limit, 50))
        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: list_learning_reports(
                    user_id=DEFAULT_USER_ID, limit=limit, include_admin=False,
                ),
            )
        except Exception as exc:
            return _err(f"failed to list learning reports: {exc}", 500)
        return _ok({"reports": rows})

    # ── Archetype catalogue ──────────────────────────────────────────────
    async def _get_archetypes(self, _req: web.Request) -> web.Response:
        """Return the canonical archetype list with current per-user state.

        Drives the per-archetype Settings UI. Each entry carries the
        canonical id, a human-readable label + description, the
        V1-doctrine default skip/multiplier (so the "reset to default"
        button knows what to set), and the user's current effective
        skip flag and multiplier value.
        """
        cfg = get_user_config()
        skip_set = set(cfg.archetype_skip_list or ())
        mults = dict(cfg.archetype_stake_multipliers or {})

        out = []
        for arch in ARCHETYPES:
            meta = ARCHETYPE_META.get(arch, {"label": arch, "description": ""})
            default_mult = float(V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS.get(arch, 1.0))
            default_skip = arch in V1_DEFAULT_ARCHETYPE_SKIP_LIST
            current_mult = float(mults.get(arch, default_mult))
            out.append({
                "id":             arch,
                "label":          meta["label"],
                "description":    meta["description"],
                "skip":           arch in skip_set,
                "multiplier":     current_mult,
                "default_skip":   default_skip,
                "default_mult":   default_mult,
            })

        bounds_lo, bounds_hi = ARCHETYPE_MULTIPLIER_BOUNDS
        return _ok({
            "archetypes": out,
            "bounds": {
                "multiplier_min": bounds_lo,
                "multiplier_max": bounds_hi,
            },
        })

    # ── Recent market evaluations ────────────────────────────────────────
    async def _get_evaluations(self, req: web.Request) -> web.Response:
        """Recent rows from the market_evaluations cache.

        Used by Intelligence + the Positions detail panel to show why
        Delfi looked at a market and what it concluded - including the
        ones it skipped (which never become pm_positions rows).
        """
        try:
            limit = int(req.query.get("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))

        try:
            engine = get_engine()
            with engine.connect() as conn:
                stmt = (
                    select(market_evaluations)
                    .order_by(desc(market_evaluations.c.evaluated_at))
                    .limit(limit)
                )
                rows = [dict(r._mapping) for r in conn.execute(stmt)]
        except Exception as exc:
            return _err(f"failed to read evaluations: {exc}", 500)
        return _ok({"evaluations": rows})

    # ── Notification prefs (per-category toggles) ───────────────────────
    async def _get_notifications(self, _req: web.Request) -> web.Response:
        cfg = get_user_config()
        prefs = dict(cfg.notification_prefs or {})
        # Categories not present in the stored prefs default to True so
        # a fresh install gets every notification until the user opts
        # out. The shape mirrors what the UI expects.
        full = {cat: bool(prefs.get(cat, True)) for cat in NOTIFICATION_CATEGORIES}
        return _ok({
            "categories":         list(NOTIFICATION_CATEGORIES),
            "notification_prefs": full,
        })

    async def _put_notifications(self, req: web.Request) -> web.Response:
        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)

        # Accept either {"notification_prefs": {...}} or a flat dict of
        # {category: bool}.  Either way we hand the inner dict to
        # update_user_config which validates against NOTIFICATION_CATEGORIES.
        prefs = payload.get("notification_prefs", payload)
        if not isinstance(prefs, dict):
            return _err("notification_prefs must be a JSON object", 400)
        try:
            update_user_config(notification_prefs=prefs)
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            return _err(f"update failed: {exc}", 500)

        cfg = get_user_config()
        full = {cat: bool((cfg.notification_prefs or {}).get(cat, True))
                for cat in NOTIFICATION_CATEGORIES}
        return _ok({
            "categories":         list(NOTIFICATION_CATEGORIES),
            "notification_prefs": full,
        })

    # ── License (LS hard gate) ──────────────────────────────────────────
    def _license_status_payload(self) -> dict:
        """Pack the cached state into a small dict for the LicenseGate.

        The component cares about three things:
          - is the app gated right now (`valid` boolean)
          - if not, why ("reason" string for inline display)
          - was a key ever activated ("has_key" — controls whether to
            show "Paste your license key" or "Re-activate")
        """
        from datetime import datetime, timedelta, timezone

        key = get_license_key() or ""
        meta = get_license_meta() or {}
        status = meta.get("status")
        last_validated_iso = meta.get("last_validated_at")

        valid = False
        reason: Optional[str] = None
        if not key:
            reason = "no license key activated"
        elif status == "revoked":
            reason = "license has been revoked"
        elif status == "invalid":
            reason = meta.get("error") or "license is invalid"
        elif status == "valid" and last_validated_iso:
            try:
                last = datetime.fromisoformat(last_validated_iso)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - last
                if age <= timedelta(days=LICENSE_OFFLINE_GRACE_DAYS):
                    valid = True
                else:
                    reason = "license needs re-validation (offline grace expired)"
            except Exception:
                reason = "cached license metadata is unparseable"
        else:
            reason = "license never validated"

        return {
            "valid": valid,
            "reason": reason,
            "has_key": bool(key),
            "last_validated_at": last_validated_iso,
            "instance_id": meta.get("instance_id"),
        }

    async def _get_license_status(self, _req: web.Request) -> web.Response:
        return _ok(self._license_status_payload())

    async def _post_license_activate(self, req: web.Request) -> web.Response:
        """Store + validate a license key against Lemon Squeezy.

        First-paste flow:
          1. POST /api/license/activate {"license_key": "..."}
          2. Sidecar calls LS /v1/licenses/validate.
          3. On valid: store key + meta in keychain, return 200.
          4. On invalid: return 400 with the LS error message; do NOT
             store the key (a stored bad key would block the gate
             without recourse).
        """
        from datetime import datetime, timezone
        import platform as _platform

        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)

        key = (payload.get("license_key") or "").strip()
        if not key:
            return _err("license_key is required", 400)

        # Stable per-machine identifier so LS can attribute
        # activations to the right machine in their dashboard. Uses
        # node + system; not collected anywhere else.
        instance_name = f"delfi-{_platform.node()}-{_platform.system()}"

        result = await asyncio.get_event_loop().run_in_executor(
            None, validate_license, key, instance_name,
        )

        if not result.valid:
            # Don't persist a known-bad key — it would just keep the
            # gate open with no path forward. The user can paste a
            # different key.
            return _err(result.error or "license is not valid", 400)

        set_license_key(key)
        set_license_meta({
            "status":             "valid",
            "last_validated_at":  datetime.now(timezone.utc).isoformat(),
            "instance_id":        result.instance_id,
        })
        return _ok(self._license_status_payload())

    async def _post_license_deactivate(self, _req: web.Request) -> web.Response:
        """Sign this device out of its license.

        Order of operations:
          1. If we have an LS-issued instance_id from activation, call
             `/v1/licenses/deactivate` to free the slot. This is what
             makes "move to a new machine" work without spending
             another activation against `activation_limit`.
          2. Wipe the local keychain regardless of LS outcome — the
             user pressed the button to leave this machine; we honour
             that locally even if LS is unreachable.
          3. Surface a warning string when LS rejected the
             deactivation so the user knows their slot may still be
             consumed (e.g. they're offline). Status payload itself
             still flips to `valid: false, has_key: false`.
        """
        key = get_license_key() or ""
        meta = get_license_meta() or {}
        instance_id = (meta.get("instance_id") or "").strip()

        warning: Optional[str] = None
        if key and instance_id:
            result = await asyncio.get_event_loop().run_in_executor(
                None, deactivate_license, key, instance_id,
            )
            if not result.deactivated:
                warning = (
                    f"could not free the license slot on the server "
                    f"({result.error or 'unknown error'}). The local "
                    f"key has been cleared, but if you hit your "
                    f"activation limit on a new machine, email "
                    f"info@delfibot.com to release the orphan slot."
                )

        set_license_key(None)
        set_license_meta(None)

        payload = self._license_status_payload()
        if warning:
            payload["warning"] = warning
        return _ok(payload)

    # ── Reset simulation ────────────────────────────────────────────────
    async def _reset_simulation(self, _req: web.Request) -> web.Response:
        """Wipe simulation-mode positions so the user can start a fresh sim.

        Live positions are untouched - the SQL is mode-scoped. This is
        the local equivalent of the SaaS "reset simulation" button.
        Settled simulation positions get hard-deleted (along with their
        cascading evaluation/markout rows we joined in to keep stats
        clean).
        """
        try:
            engine = get_engine()
            with engine.begin() as conn:
                # Open simulation positions: cancel them so the new run
                # starts cleanly. Settled simulation positions: drop them
                # so the simulation Brier / ROI restarts from zero.
                conn.execute(text(
                    "DELETE FROM pm_positions WHERE mode = 'simulation' "
                    "  AND user_id = :uid"
                ), {"uid": DEFAULT_USER_ID})
                conn.execute(text(
                    "DELETE FROM markouts WHERE evaluation_id IN ("
                    "  SELECT id FROM market_evaluations "
                    "  WHERE user_id = :uid"
                    ")"
                ), {"uid": DEFAULT_USER_ID})
        except Exception as exc:
            return _err(f"reset failed: {exc}", 500)
        return _ok({"ok": True, "detail": "simulation positions reset"})


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
