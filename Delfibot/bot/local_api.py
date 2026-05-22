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
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from aiohttp import web
from sqlalchemy import desc, select, text

import calibration
from db.engine import get_engine, iso_utc
from db.models import event_log, market_evaluations, pm_positions
from engine.archetype_classifier import ARCHETYPES
from engine.learning_cadence import (
    apply_suggestion,
    list_pending_suggestions,
    list_resolved_suggestions,
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
    get_gemini_api_key,
    get_license_key,
    get_license_meta,
    get_llm_backup_key,
    get_newsapi_key,
    get_polymarket_api_creds,
    get_polymarket_relayer_api_key,
    set_polymarket_relayer_api_key,
    get_user_config,
    get_user_telegram_config,
    set_anthropic_api_key,
    set_cryptopanic_key,
    set_gemini_api_key,
    set_license_key,
    set_license_meta,
    set_llm_backup_key,
    set_newsapi_key,
    set_polymarket_api_creds,
    set_user_polymarket_creds,
    set_user_telegram_config,
    update_user_config,
    validated_update_payload,
)
from engine.license import (
    fresh_meta_for,
    verify_license,
)
from execution.pm_executor import PMExecutor
from process_health import health as proc_health


# ── Static metadata: archetype labels + descriptions ────────────────────
# Human-readable text that drives the per-archetype settings UI. Kept in
# this module rather than the classifier so editing copy doesn't churn
# the engine. Description copy mirrors the SaaS Risk page.
ARCHETYPE_META: dict[str, dict[str, str]] = {
    # Sports.
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
    # Finance / markets.
    "crypto":             {"label": "Crypto",
                           "description": "BTC, ETH, SOL, altcoins. Price moves, exchange events, token unlocks."},
    "crypto_short":       {"label": "Crypto micro-window",
                           "description": "5-30 minute \"Up or Down\" direction markets settled on a single price tick. Default-skipped: research is per-event, the market is per-window, so the forecaster has no information advantage."},
    "stocks":             {"label": "Stocks",
                           "description": "Equity prices, IPOs, earnings, S&P / NASDAQ / index moves."},
    "macro":              {"label": "Macro",
                           "description": "Fed decisions, rate cuts, CPI, GDP, unemployment, monetary policy."},
    "fx_commodities":     {"label": "FX & commodities",
                           "description": "Currency pairs, gold, oil, natural gas, agricultural commodities."},
    # Politics / society.
    "election":           {"label": "Election",
                           "description": "Presidential, senate, house, governor, primary races. Vote shares and outcomes."},
    "policy_event":       {"label": "Policy event",
                           "description": "Bills, executive orders, court rulings, impeachments, tariffs, sanctions."},
    "geopolitical_event": {"label": "Geopolitical event",
                           "description": "Wars, treaties, ceasefires, coups, diplomatic accords."},
    # Tech / culture.
    "tech_release":       {"label": "Tech release",
                           "description": "AI model launches, product releases, SpaceX flights, new APIs."},
    "awards":             {"label": "Awards",
                           "description": "Oscars, Emmys, Grammys, Cannes, Nobel, Pulitzer, MVP races."},
    "entertainment":      {"label": "Entertainment",
                           "description": "Box office, streaming, music charts, album releases, tour numbers."},
    # Catch-alls.
    "weather_event":      {"label": "Weather event",
                           "description": "Hurricanes, tornadoes, snowfall thresholds, temperature records."},
    "price_threshold":    {"label": "Price threshold (other)",
                           "description": "Generic 'will X cross $Y by Z' markets that aren't crypto/stocks/macro/FX."},
    "activity_count":     {"label": "Activity count",
                           "description": "Counts of public activity (executive orders, posts, hires, layoffs) by date."},
    "binary_event":       {"label": "Other events",
                           "description": "Yes/no markets that don't fit the categories above."},
}


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for datetime / set / bytes."""
    if isinstance(obj, datetime):
        # Anchor naive datetimes to UTC. SQLite's CURRENT_TIMESTAMP
        # is UTC by spec, and SQLAlchemy returns naive Python datetime
        # objects from those columns. Without the explicit offset, JS
        # `new Date(...)` interprets the bare ISO string as LOCAL
        # time, which puts the equity chart's hover tooltip 8 hours
        # behind the real settlement time for users in UTC+8.
        from datetime import timezone
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
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


def _log_bot_toggle_request(action: str, req: web.Request) -> None:
    """Audit log every POST /api/bot/start | /api/bot/stop.

    Captures who is asking the daemon to flip bot_enabled. The flag
    is the single most consequential user-facing setting and we hit
    a bug 2026-05-14 where it was flipping ON without an explicit
    user action — every callable code path needs to be traceable so
    we can chase that down.

    Logged fields: HTTP method, remote peer address, User-Agent
    (Tauri webview vs curl vs something else), Referer (which UI
    page issued the request), and the asyncio task name (often
    contains the handler call site). Output goes to stderr with
    flush=True so it lands in the daemon's launchd-captured log
    immediately rather than getting buffered behind the next
    write.
    """
    import asyncio as _asyncio
    try:
        task_name = _asyncio.current_task().get_name()
    except Exception:
        task_name = "?"
    headers = req.headers
    ua = headers.get("User-Agent", "?")
    ref = headers.get("Referer", "?")
    xff = headers.get("X-Forwarded-For", "?")
    try:
        peer = req.transport.get_extra_info("peername") or ("?", "?")
    except Exception:
        peer = ("?", "?")
    print(
        f"[bot_toggle_audit] action=/api/bot/{action} "
        f"method={req.method} peer={peer[0]}:{peer[1]} "
        f"task={task_name} ua={ua!r} referer={ref!r} xff={xff!r}",
        file=sys.stderr, flush=True,
    )


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

    def __init__(
        self,
        analyst,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        watchdog: Optional[Any] = None,
    ) -> None:
        self._analyst = analyst
        self._host = host
        self._requested_port = port
        self._scheduler = None
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._bound_port: int = 0
        # Optional handle to the loop watchdog so /api/health can
        # surface pump latency. Any-typed to avoid hardcoding the
        # watchdog's import in this module's import graph.
        self._watchdog = watchdog

        # Dedicated threadpool for INTERACTIVE GUI endpoints
        # (/api/state, /api/summary, /api/credentials, /api/config,
        # /api/positions...). Isolated from the default loop
        # executor so that the analyst's heavy LLM + research work
        # cannot starve the dashboard. Sized at 8 workers — enough
        # for half a dozen simultaneous dashboard fetches with
        # headroom; small enough that runaway sync work in a single
        # handler can't lock up the rest of the threadpool.
        #
        # Before this: every offload went through the default
        # ThreadPoolExecutor (32 workers, shared with APScheduler).
        # When the analyst was mid-scan it could hold most of those
        # workers for 30-60s doing per-market LLM calls. Dashboard
        # fetches queued behind that work and timed out at 30s on
        # the React side, surfacing as the "/api/state timed out
        # after 30s. The sidecar may be stuck" banner.
        from concurrent.futures import ThreadPoolExecutor as _TPE
        self._api_executor = _TPE(
            max_workers=8,
            thread_name_prefix="delfi-api",
        )

    def set_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    async def start(self) -> int:
        """Bind the socket and start serving. Returns the actual bound port."""
        self._app = web.Application(middlewares=[
            self._timeout_middleware,
            self._slow_handler_middleware,
        ])
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
        app.router.add_get("/api/open-orders", self._get_open_orders)
        app.router.add_get("/api/events",      self._get_events)
        app.router.add_post("/api/bot/start",  self._bot_start)
        app.router.add_post("/api/bot/stop",   self._bot_stop)
        app.router.add_post("/api/scan",       self._scan)

        # Performance + learning
        app.router.add_get("/api/summary",          self._get_summary)
        app.router.add_get("/api/calibration",      self._get_calibration)
        app.router.add_get("/api/brier-trend",      self._get_brier_trend)
        app.router.add_get("/api/suggestions",      self._get_suggestions)
        app.router.add_get("/api/suggestions/history",
                            self._get_suggestions_history)
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

        # Telegram outbound notifications (BYO bot via @BotFather).
        app.router.add_get("/api/config/telegram",        self._get_telegram_config)
        app.router.add_put("/api/config/telegram",        self._put_telegram_config)
        app.router.add_post("/api/config/telegram/test",  self._post_telegram_test)
        app.router.add_post("/api/config/telegram/disconnect",
                            self._post_telegram_disconnect)

        # License (Lemon Squeezy hard gate). The React shell calls
        # /api/license/status on every mount and shows the
        # LicenseGate until status reports valid=true.
        app.router.add_get("/api/license/status",         self._get_license_status)
        app.router.add_post("/api/license/activate",      self._post_license_activate)
        app.router.add_post("/api/license/deactivate",    self._post_license_deactivate)

        # Simulation reset (zeros out simulation positions + bankroll)
        app.router.add_post("/api/reset-simulation",      self._reset_simulation)

        # Auto-start at login. Backed by the LaunchAgent at
        # ~/Library/LaunchAgents/com.delfi.bot.plist (created by
        # install.sh). GET reports whether the agent is currently
        # bootstrapped; PUT toggles bootstrap/bootout via launchctl.
        # macOS-only - other platforms return supported=false.
        app.router.add_get("/api/system/autostart",  self._get_autostart)
        app.router.add_put("/api/system/autostart",  self._put_autostart)

        # System operations (restart, logs, backup, launch stats,
        # login item / window-at-login). All are user-initiated from
        # Settings > Account.
        app.router.add_post("/api/system/restart",    self._post_restart)
        app.router.add_get("/api/system/logs",        self._get_logs)
        app.router.add_post("/api/system/db-backup",  self._post_db_backup)
        app.router.add_get("/api/system/launch-stats", self._get_launch_stats)
        app.router.add_get("/api/system/login-item",  self._get_login_item)
        app.router.add_put("/api/system/login-item",  self._put_login_item)

        # CSV export of positions (Performance page Export button).
        app.router.add_get("/api/positions/csv",      self._get_positions_csv)

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

    # ── Live position mark-to-market ────────────────────────────────────────
    async def _fetch_mtm_position_value(self, user_id: str) -> tuple[float, float]:
        """Current market value of all open live positions.

        Returns (current_value, open_cost):
        - current_value: sum of shares * live_market_price for each position
        - open_cost:     sum of cost_usd (purchase price; used for risk calcs)

        Fetches outcomePrices from the gamma API via synchronous urllib
        calls offloaded to an executor thread. Using urllib (not aiohttp)
        avoids async/event-loop interaction issues inside PyInstaller
        bundles. Partial failures degrade gracefully - failed markets fall
        back to cost_usd so the dashboard never shows $0.
        """
        import json as _j
        import urllib.request as _ur

        def _open_rows():
            with get_engine().connect() as conn:
                return conn.execute(text(
                    "SELECT market_id, side, shares, cost_usd "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = 'live' AND status = 'open'"
                ), {"uid": user_id}).fetchall()

        rows = await self._offload(_open_rows)
        if not rows:
            return 0.0, 0.0

        open_cost = sum(float(r[3]) for r in rows)
        market_ids = list({str(r[0]) for r in rows})

        GAMMA = "https://gamma-api.polymarket.com"

        def _fetch_prices(mids: list) -> dict:
            """Synchronous: fetch outcomePrices for each market_id."""
            result: dict[str, dict[str, float]] = {}
            for mid in mids:
                try:
                    req = _ur.Request(
                        f"{GAMMA}/markets/{mid}",
                        headers={"User-Agent": "Delfi/1.0"},
                    )
                    with _ur.urlopen(req, timeout=6) as resp:
                        d = _j.loads(resp.read())
                    # outcomePrices is a JSON-encoded string in gamma responses.
                    outcomes = _j.loads(d.get("outcomes") or "[]")
                    prices   = _j.loads(d.get("outcomePrices") or "[]")
                    result[str(mid)] = {
                        str(o).upper(): float(p)
                        for o, p in zip(outcomes, prices)
                    }
                except Exception:
                    result[str(mid)] = {}
            return result

        try:
            price_map = await self._offload(_fetch_prices, market_ids)
        except Exception:
            price_map = {}

        current_value = 0.0
        for market_id, side, shares, cost_usd in rows:
            pm = price_map.get(str(market_id), {})
            price = pm.get(str(side).upper())
            if price and float(price) > 0:
                current_value += float(shares) * float(price)
            else:
                current_value += float(cost_usd)

        return current_value, open_cost

    # ── Executor offload helper ─────────────────────────────────────────────
    async def _offload(self, fn, *args, **kwargs):
        """Run a sync callable on the API-dedicated executor.

        Every handler that touches SQLite, the keychain, or the
        filesystem MUST go through here. Sync calls on the asyncio
        loop accumulate latency, and a single slow call (SQLite WAL
        contention with the scheduler, a half-open keychain prompt)
        wedges every other endpoint until it returns. The 5-hour
        outage of 2026-05-06 was the trigger to make this universal.

        Uses `_api_executor` (8 workers) instead of the default
        ThreadPoolExecutor, so heavy work scheduled by APScheduler
        / pm_analyst can never starve the dashboard. The default
        executor still serves background jobs.
        """
        loop = asyncio.get_event_loop()
        if args or kwargs:
            return await loop.run_in_executor(
                self._api_executor, lambda: fn(*args, **kwargs)
            )
        return await loop.run_in_executor(self._api_executor, fn)

    # ── Middleware ──────────────────────────────────────────────────────────
    @web.middleware
    async def _timeout_middleware(self, request, handler):
        """Hard 25s ceiling on every handler.

        Without this, a handler that awaits forever (executor task
        that never returns, pending future that never resolves)
        leaks the connection: the GUI gives up, sends FIN, but the
        daemon never calls close() because the handler is still
        running. Each leak holds a fd + an executor worker + an
        asyncio task. Six of those is enough to wedge accept() in
        practice (CLOSE_WAIT seen 2026-05-06 incident).

        25s is deliberately under the React fetch wrapper's 30s
        ceiling (src/api.ts). The client and server racing the same
        deadline means the client almost always wins (network
        latency loss), the client sends FIN, and the server has no
        chance to write a 504 - same CLOSE_WAIT leak in a different
        outfit. Five seconds of cushion lets the server's 504 reach
        the client before its abort fires.

        Even worst-case Lemon Squeezy license round-trips fit in
        10s. Anything past 25s is a bug.
        """
        try:
            return await asyncio.wait_for(handler(request), timeout=25.0)
        except asyncio.TimeoutError:
            import sys as _sys
            print(
                f"[handler-timeout] {request.method} {request.path} "
                "exceeded 25s and was aborted. Likely a stuck executor "
                "task or a pending future that never resolved.",
                file=_sys.stderr, flush=True,
            )
            return _err(
                f"{request.path} took longer than 25s on the server. "
                "Try again; if it persists, restart Delfi from Settings.",
                504,
            )

    @web.middleware
    async def _slow_handler_middleware(self, request, handler):
        """Per-request stopwatch.

        Most API handlers complete in <50ms. Anything past 5s is either
        an external HTTP call gone slow, an executor that's saturated,
        or the start of a wedge. We log the path + duration so the
        next sidecar.err tail tells us which endpoint went bad before
        the watchdog fired.

        Doesn't kill the request - aiohttp + the request's own
        deadline handle that. Just observability.
        """
        import time as _t
        started = _t.monotonic()
        try:
            return await handler(request)
        finally:
            elapsed = _t.monotonic() - started
            if elapsed > 5.0:
                import sys as _sys
                print(
                    f"[slow-handler] {request.method} {request.path} "
                    f"took {elapsed:.1f}s",
                    file=_sys.stderr, flush=True,
                )

    # ── Handlers ────────────────────────────────────────────────────────────
    async def _health(self, _req: web.Request) -> web.Response:
        snap = proc_health.snapshot()
        # Surface loop pump latency. The watchdog updates a timestamp
        # every 5s; large values (>10s) mean the loop is starting to
        # wedge. /api/health is the cheapest endpoint, so an external
        # probe (curl in a loop, monitoring tool) can read this without
        # hammering DB-backed handlers.
        if self._watchdog is not None:
            try:
                snap["loop_silence_s"] = round(
                    float(self._watchdog.silence_seconds()), 2
                )
            except Exception:
                pass
        # Open file descriptors. CLOSE_WAIT leaks (handler tasks that
        # never returned) show up here as a steady climb. Healthy
        # baseline is ~150-200 on macOS; >500 means a leak in flight.
        try:
            import os as _os
            snap["open_fd_count"] = len(_os.listdir("/dev/fd"))
        except Exception:
            pass
        return _ok(snap)

    async def _state(self, _req: web.Request) -> web.Response:
        # `get_user_config()` opens a SQLite transaction. Running it
        # inline on the aiohttp event loop blocks every other request
        # under DB contention with the scan/resolve scheduler jobs;
        # the GUI's 30s splash timeout fires and the user sees
        # "Delfi could not start". Offload to the default thread pool.
        cfg = await asyncio.get_event_loop().run_in_executor(
            self._api_executor, get_user_config,
        )
        # `can_trade_live` is gated on `mode == 'live'` (correct for
        # the sizer's "am I actually live right now" check) but that
        # makes it useless for the UI's "can I switch to live"
        # question — there's a chicken-and-egg where the Live button
        # is disabled in simulation, so the user can never flip it.
        # Add a separate `live_creds_ready` that only checks the
        # credentials, independent of current mode. UI uses this to
        # enable the Live toggle.
        live_creds_ready = bool(
            cfg.wallet_address
            and _keyring_get(KEYRING_POLYMARKET_KEY) is not None
        )

        # idle_reason: when the scan is being skipped to save LLM tokens
        # because the user has nothing to spend, surface that on the
        # dashboard so they see a clear "the bot paused itself, here's
        # why" message instead of silent inaction. Today the only
        # programmatic idle state is insufficient_bankroll; other halts
        # (bot_enabled=False, /pause, circuit breaker) are reflected by
        # the existing bot_enabled / ready_to_trade fields.
        #
        # Cheap because the wallet probe behind it is cached with a 60s
        # TTL; offloaded to the executor anyway so a cold-cache call
        # can't block the aiohttp event loop.
        idle_reason: Optional[str] = None
        try:
            from engine.pm_analyst import is_scan_idle_for_bankroll
            if await asyncio.get_event_loop().run_in_executor(
                self._api_executor, is_scan_idle_for_bankroll,
            ):
                idle_reason = "insufficient_bankroll"
        except Exception:
            # Fail-quiet: idle_reason stays None and the dashboard
            # shows no banner. The scan gate itself is the source of
            # truth; this is just a UI hint.
            pass

        return _ok({
            "mode": cfg.mode,
            "bot_enabled": bool(getattr(cfg, "bot_enabled", False)),
            "ready_to_trade": bool(getattr(cfg, "ready_to_trade", False)),
            "starting_cash": cfg.starting_cash,
            "wallet_address": cfg.wallet_address,
            "is_onboarded": cfg.is_onboarded,
            "can_trade_live": cfg.can_trade_live,
            "live_creds_ready": live_creds_ready,
            "idle_reason": idle_reason,
            "uptime_s": proc_health.uptime_seconds,
            "started_at": (proc_health.start_time.isoformat()
                           if proc_health.start_time else None),
            "error_count": proc_health.error_count,
        })

    async def _get_config(self, _req: web.Request) -> web.Response:
        cfg = await asyncio.get_event_loop().run_in_executor(
            self._api_executor, get_user_config,
        )
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
            cfg = await self._offload(get_user_config)
            return _ok(_config_to_dict(cfg))

        # Snapshot the prior mode so we can detect a flip and notify
        # Telegram after the write succeeds. Offloaded because
        # get_user_config touches SQLite; doing it inline would stall
        # the aiohttp event loop under scheduler write contention.
        prior_cfg = await self._offload(get_user_config)
        prior_mode = (prior_cfg.mode or "").strip().lower() if prior_cfg else ""

        try:
            cfg = await self._offload(lambda: update_user_config(**changes))
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            # SQLAlchemy / sqlite errors land here. Don't let aiohttp's
            # default handler return a text/plain 500: the React UI
            # expects JSON for every response.
            return _err(f"update failed: {exc}", 500)

        new_mode = (cfg.mode or "").strip().lower() if cfg else ""
        if new_mode and prior_mode and new_mode != prior_mode:
            # Mode flip. Surface to whatever Telegram chat the user
            # has wired (no-op if Telegram isn't configured). Best
            # effort: a notifier outage must never block or fail the
            # config update. The DB write is the source of truth; the
            # GUI is about to refresh state anyway.
            self._notify_mode_switch_async(prior_mode, new_mode)

        return _ok(_config_to_dict(cfg))

    def _notify_mode_switch_async(self, prior_mode: str, new_mode: str) -> None:
        """Fire a Telegram message about a SIMULATION ↔ LIVE flip.

        Runs on the default thread-pool because telegram_notifier.notify
        is sync (it does a blocking HTTPS POST to api.telegram.org).
        Wrapped so a notifier exception never leaks back into the
        request handler.
        """
        def _send() -> None:
            try:
                from feeds.telegram_notifier import notify as _tg_notify
            except Exception:
                return  # telegram_notifier missing - configured off
            label_old = "Live" if prior_mode == "live" else "Simulation"
            label_new = "Live"  if new_mode  == "live" else "Simulation"
            if new_mode == "live":
                body = (
                    f"<b>Delfi switched to {label_new}</b>\n"
                    f"From: {label_old}\n\n"
                    f"Real-money orders will fire on the next scan if "
                    f"a market clears the forecaster's filter. Make "
                    f"sure your wallet is funded and risk settings "
                    f"are set correctly."
                )
            else:
                body = (
                    f"<b>Delfi switched to {label_new}</b>\n"
                    f"From: {label_old}\n\n"
                    f"Live trading paused. Delfi will keep forecasting "
                    f"but no real-money orders will be placed."
                )
            try:
                _tg_notify(body)
            except Exception as exc:
                print(
                    f"[local_api] telegram mode-switch notify failed: "
                    f"{exc}",
                    file=sys.stderr,
                )

        # Fire-and-forget on the default executor. Don't await; the
        # config-update response should not block on a third-party
        # HTTPS call.
        try:
            asyncio.get_event_loop().run_in_executor(self._api_executor, _send)
        except Exception as exc:
            print(
                f"[local_api] could not dispatch telegram notify: {exc}",
                file=sys.stderr,
            )

    async def _get_credentials(self, _req: web.Request) -> web.Response:
        """Return existence booleans for each keychain credential.

        Two reasons this is more involved than it should be:
          1. We do six keychain reads. Without code signing, every
             rebuild forces macOS to prompt for permission per entry
             ("delfi-sidecar wants to use confidential information").
             A user who's slow to click Always Allow blocks the
             aiohttp event loop here for tens of seconds; the
             frontend then trips its 30s fetch timeout.
          2. keyring's `get_password` is sync. Running it inline in an
             async handler blocks the loop even when prompts don't
             fire.

        Fix: offload all six reads to the default ThreadPoolExecutor
        in one shot, then cache the booleans on the instance so the
        next call short-circuits without ever touching keychain again.
        Cache is per-process (lives until sidecar exit). PUT
        /api/credentials invalidates it after a successful write.

        Note: the get_user_config() read also goes through the
        executor. SQLite read on the loop is fast in steady state,
        but contended with a scheduler thread mid-write it can stall
        long enough to time out the GUI's 30s splash window.
        """
        cfg = await self._offload(get_user_config)

        cache = getattr(self, "_creds_cache", None)
        if cache is not None:
            return _ok({"wallet_address": cfg.wallet_address, **cache})

        def _read_all() -> dict:
            pm_api = get_polymarket_api_creds() or {}
            return {
                "has_polymarket_key":   _keyring_get(KEYRING_POLYMARKET_KEY) is not None,
                "has_anthropic_key":    get_anthropic_api_key()  is not None,
                "has_llm_backup_key":   get_llm_backup_key()     is not None,
                "has_newsapi_key":      get_newsapi_key()        is not None,
                "has_cryptopanic_key":  get_cryptopanic_key()    is not None,
                # New optional keys added 2026-05-17:
                "has_gemini_key":               get_gemini_api_key() is not None,
                "has_polymarket_api_key":       bool(pm_api.get("api_key")),
                "has_polymarket_api_secret":    bool(pm_api.get("api_secret")),
                "has_polymarket_api_passphrase": bool(pm_api.get("api_passphrase")),
                # Relayer API Key (single UUID, separate from Builder
                # API tuple). Enables gasless redemption via the 2-
                # header auth scheme. One-time paste, no wallet
                # MATIC needed.
                "has_polymarket_relayer_api_key": (
                    get_polymarket_relayer_api_key() is not None
                ),
            }

        try:
            booleans = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    self._api_executor, _read_all
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            return _err(
                "keychain access is blocked. macOS may be waiting for you to "
                "click Always Allow in the keychain prompt — check for a "
                "password dialog and try again.",
                503,
            )

        # Vendor-neutral alias surfaced alongside the back-compat name.
        booleans["has_llm_key"] = booleans["has_anthropic_key"]
        self._creds_cache = booleans
        return _ok({"wallet_address": cfg.wallet_address, **booleans})

    async def _put_credentials(self, req: web.Request) -> web.Response:
        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)

        wrote: list[str] = []

        # Whitespace-only input on any credential field is a no-op:
        # the previous implementation skipped validation in that case
        # but still called the keychain setter, which silently
        # overwrote the stored key with whitespace. The UI already
        # trims and only includes fields with content, so the only
        # paths producing whitespace are typos (curl, programmatic
        # callers, automated tests) and they should not corrupt
        # stored secrets.
        def _clean(field: str) -> Optional[str]:
            v = payload.get(field)
            if not isinstance(v, str):
                return None
            s = v.strip()
            return s if s else None

        # Polymarket private key + wallet address. The DB only stores the
        # wallet (the public 0x address); the private key is keychain-only.
        # Validate locally before persisting so a typo can't end up in
        # the keychain unnoticed.
        pm_key = _clean("polymarket_private_key")
        wallet = _clean("wallet_address")
        if pm_key is not None:
            err = _validate_polymarket_private_key(pm_key, wallet)
            if err:
                return _err(err, 400)
        if pm_key is not None or wallet is not None:
            try:
                await self._offload(
                    lambda: set_user_polymarket_creds(
                        private_key=pm_key,
                        wallet_address=wallet,
                    )
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
        llm = _clean("llm_api_key") or _clean("anthropic_api_key")
        if llm is not None:
            err = _validate_llm_key_shape(llm, "LLM API key")
            if err:
                return _err(err, 400)
            try:
                await self._offload(set_anthropic_api_key, llm)
                wrote.append("llm_api_key")
            except Exception as exc:
                return _err(f"failed to write llm key: {exc}", 500)

        # Optional secondary LLM (failover / hedge against rate limits).
        llm_backup = _clean("llm_backup_key")
        if llm_backup is not None:
            err = _validate_llm_key_shape(llm_backup, "Backup LLM key")
            if err:
                return _err(err, 400)
            try:
                await self._offload(set_llm_backup_key, llm_backup)
                wrote.append("llm_backup_key")
            except Exception as exc:
                return _err(f"failed to write llm backup key: {exc}", 500)

        # Optional NewsAPI key (breaking news context).
        newsapi = _clean("newsapi_key")
        if newsapi is not None:
            err = _validate_llm_key_shape(newsapi, "NewsAPI key")
            if err:
                return _err(err, 400)
            try:
                await self._offload(set_newsapi_key, newsapi)
                wrote.append("newsapi_key")
            except Exception as exc:
                return _err(f"failed to write newsapi key: {exc}", 500)

        # Optional CryptoPanic key (crypto-specific news).
        cryptopanic = _clean("cryptopanic_key")
        if cryptopanic is not None:
            err = _validate_llm_key_shape(cryptopanic, "CryptoPanic key")
            if err:
                return _err(err, 400)
            try:
                await self._offload(set_cryptopanic_key, cryptopanic)
                wrote.append("cryptopanic_key")
            except Exception as exc:
                return _err(f"failed to write cryptopanic key: {exc}", 500)

        # Optional Gemini key (research/news pre-filter). The bot
        # logs an explicit "GEMINI_API_KEY not set" warning every
        # scan when missing.
        gemini = _clean("gemini_key") or _clean("gemini_api_key")
        if gemini is not None:
            err = _validate_llm_key_shape(gemini, "Gemini API key")
            if err:
                return _err(err, 400)
            try:
                await self._offload(set_gemini_api_key, gemini)
                wrote.append("gemini_key")
            except Exception as exc:
                return _err(f"failed to write gemini key: {exc}", 500)

        # Optional MANUAL Polymarket CLOB api creds. All three are
        # written independently; the caller can update one at a time.
        # When ALL three are populated, pm_executor + polymarket_wallet
        # skip create_or_derive_api_key entirely and use these
        # directly — useful when the SDK's auto-derived key is stale
        # after V2 migration.
        pm_api_key        = _clean("polymarket_api_key")
        pm_api_secret     = _clean("polymarket_api_secret")
        pm_api_passphrase = _clean("polymarket_api_passphrase")
        # Relayer API Key (separate key class - single UUID created
        # on polymarket.com -> Settings -> Relayer API keys). Used
        # for gasless redeem; writing this enables auto-redeem of
        # every future winning position without the user funding
        # MATIC or pasting Builder API tuples.
        pm_relayer_api_key = _clean("polymarket_relayer_api_key")
        if pm_api_key is not None or pm_api_secret is not None or pm_api_passphrase is not None:
            try:
                await self._offload(
                    lambda: set_polymarket_api_creds(
                        api_key=pm_api_key,
                        api_secret=pm_api_secret,
                        api_passphrase=pm_api_passphrase,
                    )
                )
                if pm_api_key is not None:        wrote.append("polymarket_api_key")
                if pm_api_secret is not None:     wrote.append("polymarket_api_secret")
                if pm_api_passphrase is not None: wrote.append("polymarket_api_passphrase")
            except Exception as exc:
                return _err(f"failed to write polymarket api creds: {exc}", 500)

        # Relayer API Key - independent of the Builder tuple above.
        if pm_relayer_api_key is not None:
            try:
                await self._offload(
                    lambda: set_polymarket_relayer_api_key(pm_relayer_api_key)
                )
                wrote.append("polymarket_relayer_api_key")
            except Exception as exc:
                return _err(
                    f"failed to write polymarket relayer api key: {exc}", 500,
                )

        # Hot-reload the running process so the new keys take effect
        # WITHOUT a daemon restart. Two things need to happen:
        #
        #  1. os.environ — research/fetcher.py and feeds/news_feed.py
        #     read ANTHROPIC_API_KEY / NEWS_API_KEY / CRYPTOPANIC_API_KEY
        #     directly off the env. main.py only seeds these at startup
        #     (`_seed_env_from_keychain`). Without this hot-reload, a
        #     user who saves a new key sees secrets.json updated and
        #     `has_anthropic_key: true`, but the running process still
        #     has the old (or empty) env var — every scan logs "Could
        #     not resolve authentication method" until restart.
        #
        #  2. PMAnalyst.evaluator — PolymarketEvaluator caches an
        #     `anthropic.Anthropic()` client at construction time. The
        #     SDK reads ANTHROPIC_API_KEY into the client THEN and
        #     never re-reads it. Even after we update os.environ, the
        #     cached client still hits the API with no auth header.
        #     Reset the analyst's evaluator so the next evaluate()
        #     constructs a fresh client against the now-current env.
        import os as _os
        if llm is not None:
            _os.environ["ANTHROPIC_API_KEY"] = llm
        if newsapi is not None:
            _os.environ["NEWS_API_KEY"] = newsapi
        if cryptopanic is not None:
            _os.environ["CRYPTOPANIC_API_KEY"] = cryptopanic
        if gemini is not None:
            # research/fetcher.py + feeds/news_feed.py read this
            # directly off os.environ at call time, so this propagates
            # immediately to the next scan / news pull.
            _os.environ["GEMINI_API_KEY"] = gemini
        if (pm_api_key is not None or pm_api_secret is not None
                or pm_api_passphrase is not None):
            # pm_executor's per-process ClobClient cache keys by manual
            # vs auto-derived, but the manual creds it stored were from
            # BEFORE this save. Flush the cache so the next order picks
            # up the freshly-saved creds.
            try:
                from execution.pm_executor import (
                    _CLOB_CLIENT_CACHE, reset_v2_signer_mismatch_state,
                )
                _CLOB_CLIENT_CACHE.clear()
                # Reset the V2 signer-mismatch gate. If the user just
                # pasted Trading API Keys to fix the very rejection that
                # tripped the gate, the next order must be allowed to
                # try them instead of falling straight to simulation.
                reset_v2_signer_mismatch_state()
                # Also clear the api-key-rotation memo so a manual key
                # change doesn't get blocked by "already rotated this
                # context".
                from feeds.polymarket_wallet import _API_KEY_ROTATED_CTX, clear_cache as _pw_clear
                _API_KEY_ROTATED_CTX.clear()
                _pw_clear()
            except Exception as exc:
                print(f"[creds] CLOB client cache flush failed: {exc}",
                      flush=True)
        # All LLM calls (forecaster + research keyword extraction) go
        # through the engine.llm_client singleton. Resetting it drops
        # the cached Anthropic and Gemini SDK clients in one place;
        # the next call constructs fresh against the now-current
        # ANTHROPIC_API_KEY + backup keychain entry. Failover between
        # the two providers is handled internally by llm_client.
        if llm is not None:
            try:
                from engine.llm_client import reset_llm
                reset_llm()
            except Exception as exc:
                print(f"[creds] llm_client reset failed: {exc}",
                      flush=True)

        # Invalidate the cached existence booleans so the next
        # /api/credentials read picks up whatever just got written.
        if hasattr(self, "_creds_cache"):
            self._creds_cache = None

        # Read everything we need for the response in one offloaded
        # batch. Per-getter offload would be 6 round-trips through the
        # executor for one request - measurably slower than batching.
        def _read_all_post_write():
            cfg = get_user_config()
            return {
                "wallet_address":      cfg.wallet_address,
                "has_polymarket_key":  _keyring_get(KEYRING_POLYMARKET_KEY) is not None,
                "has_anthropic_key":   get_anthropic_api_key() is not None,
                "has_llm_backup_key":  get_llm_backup_key() is not None,
                "has_newsapi_key":     get_newsapi_key() is not None,
                "has_cryptopanic_key": get_cryptopanic_key() is not None,
            }
        snap = await self._offload(_read_all_post_write)
        snap["has_llm_key"] = snap["has_anthropic_key"]
        return _ok({"wrote": wrote, **snap})

    async def _get_positions(self, req: web.Request) -> web.Response:
        try:
            limit = int(req.query.get("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))

        # Mode-scoped. User rule (2026-05-16): the dashboard surfaces
        # CURRENT mode only — sim and live ledgers stay separate.
        # Read the current mode here rather than caching it on the
        # handler so a mode toggle mid-session takes effect on the
        # next request.
        current_mode = (
            (await self._offload(get_user_config)).mode or "simulation"
        )

        def _read() -> list[dict]:
            engine = get_engine()
            with engine.connect() as conn:
                stmt = (
                    select(pm_positions)
                    .where(pm_positions.c.mode == current_mode)
                    .order_by(desc(pm_positions.c.created_at))
                    .limit(limit)
                )
                return [dict(r._mapping) for r in conn.execute(stmt)]

        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                self._api_executor, _read,
            )
        except Exception as exc:
            return _err(f"failed to read positions: {exc}", 500)
        return _ok({"positions": rows})

    async def _get_open_orders(self, _req: web.Request) -> web.Response:
        """Read-only proxy for CLOB get_open_orders().

        The Positions page surfaces this as an "Open orders" sub-tab
        so the user can see any unfilled limit orders sitting on the
        Polymarket book - the same orders the reconciler will cancel
        if they age past _STALE_ORDER_AGE_S. Returns an empty list if
        the user has no Polymarket key configured or the CLOB is
        unreachable; never errors the page.
        """
        def _read() -> list[dict]:
            from engine.user_config import (
                get_active_polymarket_creds, get_user_config,
            )
            from execution.pm_executor import _get_clob_client
            cfg = get_user_config()
            creds = get_active_polymarket_creds(cfg)
            pk = (creds.get("private_key") or "").strip()
            wallet = (creds.get("wallet_address") or "").strip()
            if not pk or not wallet:
                return []
            try:
                client = _get_clob_client(wallet, pk)
                if client is None:
                    return []
                orders = client.get_open_orders()  # type: ignore[union-attr]
            except Exception as exc:
                print(f"[open_orders] CLOB fetch failed: {exc}",
                      file=sys.stderr, flush=True)
                return []
            return orders if isinstance(orders, list) else []

        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                self._api_executor, _read,
            )
        except Exception as exc:
            return _err(f"failed to read open orders: {exc}", 500)
        return _ok({"orders": rows})

    async def _get_events(self, req: web.Request) -> web.Response:
        try:
            limit = int(req.query.get("limit", "200"))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 1000))

        def _read() -> list[dict]:
            engine = get_engine()
            with engine.connect() as conn:
                stmt = (
                    select(event_log)
                    .order_by(desc(event_log.c.timestamp))
                    .limit(limit)
                )
                return [dict(r._mapping) for r in conn.execute(stmt)]

        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                self._api_executor, _read,
            )
        except Exception as exc:
            return _err(f"failed to read events: {exc}", 500)
        return _ok({"events": rows})

    async def _bot_start(self, req: web.Request) -> web.Response:
        """Enable the bot (set user_config.bot_enabled = True).

        Validates that the user has the credentials they actually need
        for their current mode before flipping the switch. Live mode
        requires a wallet, a Polymarket private key, and an LLM API
        key. Simulation only needs the LLM key (Delfi still forecasts
        each market, just doesn't fund the trade). Mode-switching
        itself is a separate operation: PUT /api/config with
        `{"mode": "live"}` or `{"mode": "simulation"}`.
        """
        _log_bot_toggle_request("start", req)
        # License is a hard gate — any non-valid status here means
        # the LicenseGate should be re-shown by the React shell. We
        # still return a clear error string so curl users see the
        # reason.
        license_status = self._license_status_payload()
        if not license_status.get("valid"):
            reason = license_status.get("reason") or "license is not valid"
            return _err(f"license check failed: {reason}", 403)

        cfg = await self._offload(get_user_config)
        # Idempotent. A rapid double-click in the GUI used to fire
        # start then stop within the same second (the second click
        # landed on the just-re-rendered "Pause" button). Returning
        # success without re-writing the row also avoids a spurious
        # audit log entry on every redundant call.
        if cfg.bot_enabled:
            return _ok({"bot_enabled": True, "mode": cfg.mode})
        if (await self._offload(get_anthropic_api_key)) is None:
            return _err("LLM API key is not set", 400)
        if cfg.mode == "live":
            if not cfg.wallet_address:
                return _err("wallet_address is not set", 400)
            if (await self._offload(_keyring_get, KEYRING_POLYMARKET_KEY)) is None:
                return _err("polymarket private key is not in the keychain", 400)
        try:
            cfg = await self._offload(lambda: update_user_config(bot_enabled=True))
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            return _err(f"update failed: {exc}", 500)
        return _ok({"bot_enabled": True, "mode": cfg.mode})

    async def _bot_stop(self, req: web.Request) -> web.Response:
        """Disable the bot (set user_config.bot_enabled = False).

        The scheduler keeps running scan/resolve jobs but the executor
        refuses to open new positions while bot_enabled is False (see
        UserConfig.ready_to_trade). Existing positions still settle.
        """
        _log_bot_toggle_request("stop", req)
        # Idempotent. Mirrors _bot_start: a redundant stop is a no-op.
        cfg = get_user_config()
        if not cfg.bot_enabled:
            return _ok({"bot_enabled": False, "mode": cfg.mode})
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
            # Naive `datetime.utcnow()` is interpreted in the scheduler's
            # configured tz; if that ever drifts to local-tz the scan
            # never fires (or fires hours late). Always use tz-aware UTC.
            job.modify(next_run_time=datetime.now(timezone.utc))
        except Exception as exc:
            return _err(f"failed to schedule scan: {exc}", 500)
        return _ok({"queued": True})

    # ── Performance + Learning ────────────────────────────────────────────────
    async def _get_summary(self, _req: web.Request) -> web.Response:
        """
        Headline performance numbers: bankroll, equity, win rate, ROI, Brier.
        Single-user; no X-User-Id needed. Mode is always taken from
        user_config (no view-mode override in v1).

        Cached 5s. The dashboard polls this every ~5s; underlying
        portfolio stats don't change that fast (a trade settle is
        the only meaningful change source, and those fire every
        10-30min). Caching at this layer prevents 6 concurrent
        React components from each spawning a fresh DB+offload pass.
        Cache miss path is unchanged; cache hit returns in <1ms.

        Why this matters: the polymarket-runner + analyst jobs hog
        the default ThreadPoolExecutor for 5-30s at a time during
        a scan. If /api/summary's offload also competes for those
        threads, it queues. The user sees the GUI's 30s timeout
        even though no single handler took 30s. Caching upstream of
        the offload eliminates that contention almost entirely.
        """
        import time as _t
        cache = getattr(self, "_summary_cache", None)
        if cache is not None:
            ts, payload = cache
            if _t.monotonic() - ts < 5.0:
                return _ok(payload)

        def _stats() -> dict:
            executor = PMExecutor(DEFAULT_USER_ID)
            return executor.get_portfolio_stats()

        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                self._api_executor, _stats,
            )
        except Exception as exc:
            return _err(f"failed to compute portfolio stats: {exc}", 500)

        # Brier on this user's settled positions, scoped to the
        # CURRENT mode. Source filter is 'polymarket' to match the
        # bucket that drives sizing decisions. Mode-scoping was added
        # 2026-05-16 alongside the rest of the per-mode dashboard
        # changes — Brier shown on the Dashboard hero must reflect
        # only the trades you actually took in this mode.
        _stats_mode = stats.get("mode") or "simulation"
        try:
            brier = await asyncio.get_event_loop().run_in_executor(
                self._api_executor,
                lambda: calibration.get_report(
                    source="polymarket", user_id=DEFAULT_USER_ID,
                    mode=_stats_mode,
                ),
            )
        except Exception:
            brier = {"brier": None, "resolved": 0, "total": 0}

        # Live-mode balance overlay. In simulation the synthetic
        # starting_cash is correct ("how would Delfi do with $1000?")
        # but in live we MUST show what's actually spendable on
        # Polymarket. Funds deposited via the Polymarket UI sit in a
        # proxy / Magic Account, NOT at the user's EOA — so a direct
        # USDC eth_call against the EOA returns 0 even when the user
        # is funded. The authoritative source is Polymarket's CLOB
        # `/balance-allowance` endpoint, which knows the proxy and
        # returns the collateral the bot can actually trade with.
        #
        # CRITICAL: this overlay reads from the in-process cache
        # ONLY - never the network. A separate scheduled job
        # (pm_balance_refresh in main.py, 60s cadence) refreshes
        # the cache in the background. Before this change, every
        # /api/summary poll could trigger a fresh probe; a slow DNS
        # or SSL call on that probe wedged the daemon for 30s+ and
        # the dashboard kept showing "sidecar may be stuck". Moving
        # the refresh off the user-request path means /api/summary
        # always returns in milliseconds - at worst the bankroll
        # number is up to 60s stale on a network blip.
        bankroll = stats.get("bankroll")
        equity   = stats.get("equity")
        open_cost = float(stats.get("open_cost") or 0.0)
        if stats.get("mode") == "live":
            try:
                from feeds.polymarket_wallet import (
                    get_cached_total_funder_balance,
                )
                pm_key = await self._offload(
                    _keyring_get, KEYRING_POLYMARKET_KEY,
                )
                if pm_key:
                    # Total funder balance = pUSD (V2 tradeable now) +
                    # USDC.e (legacy, auto-activated by
                    # pm_activate_legacy within ~10 min). The earlier
                    # version reported only pUSD, which hid winnings
                    # that had landed as USDC.e and made the Balance
                    # number on dashboards / Telegram look wrong
                    # whenever a V1-collateral market settled.
                    # Non-blocking, no lock, no network.
                    total_balance = get_cached_total_funder_balance(pm_key)
                    if total_balance is not None:
                        bankroll = float(total_balance)
                        equity = bankroll + open_cost
            except Exception as exc:
                # Should not be reachable now that the call is non-
                # blocking and side-effect-free, but keep the guard.
                print(f"[summary] live balance overlay failed: {exc}",
                      flush=True)

        starting = float(stats.get("starting_cash") or 0.0)
        realized = float(stats.get("realized_pnl") or 0.0)

        # In LIVE mode, the legacy `starting_cash` from user_config is
        # whatever the user typed at SIM onboarding (typically $1000).
        # That number has nothing to do with live trading - it makes
        # the Dashboard's Risk gauges nonsense ("drawdown 99.1%" when
        # the user just deposited $9, exposure cap "$900" when the
        # user has $9 of collateral). Override it to a value that
        # matches the live session:
        #
        #     live_starting = bankroll - realized_pnl + open_cost
        #
        # Rationale: bankroll = starting + realized_pnl - open_cost,
        # so the formula above gives back the user's effective
        # starting capital for this live session. With no trades yet,
        # live_starting == bankroll and drawdown = 0%. As trades
        # settle the drawdown gauge tracks the right thing.
        if stats.get("mode") == "live":
            open_cost = float(stats.get("open_cost") or 0.0)
            live_starting = bankroll - realized + open_cost
            # Floor at $1 so the downstream gauge math (which divides
            # by `starting`) doesn't choke when bankroll is briefly
            # zero (e.g. mid-deposit).
            starting = max(1.0, live_starting)

        roi = (realized / starting) if starting > 0 else None

        # Unrealized P&L = mark-to-market value of currently-open
        # positions minus their cost basis. open_cost is the data-api
        # MTM sum, bot_open_cost is the DB cost basis sum. Their diff
        # is the unrealized gain/loss. Total P&L matches Polymarket's
        # "All-Time Profit/Loss" tile, which is (current portfolio
        # value - cumulative deposits) - i.e. realized + unrealized
        # over every trade ever made. Without this, Delfi's
        # realized-only number underrepresents performance against
        # the Polymarket UI any time positions are open at a gain.
        open_cost_mtm   = float(stats.get("open_cost") or 0.0)
        open_cost_basis = float(stats.get("bot_open_cost") or 0.0)
        unrealized_pnl  = open_cost_mtm - open_cost_basis
        total_pnl       = realized + unrealized_pnl

        payload = {
            "mode":           stats.get("mode"),
            "bankroll":       bankroll,
            "equity":         equity,
            "starting_cash":  starting,
            "open_positions": stats.get("open_positions"),
            # open_cost is the user-facing "Locked Capital" number:
            # sum of current market value of EVERY position the wallet
            # holds (bot-opened + manually-opened on Polymarket).
            # Source is Polymarket's data-api in live mode, the DB sum
            # in sim mode. bot_open_cost is the bot-tracked subset
            # used for the bot's own P&L bookkeeping; surfaces that
            # want to show "tracked vs untracked" can subtract.
            "open_cost":      stats.get("open_cost"),
            "bot_open_cost":  stats.get("bot_open_cost"),
            "settled_total":  stats.get("settled_total"),
            "settled_wins":   stats.get("settled_wins"),
            "skipped_total":  stats.get("skipped_total"),
            "win_rate":       stats.get("win_rate"),
            "realized_pnl":   stats.get("realized_pnl"),
            "unrealized_pnl": unrealized_pnl,
            "total_pnl":      total_pnl,
            "roi":            roi,
            "brier":          brier.get("brier"),
            "resolved_predictions": brier.get("resolved"),
            "total_predictions":    brier.get("total"),
        }
        self._summary_cache = (_t.monotonic(), payload)
        return _ok(payload)

    async def _get_calibration(self, req: web.Request) -> web.Response:
        """Full calibration report (Brier + reliability buckets).

        Mode-scoped to the user's current trading mode — the
        Performance page's By Category / By Horizon / By Price-Band
        tables now reflect only the trades you actually took in
        this mode. (User rule 2026-05-16: no sim/live data
        mixing.)
        """
        source = req.query.get("source") or "polymarket"
        since_raw = req.query.get("since_days")
        since_int = int(since_raw) if since_raw and since_raw.isdigit() else None
        current_mode = (
            (await self._offload(get_user_config)).mode or "simulation"
        )
        try:
            report = await asyncio.get_event_loop().run_in_executor(
                self._api_executor,
                lambda: calibration.get_report(
                    source=None if source == "all" else source,
                    since_days=since_int,
                    user_id=DEFAULT_USER_ID,
                    mode=current_mode,
                ),
            )
        except Exception as exc:
            return _err(f"calibration query failed: {exc}", 500)
        return _ok(report)

    async def _get_brier_trend(self, _req: web.Request) -> web.Response:
        """
        Running Brier over settled positions, in chronological order.
        Used to show whether the forecaster is getting more or less
        calibrated as more data accumulates. Mode-scoped — the
        Performance page shows the curve for the user's current
        mode only (no sim/live bleed).
        """
        current_mode = (
            (await self._offload(get_user_config)).mode or "simulation"
        )

        def _query() -> list:
            with get_engine().begin() as conn:
                return conn.execute(text(
                    "SELECT settled_at, claude_probability, side, "
                    "       CASE WHEN settlement_outcome = side THEN 1 ELSE 0 END "
                    "FROM pm_positions "
                    "WHERE user_id = :uid "
                    "  AND mode = :m "
                    "  AND settled_at IS NOT NULL "
                    "  AND claude_probability IS NOT NULL "
                    "  AND settlement_outcome IN ('YES', 'NO') "
                    "ORDER BY settled_at ASC"
                ), {"uid": DEFAULT_USER_ID, "m": current_mode}).fetchall()

        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                self._api_executor, _query,
            )
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
            # SQLite returns DATETIME columns as strings under raw
            # iso_utc anchors the SQLite-returned datetime string with
            # an explicit UTC offset so the JS Date parser doesn't
            # fall back to local-time interpretation. See
            # db.engine.iso_utc.
            points.append({
                "date":  iso_utc(r[0]),
                "brier": round(running / i, 4),
                "n":     i,
            })
        return _ok({"points": points})

    async def _get_suggestions(self, req: web.Request) -> web.Response:
        """List pending V1-multiplier proposals from the learning cadence."""
        include_snoozed = req.query.get("include_snoozed", "1") != "0"
        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                self._api_executor,
                lambda: list_pending_suggestions(
                    DEFAULT_USER_ID, include_snoozed=include_snoozed,
                ),
            )
        except Exception as exc:
            return _err(f"failed to list suggestions: {exc}", 500)
        return _ok({"suggestions": rows})

    async def _get_suggestions_history(self, req: web.Request) -> web.Response:
        """List historically resolved (applied/skipped) proposals.

        The Intelligence page reads this so it can show "you've seen
        proposals before" context instead of the brand-new-user empty
        state when every prior proposal has already been actioned.
        """
        try:
            limit = int(req.query.get("limit", "20"))
        except ValueError:
            limit = 20
        limit = max(1, min(200, limit))
        try:
            rows = await asyncio.get_event_loop().run_in_executor(
                self._api_executor,
                lambda: list_resolved_suggestions(DEFAULT_USER_ID, limit=limit),
            )
        except Exception as exc:
            return _err(f"failed to list resolved suggestions: {exc}", 500)
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
            result = await loop.run_in_executor(
                self._api_executor, lambda: fn(sid, **kwargs),
            )
        except TypeError:
            # Older signatures may not accept wait_trades; retry without.
            kwargs.pop("wait_trades", None)
            try:
                result = await loop.run_in_executor(
                    self._api_executor, lambda: fn(sid, **kwargs),
                )
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
                self._api_executor,
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
        cfg = await self._offload(get_user_config)
        skip_set = set(cfg.archetype_skip_list or ())
        mults = dict(cfg.archetype_stake_multipliers or {})
        # Per-archetype price-band overrides. Stored as a tuple of
        # (lo, hi) tuples on the dataclass; flatten to JSON-friendly
        # list-of-lists per archetype on the way out.
        arch_bands = dict(cfg.archetype_skip_market_price_bands or {})

        out = []
        for arch in ARCHETYPES:
            meta = ARCHETYPE_META.get(arch, {"label": arch, "description": ""})
            default_mult = float(V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS.get(arch, 1.0))
            default_skip = arch in V1_DEFAULT_ARCHETYPE_SKIP_LIST
            current_mult = float(mults.get(arch, default_mult))
            bands_tuple = arch_bands.get(arch, ())
            out.append({
                "id":             arch,
                "label":          meta["label"],
                "description":    meta["description"],
                "skip":           arch in skip_set,
                "multiplier":     current_mult,
                "default_skip":   default_skip,
                "default_mult":   default_mult,
                "bands":          [list(p) for p in bands_tuple],
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

        def _read():
            engine = get_engine()
            with engine.connect() as conn:
                stmt = (
                    select(market_evaluations)
                    .order_by(desc(market_evaluations.c.evaluated_at))
                    .limit(limit)
                )
                return [dict(r._mapping) for r in conn.execute(stmt)]
        try:
            rows = await self._offload(_read)
        except Exception as exc:
            return _err(f"failed to read evaluations: {exc}", 500)
        return _ok({"evaluations": rows})

    # ── Notification prefs (per-category toggles) ───────────────────────
    async def _get_notifications(self, _req: web.Request) -> web.Response:
        cfg = await self._offload(get_user_config)
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
            await self._offload(lambda: update_user_config(notification_prefs=prefs))
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            return _err(f"update failed: {exc}", 500)

        cfg = await self._offload(get_user_config)
        full = {cat: bool((cfg.notification_prefs or {}).get(cat, True))
                for cat in NOTIFICATION_CATEGORIES}
        return _ok({
            "categories":         list(NOTIFICATION_CATEGORIES),
            "notification_prefs": full,
        })

    # ── Telegram outbound notifications ─────────────────────────────────
    async def _get_telegram_config(self, _req: web.Request) -> web.Response:
        """Return whether Telegram is configured + the chat id.

        We never return the bot token. The UI cares only about the
        binary state (configured or not); the actual token is read by
        the notifier from the keychain on each send.
        """
        return _ok(await self._offload(get_user_telegram_config))

    async def _put_telegram_config(self, req: web.Request) -> web.Response:
        """Save the bot token + chat id without testing first.

        Restarts the inbound command listener so /help, /status etc
        start working against the new creds without an app restart.
        """
        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)
        token = payload.get("bot_token")
        chat_id = payload.get("chat_id")
        if token is not None and not isinstance(token, str):
            return _err("bot_token must be a string", 400)
        if chat_id is not None and not isinstance(chat_id, str):
            return _err("chat_id must be a string", 400)
        try:
            await self._offload(
                lambda: set_user_telegram_config(bot_token=token, chat_id=chat_id)
            )
        except ValueError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            return _err(f"update failed: {exc}", 500)
        # Best-effort: pick up the new creds. Failures are non-fatal -
        # outbound notifications still work, just not commands.
        try:
            from feeds.telegram_notifier import start_command_listener
            await self._offload(start_command_listener)
        except Exception as exc:
            print(f"[telegram] listener restart failed: {exc}", file=sys.stderr)
        return _ok(await self._offload(get_user_telegram_config))

    async def _post_telegram_test(self, req: web.Request) -> web.Response:
        """Send a real probe message. NEVER persists.

        Resolves the (token, chat_id) pair as follows:
          - Use whatever the request body contains, if non-empty.
          - Otherwise fall back to the currently saved keychain +
            user_config values.

        That lets the UI offer two flows:
          - "Save and then Test" (form empty after save → uses saved)
          - "Test before saving" (form filled → uses form)

        Returns 200 on success or 400 with the Telegram error string.
        """
        try:
            payload = await req.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        token = (payload.get("bot_token") or "").strip()
        chat_id = (payload.get("chat_id") or "").strip()

        # Fall back to saved values when the form fields are blank.
        # The token field is masked when one is saved, so an empty
        # `bot_token` in the body just means "use the saved one".
        if not token:
            from engine.user_config import get_telegram_bot_token
            token = ((await self._offload(get_telegram_bot_token)) or "").strip()
        if not chat_id:
            cfg = await self._offload(get_user_config)
            chat_id = (cfg.telegram_chat_id or "").strip()

        if not token:
            return _err("no bot token configured", 400)
        if not chat_id:
            return _err("no chat id configured", 400)

        # Use aiohttp directly so the timeout is enforced inside the
        # event loop and the request can't wedge an executor thread on
        # a slow TLS handshake (the urllib path was hanging on certain
        # PyInstaller builds, even with timeout= set).
        import aiohttp
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = {
            "chat_id":    chat_id,
            "text":       "Delfi test message. You'll see trades, settlements, and risk events here.",
            "parse_mode": "HTML",
        }
        ok = False
        err: Optional[str] = None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
                async with sess.post(url, json=body) as resp:
                    data = await resp.json(content_type=None)
                    if data.get("ok"):
                        ok = True
                    else:
                        err = str(data.get("description") or f"HTTP {resp.status}")
        except aiohttp.ClientConnectorError as exc:
            err = f"could not reach Telegram: {exc}"
        except asyncio.TimeoutError:
            err = "Telegram took too long to respond (timeout)"
        except Exception as exc:
            err = f"send failed: {exc}"
        if not ok:
            return _err(err or "telegram test failed", 400)

        return _ok({"ok": True})

    async def _post_telegram_disconnect(self, _req: web.Request) -> web.Response:
        """Wipe the Telegram bot token + chat id.

        Stops the inbound command listener too - otherwise it'd keep
        long-polling Telegram with stale creds until process exit.
        """
        try:
            set_user_telegram_config(clear=True)
        except Exception as exc:
            return _err(f"disconnect failed: {exc}", 500)
        try:
            from feeds.telegram_notifier import stop_command_listener
            stop_command_listener()
        except Exception as exc:
            print(f"[telegram] listener stop failed: {exc}", file=sys.stderr)
        return _ok(get_user_telegram_config())

    # ── License (offline Ed25519 hard gate) ─────────────────────────────
    def _license_status_payload(self) -> dict:
        """Pack the cached state into a small dict for the LicenseGate.

        The React shell cares about three things:
          - is the app gated right now (`valid` boolean)
          - if not, why ("reason" string for inline display)
          - was a key ever activated ("has_key" - controls whether to
            show "Paste your license key" or "Re-activate")

        Under the offline Ed25519 model we re-verify the cached blob
        on every call. The crypto check is sub-millisecond, so there
        is no value in trusting a stale "good last week" stamp; this
        also means a build whose embedded public key gets rotated
        will immediately gate the app until a new license is pasted.
        """
        key = get_license_key() or ""
        meta = get_license_meta() or {}

        if not key:
            return {
                "valid": False,
                "reason": "no license key activated",
                "has_key": False,
                "last_validated_at": None,
                "instance_id": None,
                "email": None,
            }

        result = verify_license(key)
        if not result.valid:
            return {
                "valid": False,
                "reason": result.error or "license is not valid",
                "has_key": True,
                "last_validated_at": meta.get("last_validated_at"),
                "instance_id": meta.get("instance_id"),
                "email": (meta.get("payload") or {}).get("email"),
            }

        # Verification succeeded; refresh the cached meta so admin
        # tooling has a current timestamp.
        refreshed = fresh_meta_for(result.payload or {})
        try:
            set_license_meta(refreshed)
        except Exception:
            # Persisting the refresh is best-effort; verification is
            # what gates the bot.
            pass

        return {
            "valid": True,
            "reason": None,
            "has_key": True,
            "last_validated_at": refreshed["last_validated_at"],
            "instance_id": refreshed["instance_id"],
            "email": (result.payload or {}).get("email"),
        }

    async def _get_license_status(self, _req: web.Request) -> web.Response:
        return _ok(self._license_status_payload())

    async def _post_license_activate(self, req: web.Request) -> web.Response:
        """Store + verify a signed license blob.

        First-paste flow:
          1. POST /api/license/activate {"license_key": "<blob>"}
          2. Sidecar verifies the Ed25519 signature offline.
          3. On valid: store key + meta in keychain, return 200.
          4. On invalid: return 400 with the verifier's error
             message; do NOT store the blob (a stored bad blob
             would just keep the gate open with no path forward).
        """
        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)

        key = (payload.get("license_key") or "").strip()
        if not key:
            return _err("license_key is required", 400)

        result = verify_license(key)
        if not result.valid:
            return _err(result.error or "license is not valid", 400)

        set_license_key(key)
        set_license_meta(fresh_meta_for(result.payload or {}))
        return _ok(self._license_status_payload())

    async def _post_license_deactivate(self, _req: web.Request) -> web.Response:
        """Sign this device out of its license.

        Under the offline Ed25519 model there is no online activation
        slot to release; we just wipe the local keychain so the
        LicenseGate re-shows. The same paid blob can be pasted again
        on this or any other machine the user owns.
        """
        set_license_key(None)
        set_license_meta(None)
        return _ok(self._license_status_payload())

    # ── Reset simulation ────────────────────────────────────────────────
    async def _reset_simulation(self, _req: web.Request) -> web.Response:
        """Wipe simulation-mode positions so the user can start a fresh sim.

        Live positions are untouched - the SQL is mode-scoped. Shared
        research artifacts (market_evaluations, markouts, news,
        sentiment, macro) are also untouched: an evaluation cost a
        real Anthropic call and the forecast it produced doesn't
        change based on whether you'd play it with paper or USDC.
        Reset is for the sandbox's money ledger only; the bot's
        accumulated knowledge about markets stays.

        Earlier behaviour also issued `DELETE FROM markouts WHERE
        evaluation_id IN (every-evaluation-by-this-user)`, which was
        wrong twice over: it wasn't mode-scoped (it wiped markouts
        attached to live positions too) and it conflicted with the
        "user can only clear simulation, never live" rule.
        """
        def _do_reset():
            engine = get_engine()
            with engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM pm_positions WHERE mode = 'simulation' "
                    "  AND user_id = :uid"
                ), {"uid": DEFAULT_USER_ID})
        try:
            await self._offload(_do_reset)
        except Exception as exc:
            return _err(f"reset failed: {exc}", 500)
        return _ok({"ok": True, "detail": "simulation positions reset"})

    # ── Auto-start at login (LaunchAgent supervision) ──────────────────────
    #
    # Backed by launchd on macOS. The LaunchAgent plist at
    # ~/Library/LaunchAgents/com.delfi.bot.plist (created by the
    # bundled install.sh) carries RunAtLoad=true + KeepAlive=true,
    # so when it's bootstrapped it auto-starts at user login and
    # auto-restarts on crash. Toggling this setting calls
    # `launchctl bootstrap` (turn on) or `launchctl bootout`
    # (turn off). bootout signals SIGTERM to the running daemon -
    # so toggling OFF stops the bot. bootstrap+kickstart starts
    # a fresh daemon immediately.
    #
    # Windows uses a HKCU\\Run registry value pointing at the installed
    # delfi.exe (the Tauri GUI). At user login Windows reads the Run key
    # and launches every value listed; our GUI starts, sees no existing
    # daemon via the port-file probe, and spawns the sidecar itself
    # (release-mode fallback - see src-tauri/src/main.rs).
    #
    # Linux: still unwired. systemd --user units would be the right path
    # but distros vary enough that we'd need per-distro logic.

    _WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _WIN_RUN_VALUE_NAME = "Delfi"

    def _autostart_paths(self) -> tuple[str, str]:
        """Return (plist path, launchctl service id) for the current user."""
        import os
        home = os.path.expanduser("~")
        plist = os.path.join(home, "Library", "LaunchAgents",
                             "com.delfi.bot.plist")
        uid = os.getuid()
        service_id = f"gui/{uid}/com.delfi.bot"
        return plist, service_id

    def _windows_gui_exe_path(self) -> Optional[str]:
        """Resolve the absolute path to the installed delfi.exe (Tauri GUI).

        In production (PyInstaller bundle inside the Tauri installer)
        the GUI binary lives in the same directory as the sidecar
        binary. `sys.executable` for a PyInstaller onefile is the
        sidecar's own path, so the GUI is its sibling.

        Returns None when the sidecar is running outside the installed
        bundle (dev mode `python main.py`, or some weird location), in
        which case Windows autostart should report unsupported.
        """
        import os
        import sys
        try:
            sidecar_dir = os.path.dirname(os.path.abspath(sys.executable))
        except Exception:
            return None
        gui_path = os.path.join(sidecar_dir, "delfi.exe")
        return gui_path if os.path.isfile(gui_path) else None

    def _autostart_status_windows(self) -> dict:
        """Probe the HKCU\\Run registry value for our autostart entry."""
        try:
            import winreg
        except ImportError:
            return {
                "supported": False,
                "enabled":   False,
                "reason":    "winreg is not available on this Python build.",
            }
        gui = self._windows_gui_exe_path()
        if gui is None:
            # Dev mode or broken install - the toggle would point at
            # a path that doesn't exist, so refuse to claim support.
            return {
                "supported": False,
                "enabled":   False,
                "reason":    "Autostart needs the installed Delfi bundle "
                             "(could not find delfi.exe).",
            }
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self._WIN_RUN_KEY,
                0,
                winreg.KEY_READ,
            ) as key:
                try:
                    value, _ = winreg.QueryValueEx(
                        key, self._WIN_RUN_VALUE_NAME,
                    )
                    return {
                        "supported": True,
                        "enabled":   bool(value),
                        "reason":    None,
                    }
                except FileNotFoundError:
                    return {
                        "supported": True,
                        "enabled":   False,
                        "reason":    None,
                    }
        except OSError as exc:
            return {
                "supported": True,
                "enabled":   False,
                "reason":    f"registry probe failed: {exc}",
            }

    def _put_autostart_windows(self, target: bool) -> tuple[bool, Optional[str]]:
        """Set or clear the HKCU\\Run value. Returns (ok, error)."""
        try:
            import winreg
        except ImportError:
            return False, "winreg is not available on this Python build."

        if target:
            gui = self._windows_gui_exe_path()
            if gui is None:
                return False, (
                    "could not find delfi.exe next to the sidecar - "
                    "autostart requires the installed Delfi bundle."
                )
            # Wrap in quotes so paths with spaces (Program Files) work
            # when Windows shells out to the value at login.
            value = f'"{gui}"'
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    self._WIN_RUN_KEY,
                    0,
                    winreg.KEY_SET_VALUE,
                ) as key:
                    winreg.SetValueEx(
                        key, self._WIN_RUN_VALUE_NAME, 0,
                        winreg.REG_SZ, value,
                    )
            except OSError as exc:
                return False, f"registry write failed: {exc}"
            return True, None

        # target = False: delete the value. Idempotent: missing value
        # means we're already in the target state.
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self._WIN_RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                try:
                    winreg.DeleteValue(key, self._WIN_RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        except OSError as exc:
            return False, f"registry delete failed: {exc}"
        return True, None

    async def _autostart_status(self) -> dict:
        """Probe the LaunchAgent state. Caller decides what to do with it."""
        import os
        import platform
        import subprocess

        sysname = platform.system()
        if sysname == "Windows":
            return self._autostart_status_windows()
        if sysname != "Darwin":
            return {
                "supported": False,
                "enabled":   False,
                "reason":    "Auto-start at login on this OS is not "
                             "implemented yet.",
            }

        plist, service_id = self._autostart_paths()
        if not os.path.isfile(plist):
            return {
                "supported": True,
                "enabled":   False,
                "reason":    ("LaunchAgent file not found. Run "
                              "Delfibot/install.sh to install it."),
            }
        # `launchctl print` exits 0 when the service is bootstrapped,
        # nonzero when it is not. We don't need stdout.
        try:
            r = subprocess.run(
                ["launchctl", "print", service_id],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "supported": True,
                "enabled":   False,
                "reason":    f"launchctl probe failed: {exc}",
            }
        return {
            "supported": True,
            "enabled":   r.returncode == 0,
            "reason":    None,
        }

    async def _get_autostart(self, _req: web.Request) -> web.Response:
        return _ok(await self._autostart_status())

    async def _put_autostart(self, req: web.Request) -> web.Response:
        import os
        import platform
        import subprocess

        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)
        if "enabled" not in payload or not isinstance(payload["enabled"], bool):
            return _err("body must include 'enabled' (boolean)", 400)
        target = bool(payload["enabled"])

        sysname = platform.system()
        if sysname == "Windows":
            ok, err = self._put_autostart_windows(target)
            if not ok:
                return _err(err or "autostart toggle failed", 500)
            # Registry writes are synchronous - no reconcile loop needed,
            # the next status read will reflect the change immediately.
            return _ok(self._autostart_status_windows())
        if sysname != "Darwin":
            return _err(
                "Auto-start at login on this OS is not implemented yet.",
                400,
            )

        plist, service_id = self._autostart_paths()
        if not os.path.isfile(plist):
            return _err(
                f"LaunchAgent plist not found at {plist}. Run "
                f"Delfibot/install.sh to install it.",
                400,
            )

        # bootout is idempotent: nonzero on "not loaded" is fine.
        # bootstrap fails with errno 17 if already loaded; treat
        # that as success.
        def _run(args: list[str]) -> tuple[int, str]:
            try:
                r = subprocess.run(args, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return -1, str(exc)
            err_text = (r.stderr or b"").decode("utf-8", "replace").strip()
            return r.returncode, err_text

        try:
            if target:
                # bootout first so we always start from a clean slate.
                # bootout is async-ish: launchd accepts the request and
                # signals SIGTERM, but the agent isn't fully torn down
                # until the daemon process exits and launchd reaps. If
                # we bootstrap before that completes, bootstrap returns
                # rc=5 ("Input/output error") or rc=37 ("Operation
                # already in progress"). Poll briefly to make sure the
                # previous registration is gone.
                _run(["launchctl", "bootout", service_id])
                for _ in range(20):  # up to ~2s
                    rc_check, _ = _run(["launchctl", "print", service_id])
                    if rc_check != 0:
                        break
                    await asyncio.sleep(0.1)
                rc, err = _run([
                    "launchctl", "bootstrap",
                    f"gui/{os.getuid()}", plist,
                ])
                # bootstrap return codes that mean "agent is loaded":
                #   0  = success
                #   17 = "File exists" (already loaded, older macOS)
                #   5  = "Input/output error" (already loaded or
                #         service is mid-launch)
                # We don't fail eagerly on these - reconcile against
                # actual status below.
                # kickstart -k forces an immediate (re)start so the
                # daemon is alive without waiting for the next login.
                _run(["launchctl", "kickstart", "-k", service_id])
            else:
                rc, err = _run(["launchctl", "bootout", service_id])
                # rc 113 = not currently loaded; rc 36 / 3 = service
                # not present. Treat all "already in target state" rcs
                # as success and reconcile from status below.
        except Exception as exc:
            return _err(f"autostart toggle failed: {exc}", 500)

        # Reconcile: poll the actual launchctl state for up to ~1s,
        # because bootstrap/bootout return before the daemon has
        # finished its lifecycle change. The earlier non-zero rc from
        # bootstrap is only an error if the END state doesn't match
        # what the user asked for.
        status = await self._autostart_status()
        for _ in range(10):
            if status["enabled"] == target:
                break
            await asyncio.sleep(0.1)
            status = await self._autostart_status()

        if status["enabled"] != target:
            return _err(
                f"autostart toggle did not converge to enabled={target}. "
                f"launchctl reported: rc={rc} {err!r}",
                500,
            )
        return _ok(status)

    # ── System ops: restart, logs, backup, launch stats, login item ─────────

    async def _post_restart(self, _req: web.Request) -> web.Response:
        """Restart the daemon by self-SIGTERMing the running process.

        We used to call `launchctl kickstart -k`, which kicks the pid
        launchd CURRENTLY THINKS is the daemon. When launchd loses
        track of the right pid (which happens after install.sh's
        bootout/rsync/bootstrap dance leaves the original daemon
        alive while a duplicate respawn wins launchd's tracked-pid
        slot), kickstart aims at a ghost and the real lock-holding
        daemon never receives SIGTERM. Restart hangs forever and
        the user sees a stuck spinner.

        os.kill(getpid(), SIGTERM) targets the actual running daemon
        unconditionally. Whether launchd's tracked pid is right is
        irrelevant. KeepAlive=true on the LaunchAgent respawns us
        within ThrottleInterval (10s); the respawn wins the
        singleton lock cleanly (we just freed it) and writes a fresh
        sidecar.port that the GUI's retry logic picks up.

        Probing launchd first confirms the service is bootstrapped
        (i.e. respawn WILL happen). If it isn't, we'd die and never
        come back, leaving a dead app.

        macOS-only for now. On Windows we'd need to wire a Windows
        Service equivalent.
        """
        import asyncio as _asyncio
        import os
        import platform
        import signal
        import subprocess

        sysname = platform.system()
        if sysname == "Windows":
            # Same trick as macOS: detach a child that pauses briefly,
            # then taskkills US. By the time taskkill fires we've
            # already returned this HTTP response. The Tauri shell's
            # respawn loop picks up the Terminated event and starts a
            # fresh sidecar.
            #
            # `start /B "" cmd /c "...timeout & taskkill..."` spawns a
            # detached cmd.exe that survives our death. The empty ""
            # is the window title (required positional arg of start).
            try:
                subprocess.Popen(
                    ["cmd", "/c",
                     'start /B "" cmd /c '
                     '"timeout /t 1 /nobreak > nul & '
                     'taskkill /F /T /IM delfi-sidecar.exe"'],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                return _err(f"failed to spawn restart: {exc}", 500)
            return _ok({"ok": True, "detail": "Restart signal sent. Delfi "
                        "will be back online in a few seconds."})

        if sysname != "Darwin":
            return _err("Restart is not implemented on this OS yet.", 400)

        _, service_id = self._autostart_paths()

        # Probe the LaunchAgent before scheduling the kill. If the
        # service isn't bootstrapped, no respawn will happen and the
        # daemon stays dead.
        try:
            probe = subprocess.run(
                ["launchctl", "print", service_id],
                capture_output=True, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _err(f"launchctl probe failed: {exc}", 500)
        if probe.returncode != 0:
            return _err(
                "Daemon isn't bootstrapped under launchd. Turn on "
                "'Start Delfi at login' in Settings, then try again.",
                400,
            )

        # Schedule self-SIGTERM AFTER returning the HTTP response. We
        # need enough runway to finish writing the response body back
        # to the client before our event loop tears down. 500 ms is
        # comfortable - the aiohttp handler returns in milliseconds.
        async def _self_terminate() -> None:
            await _asyncio.sleep(0.5)
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except OSError as exc:
                print(f"[restart] self-SIGTERM failed: {exc}", flush=True)

        _asyncio.create_task(_self_terminate())

        return _ok({"ok": True, "detail": "Restart signal sent. The daemon "
                    "will be back online in a few seconds."})

    async def _get_logs(self, req: web.Request) -> web.Response:
        """Tail the daemon's stdout / stderr log files.

        Query params:
          stream = stdout | stderr (default stdout)
          lines  = how many lines from the end to return (default 200,
                   capped at 2000 to avoid blowing memory on chatty logs)
        """
        import os

        stream = req.query.get("stream", "stdout")
        if stream not in ("stdout", "stderr"):
            return _err("stream must be 'stdout' or 'stderr'", 400)
        try:
            n = int(req.query.get("lines", "200"))
        except ValueError:
            return _err("lines must be an integer", 400)
        n = max(10, min(n, 2000))

        # Path mirrors the LaunchAgent plist's StandardOutPath /
        # StandardErrorPath (~/Library/Logs/Delfi/sidecar.{log,err}).
        # On Windows we don't redirect sidecar stdout to a file yet, but
        # the Tauri shell writes a small diagnostic log to %TEMP% that
        # captures startup-phase events (port resolution, respawns,
        # tray init). It's not a full sidecar log but it covers the
        # "Delfi won't start" support case.
        import platform
        sysname = platform.system()
        if sysname == "Windows":
            tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
            path = os.path.join(tmp, "delfi-shell.log") if tmp else ""
        else:
            home = os.path.expanduser("~")
            log_dir = os.path.join(home, "Library", "Logs", "Delfi")
            path = os.path.join(
                log_dir,
                "sidecar.log" if stream == "stdout" else "sidecar.err",
            )
        if not os.path.isfile(path):
            return _ok({
                "stream": stream,
                "path":   path,
                "lines":  [],
                "note":   "Log file not found yet. It populates once the "
                          "launchd daemon writes its first line.",
            })

        # Cheap "tail": read the whole file, slice. The log files
        # are appended-only and macOS launchd doesn't rotate them
        # by default, so they CAN grow indefinitely. To stay sane
        # we read at most the last 2 MiB.
        def _read_tail():
            sz = os.path.getsize(path)
            with open(path, "rb") as f:
                if sz > 2 * 1024 * 1024:
                    f.seek(sz - 2 * 1024 * 1024)
                return f.read()
        try:
            blob = await self._offload(_read_tail)
            text_blob = blob.decode("utf-8", "replace")
            tail = text_blob.splitlines()[-n:]
        except Exception as exc:
            return _err(f"could not read log: {exc}", 500)

        return _ok({
            "stream": stream,
            "path":   path,
            "lines":  tail,
            "note":   None,
        })

    async def _post_db_backup(self, req: web.Request) -> web.Response:
        """Copy the SQLite DB to a user-supplied path.

        Body: {"dest_path": "/path/to/backup.db"}

        Uses SQLite's VACUUM INTO so the snapshot is consistent even
        while the bot is mid-write. Returns the resulting file size
        so the UI can confirm "exported X MB".
        """
        import os

        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)
        dest = payload.get("dest_path")
        if not isinstance(dest, str) or not dest.strip():
            return _err("dest_path (string) is required", 400)
        dest = os.path.expanduser(dest.strip())

        # Refuse to overwrite the live DB - that would corrupt it.
        # Refuse to write into the AppData dir at all so backups
        # always land somewhere outside the app's volatile state.
        from db.engine import _default_db_path
        live = str(_default_db_path().resolve())
        try:
            dest_real = os.path.realpath(dest)
        except Exception:
            dest_real = dest
        if dest_real == live:
            return _err(
                "dest_path cannot be the live database file.", 400,
            )

        # Make sure parent exists.
        parent = os.path.dirname(dest)
        if parent and not os.path.isdir(parent):
            return _err(
                f"parent directory does not exist: {parent}", 400,
            )

        def _do_vacuum():
            engine = get_engine()
            with engine.begin() as conn:
                # VACUUM INTO requires a literal path string -
                # parameter binding doesn't work here. We've already
                # validated dest as a non-empty string and prevented
                # overwriting the live DB; quoting handles the rest.
                escaped = dest.replace("'", "''")
                conn.execute(text(f"VACUUM INTO '{escaped}'"))
        try:
            await self._offload(_do_vacuum)
        except Exception as exc:
            return _err(f"backup failed: {exc}", 500)

        try:
            size = os.path.getsize(dest)
        except OSError:
            size = 0
        return _ok({
            "ok":      True,
            "path":    dest,
            "size":    size,
            "detail":  f"Backup written to {dest} ({size:,} bytes)",
        })

    async def _get_launch_stats(self, _req: web.Request) -> web.Response:
        """Parse `launchctl print` to extract daemon supervision stats.

        Useful diagnostic when something is misbehaving:
          - `runs`: total respawn count since the agent was bootstrapped.
            High = something's crashing.
          - `last_exit_code`: nonzero = the last run died on an
            exception.
          - `pid`: current daemon PID, or null if not running.
        """
        import platform
        import re
        import subprocess

        if platform.system() != "Darwin":
            # On non-macOS, the sidecar is spawned + supervised by the
            # Tauri GUI (no launchd / Service). Report self-state so
            # the daemon-health pill shows "running" instead of
            # "unsupported": this endpoint only fires when the sidecar
            # is alive enough to answer HTTP, so by definition we ARE
            # running. runs / last_exit_code are not tracked because
            # the GUI-spawned model has no respawn-counter equivalent.
            return _ok({
                "supported": True,
                "runs":      1,
                "last_exit_code": None,
                "pid":       os.getpid(),
                "state":     "running",
            })

        _, service_id = self._autostart_paths()
        try:
            r = subprocess.run(
                ["launchctl", "print", service_id],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _ok({
                "supported": True,
                "runs":      None,
                "last_exit_code": None,
                "pid":       None,
                "state":     None,
            })

        if r.returncode != 0:
            return _ok({
                "supported": True,
                "runs":      0,
                "last_exit_code": None,
                "pid":       None,
                "state":     "unloaded",
            })
        out = (r.stdout or b"").decode("utf-8", "replace")

        def _grab(pattern: str) -> Optional[str]:
            # MULTILINE so ^ matches per line (launchctl print uses
            # tab-indented key = value lines, not start-of-string).
            m = re.search(pattern, out, re.MULTILINE)
            return m.group(1).strip() if m else None

        # `runs = N` and `last exit code = X` are top-level fields in
        # launchctl print's output. `state = running` plus `pid = N`.
        runs_str = _grab(r"^\s*runs\s*=\s*(\d+)")
        last_exit_str = _grab(r"^\s*last exit code\s*=\s*(-?\d+|UNKNOWN)")
        # Match pid (not pid-local-endpoints which is a sub-dict). The
        # `pid-local` line shares the prefix so be specific: pid =
        # followed by digits and end of line.
        pid_str = _grab(r"^\s*pid\s*=\s*(\d+)\s*$")
        state_str = _grab(r"^\s*state\s*=\s*(\S+)")

        runs = int(runs_str) if runs_str and runs_str.isdigit() else None
        last_exit = None
        if last_exit_str and last_exit_str.lstrip("-").isdigit():
            last_exit = int(last_exit_str)
        pid = int(pid_str) if pid_str and pid_str.isdigit() else None
        return _ok({
            "supported": True,
            "runs":      runs,
            "last_exit_code": last_exit,
            "pid":       pid,
            "state":     state_str,
        })

    # ── Login item: open the GUI window automatically at login ──────────────
    #
    # Separate from autostart-the-daemon. The daemon is supervised by
    # launchd and runs headlessly. This toggle controls whether the
    # Tauri shell window pops up at user login. Implemented via macOS
    # Login Items (the same list users see in System Settings >
    # General > Login Items).

    def _login_item_app_path(self) -> str:
        return "/Applications/Delfi.app"

    async def _login_item_status(self) -> dict:
        """Internal: return the login item status dict (no HTTP wrap)."""
        import platform
        import subprocess

        if platform.system() != "Darwin":
            return {
                "supported": False,
                "enabled":   False,
                "reason":    "Login item is currently macOS-only.",
            }
        applescript = (
            'tell application "System Events" to get the path of every '
            'login item'
        )
        # 30s timeout: the FIRST call to osascript tell-System-Events
        # triggers a macOS permission prompt that the user has to
        # actually click. 5s wasn't enough; the call timed out before
        # the user could finish reading "Delfi wants to control
        # System Events. Allow / Don't Allow." Subsequent calls (after
        # permission is granted or denied) return in milliseconds.
        try:
            r = subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {
                "supported": True,
                "enabled":   False,
                "reason":    ("Waiting for System Events permission. "
                              "Click Allow on the macOS prompt and "
                              "try again."),
            }
        except OSError as exc:
            return {
                "supported": True,
                "enabled":   False,
                "reason":    f"osascript probe failed: {exc}",
            }
        if r.returncode != 0:
            err_text = (r.stderr or b"").decode("utf-8", "replace").strip()
            # Specific message for the most common failure: user
            # clicked Don't Allow on the macOS prompt. The error text
            # includes "1743" or "Not authorized to send Apple events"
            # depending on the macOS version.
            low = err_text.lower()
            if "not authoris" in low or "not authoriz" in low or "1743" in low:
                err_text = ("System Events access denied. Open System "
                            "Settings > Privacy & Security > "
                            "Automation, find Delfi, and enable "
                            "System Events.")
            return {
                "supported": True,
                "enabled":   False,
                "reason":    err_text,
            }
        out = (r.stdout or b"").decode("utf-8", "replace")
        target = self._login_item_app_path()
        return {
            "supported": True,
            "enabled":   target in out,
            "reason":    None,
        }

    async def _get_login_item(self, _req: web.Request) -> web.Response:
        return _ok(await self._login_item_status())

    async def _put_login_item(self, req: web.Request) -> web.Response:
        import platform
        import subprocess

        try:
            payload = await req.json()
        except Exception:
            return _err("invalid json", 400)
        if not isinstance(payload, dict):
            return _err("body must be a JSON object", 400)
        if "enabled" not in payload or not isinstance(payload["enabled"], bool):
            return _err("body must include 'enabled' (boolean)", 400)
        target = bool(payload["enabled"])

        if platform.system() != "Darwin":
            return _err("Login item is currently macOS-only.", 400)

        path = self._login_item_app_path()
        if target:
            # Delete first so we don't end up with a duplicate entry
            # if the user toggles ON when ON.
            del_script = (
                f'tell application "System Events" to delete (every login '
                f'item whose path is "{path}")'
            )
            subprocess.run(
                ["osascript", "-e", del_script],
                capture_output=True, timeout=5,
            )
            add_script = (
                f'tell application "System Events" to make login item at '
                f'end with properties {{path:"{path}", hidden:false}}'
            )
            try:
                r = subprocess.run(
                    ["osascript", "-e", add_script],
                    capture_output=True, timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return _err(f"login item add failed: {exc}", 500)
        else:
            del_script = (
                f'tell application "System Events" to delete (every login '
                f'item whose path is "{path}")'
            )
            try:
                r = subprocess.run(
                    ["osascript", "-e", del_script],
                    capture_output=True, timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return _err(f"login item delete failed: {exc}", 500)

        status = await self._login_item_status()
        if status["enabled"] != target:
            err_text = (r.stderr or b"").decode("utf-8", "replace").strip()
            return _err(
                f"login item toggle did not converge: {err_text}",
                500,
            )
        return _ok(status)

    # ── Positions CSV export ────────────────────────────────────────────────
    async def _get_positions_csv(self, req: web.Request) -> web.Response:
        """Return a CSV of positions for the current user, current mode.

        Used by Performance > Export. We deliberately stream the
        whole table - typical user has hundreds to a few thousand
        rows, well under the streaming threshold.

        Mode-scoped per the 2026-05-16 rule: the export matches what
        the dashboard shows (current mode only). Users who want
        full cross-mode dumps can run a direct SQLite query against
        <app-data>/delfi.db.
        """
        import csv
        import io

        current_mode = (
            (await self._offload(get_user_config)).mode or "simulation"
        )

        def _read():
            engine = get_engine()
            with engine.begin() as conn:
                return conn.execute(text(
                    "SELECT id, created_at, prediction_id, market_id, "
                    "       slug, question, category, market_archetype, "
                    "       side, shares, entry_price, cost_usd, "
                    "       claude_probability, mode, status, "
                    "       expected_resolution_at, settled_at, "
                    "       settlement_outcome, settlement_price, "
                    "       realized_pnl_usd, venue "
                    "FROM pm_positions WHERE user_id = :uid "
                    "  AND mode = :m "
                    "ORDER BY created_at DESC"
                ), {"uid": DEFAULT_USER_ID, "m": current_mode}).mappings().all()
        try:
            rows = await self._offload(_read)
        except Exception as exc:
            return _err(f"export failed: {exc}", 500)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "created_at_utc", "prediction_id", "market_id",
            "slug", "question", "category", "archetype",
            "side", "shares", "entry_price", "cost_usd",
            "delfi_probability", "mode", "status",
            "expected_resolution_at_utc", "settled_at_utc",
            "settlement_outcome", "settlement_price",
            "realized_pnl_usd", "venue",
        ])
        for r in rows:
            writer.writerow([
                r["id"],
                iso_utc(r["created_at"]),
                r["prediction_id"],
                r["market_id"],
                r["slug"],
                r["question"],
                r["category"],
                r["market_archetype"],
                r["side"],
                r["shares"],
                r["entry_price"],
                r["cost_usd"],
                r["claude_probability"],
                r["mode"],
                r["status"],
                iso_utc(r["expected_resolution_at"]),
                iso_utc(r["settled_at"]),
                r["settlement_outcome"],
                r["settlement_price"],
                r["realized_pnl_usd"],
                r["venue"],
            ])

        csv_bytes = buf.getvalue().encode("utf-8")
        # Plain text/csv response with attachment so the browser /
        # Tauri webview triggers a save dialog.
        from datetime import datetime as _dt
        filename = f"delfi-trades-{_dt.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
        return web.Response(
            body=csv_bytes,
            headers={
                "Content-Type":        "text/csv; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Access-Control-Allow-Origin": "*",
            },
        )


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
