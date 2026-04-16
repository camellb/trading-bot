"""
Lightweight HTTP API for the dashboard.

Bound to 127.0.0.1 only. Every request must carry an X-Bot-Secret header
matching BOT_API_SECRET from the environment. If the env var is missing the
server refuses to start — the dashboard should never reach a bot that would
accept unauthenticated commands.

Phase 1:  /api/request-review
Phase 3:  /api/ask-claude, /api/preview-trade, /api/execute-trade,
          /api/close-position, /api/deep-research, /api/update-config

Safety model for Phase 3 endpoints:
  * execute-trade    → runs strategist._enforce_risk_limits (daily loss cap,
                       max positions, one-per-symbol, feed health, trading
                       halt) before place_order. Manual override bypasses
                       Claude's decision queue, NOT the risk kernel.
  * close-position   → delegates to position_monitor._close_position which
                       handles the full flat-confirm-before-DB flow.
  * update-config    → writes a pending change to _pending_config, asks the
                       user to confirm via Telegram /confirm-config. Nothing
                       is written to config.py until confirmation.
  * Claude endpoints → direct Anthropic calls, independent of the decision
                       queue.  PAPER_MODE is NOT in the allowed-keys list —
                       it stays a deliberate file edit + restart.
"""

import asyncio
import json
import os
import re
import sys
import importlib
from datetime import datetime, timezone

from aiohttp import web

import anthropic
from sqlalchemy import create_engine, text

import config
from db.logger import load_open_trades


BOT_API_HOST = "127.0.0.1"
BOT_API_PORT = 8765


# Keys the dashboard is allowed to edit at runtime. PAPER_MODE is deliberately
# omitted — switching paper/live must remain a conscious edit + restart.
ALLOWED_CONFIG_KEYS: dict[str, type] = {
    "DAILY_LOSS_CAP_USD":         float,
    "PORTFOLIO_MAX_TRADE_USD":    float,
    "PORTFOLIO_MIN_TRADE_USD":    float,
    "MAX_SIMULTANEOUS_POSITIONS": int,
    "OKX_LEVERAGE":               int,
}


class BotAPI:
    def __init__(
        self,
        scanner,
        order_manager,
        position_monitor,
        notifier,
        strategist=None,
        ws_manager=None,
    ):
        self._scanner          = scanner
        self._order_manager    = order_manager
        self._position_monitor = position_monitor
        self._notifier         = notifier
        self._strategist       = strategist
        self._ws_manager       = ws_manager
        self._secret           = os.environ.get("BOT_API_SECRET") or ""
        self._runner: web.AppRunner | None = None
        self._started_at: datetime | None  = None

        # Pending runtime config change — populated by /api/update-config,
        # applied by /confirm-config, discarded by /reject-config.
        self._pending_config: dict | None = None

        # Anthropic client for ad-hoc Claude calls (ask, preview, research).
        # Lazily falls back to None if no API key is configured so the
        # endpoints can return a clean 503 instead of crashing.
        try:
            self._claude = anthropic.Anthropic()
        except Exception as exc:
            print(f"[bot_api] Anthropic client init failed: {exc}", file=sys.stderr)
            self._claude = None

    # ── Auth ──────────────────────────────────────────────────────────────────
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        provided = request.headers.get("X-Bot-Secret", "")
        if not self._secret or provided != self._secret:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    # ── Handlers: lifecycle / read ────────────────────────────────────────────
    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status":     "ok",
            "paper_mode": config.PAPER_MODE,
            "started_at": self._started_at.isoformat() if self._started_at else None,
        })

    async def _handle_request_review(self, _request: web.Request) -> web.Response:
        if self._scanner is None:
            return web.json_response({"error": "scanner not available"}, status=503)
        monitor = getattr(self._scanner, "_monitor", None)
        if monitor is not None and not monitor.are_core_feeds_healthy():
            return web.json_response(
                {"error": "core feeds degraded — briefing skipped"}, status=503
            )
        detail = "Manual review requested from dashboard"
        async def _trigger(pair: str) -> None:
            try:
                await self._scanner._compile_and_send_briefing(
                    pair, "dashboard_request", detail
                )
            except Exception as exc:
                print(f"[bot_api] request_review {pair} failed: {exc}",
                      file=sys.stderr, flush=True)
        for pair in config.TRADING_PAIRS:
            asyncio.create_task(_trigger(pair))
        return web.json_response({
            "status":       "triggered",
            "pairs":        list(config.TRADING_PAIRS),
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        })

    # ── Handlers: Claude ──────────────────────────────────────────────────────
    async def _handle_ask_claude(self, request: web.Request) -> web.Response:
        if self._claude is None:
            return web.json_response({"error": "Claude client not configured"}, status=503)
        data = await request.json()
        pair     = str(data.get("pair") or "").strip()
        question = str(data.get("question") or "").strip()
        if not pair or not question:
            return web.json_response({"error": "pair and question required"}, status=400)
        if len(question) > 1000:
            return web.json_response({"error": "question too long (1000 char max)"}, status=400)
        briefing = await self._build_pair_snapshot(pair)
        system = (
            "You are a crypto trading analyst. Answer the question based on "
            "the current market data provided. Be direct and specific. "
            "Use actual numbers from the data. Under 200 words."
        )
        user = (
            f"Current market context for {pair}:\n{briefing}\n\n"
            f"Question: {question}"
        )
        try:
            answer = await self._call_claude(system, user, max_tokens=800)
            return web.json_response({"answer": answer, "pair": pair})
        except Exception as exc:
            return web.json_response({"error": f"Claude API error: {exc}"}, status=502)

    async def _handle_preview_trade(self, request: web.Request) -> web.Response:
        if self._claude is None:
            return web.json_response({"error": "Claude client not configured"}, status=503)
        data = await request.json()
        try:
            pair      = str(data["pair"])
            direction = str(data["direction"]).upper()
            size      = float(data["size_usd"])
            stop      = float(data["stop_loss"])
            tp        = float(data["take_profit"])
        except (KeyError, TypeError, ValueError) as exc:
            return web.json_response({"error": f"invalid payload: {exc}"}, status=400)
        if direction not in {"LONG", "SHORT"}:
            return web.json_response({"error": "direction must be LONG or SHORT"}, status=400)
        briefing = await self._build_pair_snapshot(pair)
        system = (
            "You are a crypto trading analyst assessing a manually-proposed trade. "
            "Reply with STRICT JSON only (no markdown fence) with keys: "
            'assessment ("good"|"risky"|"bad"), confidence (0..1), '
            "suggested_stop (number), suggested_tp (number), "
            "reasoning (string), warnings (list of strings). "
            "Use actual market numbers. Keep reasoning under 120 words."
        )
        user = (
            f"Current market context for {pair}:\n{briefing}\n\n"
            f"Proposed trade: {direction} {pair} size=${size} "
            f"stop=${stop} take_profit=${tp}.\n\n"
            "Assess this setup. Is the entry logical given current price action? "
            "Are the stop and take-profit levels sensible in ATR terms? "
            "What are the top risks? Reply with the JSON object only."
        )
        try:
            raw = await self._call_claude(system, user, max_tokens=800)
            parsed = _parse_json_object(raw)
            if parsed is None:
                return web.json_response({
                    "assessment": "risky",
                    "confidence": 0.4,
                    "suggested_stop": stop,
                    "suggested_tp":   tp,
                    "reasoning": raw[:500],
                    "warnings": ["Claude did not return valid JSON; showing raw text."],
                })
            return web.json_response(parsed)
        except Exception as exc:
            return web.json_response({"error": f"Claude API error: {exc}"}, status=502)

    async def _handle_deep_research(self, request: web.Request) -> web.Response:
        if self._claude is None:
            return web.json_response({"error": "Claude client not configured"}, status=503)
        data = await request.json()
        question = str(data.get("question") or "").strip()
        if not question:
            return web.json_response({"error": "question required"}, status=400)
        if len(question) > 2000:
            return web.json_response({"error": "question too long (2000 char max)"}, status=400)
        # Aggregate snapshot across all pairs for cross-asset analysis.
        snapshots = []
        for pair in config.TRADING_PAIRS:
            snap = await self._build_pair_snapshot(pair)
            snapshots.append(f"── {pair} ──\n{snap}")
        macro = await asyncio.get_running_loop().run_in_executor(
            None, self._latest_macro_context
        )
        system = (
            "You are a crypto market analyst with access to real-time market data "
            "across multiple pairs plus the macro context. Provide deep, specific, "
            "actionable analysis. Reference actual numbers. Under 600 words. "
            "Format the answer as plain prose — no markdown headers."
        )
        user = (
            f"Market snapshots:\n\n" + "\n\n".join(snapshots)
            + f"\n\n── Macro context ──\n{macro}\n\n"
            f"Question: {question}"
        )
        started = datetime.now(timezone.utc)
        try:
            text_out = await self._call_claude(system, user, max_tokens=2000)
            return web.json_response({
                "analysis":  text_out,
                "started_at": started.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            return web.json_response({"error": f"Claude API error: {exc}"}, status=502)

    # ── Handlers: execute / close ─────────────────────────────────────────────
    async def _handle_execute_trade(self, request: web.Request) -> web.Response:
        if self._order_manager is None or self._strategist is None:
            return web.json_response({"error": "trading stack not available"}, status=503)
        data = await request.json()
        try:
            pair      = str(data["pair"])
            direction = str(data["direction"]).upper()
            size      = float(data["size_usd"])
            stop      = float(data["stop_loss"])
            tp        = float(data["take_profit"])
        except (KeyError, TypeError, ValueError) as exc:
            return web.json_response({"error": f"invalid payload: {exc}"}, status=400)

        if direction not in {"LONG", "SHORT"}:
            return web.json_response({"status": "blocked", "reason": "invalid direction"})
        if pair not in config.TRADING_PAIRS:
            return web.json_response({"status": "blocked", "reason": f"pair {pair} not in TRADING_PAIRS"})

        # Gate 1 — trading halted
        if getattr(self._order_manager, "_trading_halted", False):
            return web.json_response({"status": "blocked", "reason": "trading halted (run /resume)"})

        # Gate 2 — feed health
        monitor = getattr(self._order_manager, "_monitor", None) \
                  or getattr(self._scanner, "_monitor", None)
        if monitor is not None and not monitor.are_core_feeds_healthy():
            return web.json_response({"status": "blocked", "reason": "core feeds degraded"})

        # Gate 3 — resolve entry price from live ticker (never trust the client)
        entry = self._get_mark_price(pair)
        if entry is None or entry <= 0:
            return web.json_response({"status": "blocked", "reason": "no live price for pair"})

        # Gate 4 — validate stop/TP directionality
        if direction == "LONG":
            if not (stop < entry < tp):
                return web.json_response({"status": "blocked",
                    "reason": f"LONG requires stop ({stop}) < entry ({entry:.2f}) < tp ({tp})"})
        else:
            if not (tp < entry < stop):
                return web.json_response({"status": "blocked",
                    "reason": f"SHORT requires tp ({tp}) < entry ({entry:.2f}) < stop ({stop})"})

        # Gate 5 — risk kernel (reuses strategist logic: daily loss cap,
        # max positions, one-per-symbol, min size; may cap size downwards).
        loop = asyncio.get_running_loop()
        fresh_trades = await loop.run_in_executor(
            None, lambda: load_open_trades(config.PAPER_MODE)
        )
        portfolio_value = await loop.run_in_executor(
            None, lambda: self._order_manager.get_portfolio_value()
        )
        action = {"pair": pair, "size_usd": size}
        allowed, reason = await self._strategist._enforce_risk_limits(
            action, fresh_trades, portfolio_value,
        )
        if not allowed:
            return web.json_response({"status": "blocked", "reason": reason})
        size = float(action["size_usd"])  # may have been capped

        # Execute via existing order path — this is the same code the scanner
        # pipeline uses, just without going through the decision queue.
        cycle_result = {
            "pair":           pair,
            "signal":         direction,
            "entry_price":    entry,
            "order_size_usd": size,
            "stop_loss":      stop,
            "take_profit":    tp,
            "regime":         "MANUAL_OVERRIDE",
        }
        try:
            fill = await loop.run_in_executor(
                None, lambda: self._order_manager.place_order(cycle_result),
            )
        except Exception as exc:
            return web.json_response({"status": "failed", "reason": str(exc)}, status=500)

        status = fill.get("status", "unknown")
        return web.json_response({
            "status":          status,
            "trade_id":        fill.get("trade_id"),
            "filled_price":    fill.get("filled_price"),
            "filled_size_usd": fill.get("filled_size_usd"),
            "paper":           fill.get("paper", config.PAPER_MODE),
            "error":           fill.get("error"),
        })

    async def _handle_close_position(self, request: web.Request) -> web.Response:
        if self._position_monitor is None:
            return web.json_response({"error": "position monitor not available"}, status=503)
        data = await request.json()
        try:
            trade_id = int(data["trade_id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "trade_id required (int)"}, status=400)
        positions = self._position_monitor._positions
        if trade_id not in positions:
            return web.json_response({"error": f"trade_id {trade_id} not tracked"}, status=404)
        pair = positions[trade_id].get("pair")
        exit_price = self._get_mark_price(pair) or positions[trade_id].get("entry_price", 0.0)
        result = await self._position_monitor._close_position(
            trade_id, float(exit_price), "manual_dashboard_close",
        )
        return web.json_response(result)

    # ── Handler: update-config (two-phase with Telegram confirmation) ─────────
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
        self._pending_config = {"key": key, "value": value, "previous": current}

        if self._notifier is not None:
            try:
                await self._notifier.send(
                    f"⚙️ <b>Dashboard config change requested</b>\n"
                    f"<code>{key}</code>: {current} → {value}\n"
                    f"Reply /confirm-config to apply, /reject-config to cancel."
                )
            except Exception as exc:
                print(f"[bot_api] Telegram notify failed: {exc}", file=sys.stderr)

        return web.json_response({
            "status": "pending",
            "key": key,
            "previous": current,
            "value": value,
            "message": "Telegram confirmation sent. Awaiting /confirm-config or /reject-config.",
        })

    # Called by telegram_notifier when the operator replies /confirm-config.
    def apply_pending_config(self) -> dict:
        if not self._pending_config:
            return {"status": "none", "reason": "no pending config change"}
        pc = self._pending_config
        key, value, previous = pc["key"], pc["value"], pc["previous"]
        try:
            # 1. Rewrite config.py so the change survives restart.
            _persist_config_value(key, value)
            # 2. Reload module so live readers see the new value.
            importlib.reload(config)
            # 3. Log the change (reuses the same audit table as /apply).
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

    # ── Helpers ───────────────────────────────────────────────────────────────
    async def _call_claude(self, system: str, user: str, max_tokens: int = 800) -> str:
        loop = asyncio.get_running_loop()
        assert self._claude is not None
        response = await loop.run_in_executor(
            None,
            lambda: self._claude.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            ),
        )
        return response.content[0].text

    def _get_mark_price(self, pair: str) -> float | None:
        """Best-effort current price from the WS manager, falling back to None."""
        if self._ws_manager is None:
            return None
        try:
            ticker = self._ws_manager.get_latest_ticker(pair) or {}
            for k in ("mark_price", "last", "index_price", "bid"):
                v = ticker.get(k)
                if v is not None and float(v) > 0:
                    return float(v)
        except Exception:
            pass
        return None

    async def _build_pair_snapshot(self, pair: str) -> str:
        """Compact text briefing per pair, fed to Claude endpoints."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._pair_snapshot_sync, pair)

    def _pair_snapshot_sync(self, pair: str) -> str:
        price = self._get_mark_price(pair)
        latest_tick = _query_one(
            """
            SELECT regime, adx, realized_vol_pct, funding_pct, iv,
                   decision, decision_reason, timestamp
            FROM ticks
            WHERE pair = %s
            ORDER BY timestamp DESC LIMIT 1
            """,
            (pair,),
        )
        open_trades = _query_all(
            """
            SELECT id, direction, entry_price, size_usd, stop_loss, take_profit,
                   thesis, timestamp_open
            FROM trades
            WHERE pair = %s AND timestamp_close IS NULL
            ORDER BY timestamp_open DESC
            """,
            (pair,),
        )
        lines = [
            f"Pair:          {pair}",
            f"Mark price:    {price if price else 'n/a'}",
            f"Paper mode:    {config.PAPER_MODE}",
        ]
        if latest_tick:
            lines.append(
                f"Regime:        {latest_tick['regime']}  "
                f"(ADX {latest_tick['adx']}, vol% {latest_tick['realized_vol_pct']}, "
                f"funding {latest_tick['funding_pct']}, IV {latest_tick['iv']})"
            )
            lines.append(
                f"Last decision: {latest_tick['decision']} — "
                f"{(latest_tick['decision_reason'] or '')[:300]}"
            )
        if open_trades:
            lines.append("Open positions on this pair:")
            for t in open_trades:
                lines.append(
                    f"  id={t['id']} {t['direction']} @{t['entry_price']} "
                    f"size=${t['size_usd']} SL={t['stop_loss']} TP={t['take_profit']}"
                )
        else:
            lines.append("Open positions: none for this pair")
        return "\n".join(lines)

    def _latest_macro_context(self) -> str:
        row = _query_one(
            """
            SELECT sentiment, confidence, risk_multiplier, reasoning, watch_for,
                   generated_at
            FROM macro_context_log
            ORDER BY generated_at DESC LIMIT 1
            """
        )
        if not row:
            return "No macro context available."
        return (
            f"Sentiment: {row['sentiment']} (confidence {row['confidence']}), "
            f"risk multiplier {row['risk_multiplier']}\n"
            f"Reasoning: {(row['reasoning'] or '')[:400]}\n"
            f"Watch for: {(row['watch_for'] or '')[:200]}"
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self._secret:
            raise RuntimeError(
                "BOT_API_SECRET is not set. Add it to .env (32+ random chars) "
                "and matching dashboard/.env.local before starting the bot."
            )
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get ("/api/health",          self._handle_health)
        app.router.add_post("/api/request-review",  self._handle_request_review)
        app.router.add_post("/api/ask-claude",      self._handle_ask_claude)
        app.router.add_post("/api/preview-trade",   self._handle_preview_trade)
        app.router.add_post("/api/execute-trade",   self._handle_execute_trade)
        app.router.add_post("/api/close-position",  self._handle_close_position)
        app.router.add_post("/api/deep-research",   self._handle_deep_research)
        app.router.add_post("/api/update-config",   self._handle_update_config)
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


# ── Module-level helpers ─────────────────────────────────────────────────────
def _parse_json_object(text: str) -> dict | None:
    """Strip optional markdown fences, return dict or None."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1 :]
        if t.endswith("```"):
            t = t[: -3]
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _query_one(sql: str, params: tuple | None = None) -> dict | None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        engine = create_engine(url)
        with engine.begin() as conn:
            rows = list(conn.execute(text(_to_named(sql)), dict(_params(sql, params))))
            if not rows:
                return None
            return dict(rows[0]._mapping)
    except Exception as exc:
        print(f"[bot_api] _query_one failed: {exc}", file=sys.stderr)
        return None


def _query_all(sql: str, params: tuple | None = None) -> list[dict]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return []
    try:
        engine = create_engine(url)
        with engine.begin() as conn:
            rows = list(conn.execute(text(_to_named(sql)), dict(_params(sql, params))))
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        print(f"[bot_api] _query_all failed: {exc}", file=sys.stderr)
        return []


def _to_named(sql: str) -> str:
    # Convert %s placeholders to SQLAlchemy :pN named params.
    i = 0
    out = []
    parts = sql.split("%s")
    for idx, p in enumerate(parts):
        out.append(p)
        if idx < len(parts) - 1:
            out.append(f":p{i}")
            i += 1
    return "".join(out)


def _params(sql: str, params: tuple | None) -> dict:
    if not params:
        return {}
    return {f"p{i}": v for i, v in enumerate(params)}


def _persist_config_value(key: str, value) -> None:
    """Rewrite the first `KEY = …` line in config.py with the new value.

    Narrow regex: only the assignment line is touched. If the key isn't found
    the call raises — caller surfaces the error to the operator.
    """
    cfg_path = os.path.join(os.path.dirname(__file__), "config.py")
    with open(cfg_path, "r", encoding="utf-8") as f:
        src = f.read()
    pattern = re.compile(rf"(?m)^({re.escape(key)}\s*=\s*)[^\n#]*(\s*(?:#.*)?)$")
    new_literal = repr(value) if isinstance(value, str) else str(value)
    new_src, n = pattern.subn(rf"\g<1>{new_literal}\g<2>", src, count=1)
    if n != 1:
        raise RuntimeError(f"config.py has no top-level assignment for {key}")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(new_src)


def _audit_config_change(key: str, old, new, source: str) -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return
    try:
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO config_change_history "
                "(param_name, old_value, new_value, reason, suggested_by, outcome) "
                "VALUES (:k, :o, :n, :r, :s, 'pending')"
            ), {
                "k": key, "o": str(old), "n": str(new),
                "r": "dashboard /api/update-config", "s": source,
            })
    except Exception as exc:
        print(f"[bot_api] audit log write failed: {exc}", file=sys.stderr)
