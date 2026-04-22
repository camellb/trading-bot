"""
Telegram Notifier — multi-tenant.

Every user brings their own Telegram bot (via @BotFather) and their own
chat_id. Both are stored on `user_config` (columns `telegram_bot_token`
and `telegram_chat_id`). If either is missing for a given user, every
notifier call for that user silently no-ops — Telegram delivery is
opt-in.

Credentials are looked up per-user with a 60-second in-memory TTL cache
so high-frequency paths (scan notifications, settlements) don't hammer
the DB.

Every user-facing string lives in `feeds.telegram_messages`. This module
only handles delivery, sanitisation, and command routing. Locked user
commands (per Delfi Messages Spec v1): /status, /pause, /resume, /apply,
/reject, /help.

Operator-facing output (feed health warnings, wiring errors, tracebacks)
is logged to stderr only — it is not sent to any user channel.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Tuple

import aiohttp
from sqlalchemy import text

import config
from db.engine import get_engine
from engine.notifier_state import (
    mark_first_loss_if_unsent,
    mark_first_win_if_unsent,
    set_trading_paused,
)
from engine.user_config import (
    DEFAULT_USER_ID,
    get_user_telegram_creds,
    list_users_with_telegram,
)
from feeds import telegram_messages as tm


_TELEGRAM_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# TTL for the in-memory creds cache. Short so dashboard edits propagate
# quickly without restarting the bot, long enough to absorb bursty sends.
_CREDS_TTL_SEC = 60.0


# Telegram's HTML parse_mode rejects the whole message on any stray "<",
# ">", or un-escaped "&", or on any tag it does not recognize. Dynamic text
# (market questions, Claude reasoning, error details) routinely contains
# these. We sanitize before sending: keep allowed tags as-is, escape the
# rest. Already-escaped entities (&lt; &amp; &#39; …) pass through untouched.
_TG_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "tg-spoiler", "tg-emoji", "blockquote",
}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9\-]*)(\s[^>]*)?>")
_RAW_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def _escape_text(s: str) -> str:
    s = _RAW_AMP.sub("&amp;", s)
    return s.replace("<", "&lt;").replace(">", "&gt;")


def _sanitize_html(msg: str) -> str:
    """Escape bare <, >, & while preserving the Telegram-allowed HTML tags."""
    if not msg:
        return msg
    out: list[str] = []
    last = 0
    for m in _TAG_RE.finditer(msg):
        tag = m.group(2).lower()
        out.append(_escape_text(msg[last:m.start()]))
        if tag in _TG_ALLOWED_TAGS:
            out.append(m.group(0))
        else:
            out.append(_escape_text(m.group(0)))
        last = m.end()
    out.append(_escape_text(msg[last:]))
    return "".join(out)


class TelegramNotifier:
    """Per-user Telegram delivery. Call every send_* / notify_* with user_id."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

        # Cache: user_id -> (token, chat_id, fetched_at_epoch). A value of
        # (None, None, fetched_at) indicates "user has no creds" and is
        # cached too so we don't re-hit the DB on every scan outcome.
        self._creds_cache: dict[str, Tuple[Optional[str], Optional[str], float]] = {}
        self._creds_lock = threading.Lock()

        # Wired in by main.py after construction.
        self._loop:            Optional[asyncio.AbstractEventLoop] = None
        self._bot_start_time:  Optional[datetime] = None
        self._monitor                              = None   # FeedHealthMonitor
        self._executor                             = None   # PMExecutor
        self._analyst                              = None   # PMAnalyst
        self._bot_api                              = None   # BotAPI

    # ── Creds cache ──────────────────────────────────────────────────────────
    def _get_creds(self, user_id: str) -> Optional[Tuple[str, str]]:
        now = time.monotonic()
        with self._creds_lock:
            entry = self._creds_cache.get(user_id)
            if entry is not None:
                tok, cid, at = entry
                if now - at < _CREDS_TTL_SEC:
                    if tok and cid:
                        return tok, cid
                    return None
        creds = get_user_telegram_creds(user_id)
        with self._creds_lock:
            if creds is None:
                self._creds_cache[user_id] = (None, None, now)
                return None
            self._creds_cache[user_id] = (creds[0], creds[1], now)
            return creds

    def invalidate_creds(self, user_id: str) -> None:
        """Drop the cached entry for a user — call after a dashboard update."""
        with self._creds_lock:
            self._creds_cache.pop(user_id, None)

    # ── Core send ────────────────────────────────────────────────────────────
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def send(self, user_id: str, message: str) -> None:
        creds = self._get_creds(user_id)
        if creds is None:
            return
        token, chat_id = creds
        url = _TELEGRAM_BASE.format(token=token)
        safe = _sanitize_html(message)
        payload = {"chat_id": chat_id, "text": safe, "parse_mode": "HTML"}
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[telegram] send failed {resp.status}: {body[:200]}",
                          file=sys.stderr)
        except Exception as exc:
            print(f"[telegram] send exception: {exc}", file=sys.stderr)

    def send_sync(self, user_id: str, message: str) -> None:
        """Blocking send for use outside the event loop (crash handler, shutdown)."""
        creds = self._get_creds(user_id)
        if creds is None:
            return
        token, chat_id = creds
        url = _TELEGRAM_BASE.format(token=token)
        safe = _sanitize_html(message)
        payload = json.dumps(
            {"chat_id": chat_id, "text": safe, "parse_mode": "HTML"}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as exc:
            print(f"[telegram] sync send failed: {exc}", file=sys.stderr)

    async def _broadcast(self, message: str) -> None:
        """Send to every user with configured creds. For startup/shutdown."""
        for uid in list_users_with_telegram():
            await self.send(uid, message)

    def _broadcast_sync(self, message: str) -> None:
        for uid in list_users_with_telegram():
            self.send_sync(uid, message)

    # ── Feed health (admin log only) ─────────────────────────────────────────
    async def notify_feed_degraded(self, feed_name: str, detail: str) -> None:
        # Spec v1: feed health is operator-only. Stderr, not user channel.
        print(f"[telegram][admin] feed degraded: {feed_name} — {detail}",
              file=sys.stderr)

    async def notify_feed_recovered(self, feed_name: str) -> None:
        print(f"[telegram][admin] feed recovered: {feed_name}", file=sys.stderr)

    # ── Settlement ──────────────────────────────────────────────────────────
    async def notify_settlement(self, user_id: str, position_id: int,
                                 question: str, side: str, outcome: str,
                                 pnl: float, cost: float) -> None:
        if self._get_creds(user_id) is None:
            return
        # Spec v1 drops the INVALID settlement variant from the user channel.
        if outcome == "INVALID":
            print(
                f"[telegram][admin] invalid settlement pos={position_id} "
                f"question={question[:80]}",
                file=sys.stderr,
            )
            return

        won = (outcome == side)
        roi = (pnl / cost * 100) if cost > 0 else 0.0
        bankroll = 0.0
        if self._executor:
            try:
                bankroll = float(self._executor.get_bankroll())
            except Exception:
                pass

        if won:
            if mark_first_win_if_unsent():
                msg = tm.first_win(
                    question=question, pnl=pnl, roi=roi, bankroll=bankroll,
                )
            else:
                msg = tm.settled_win(
                    question=question, side=side, outcome=outcome,
                    pnl=pnl, roi=roi, bankroll=bankroll,
                )
        else:
            if mark_first_loss_if_unsent():
                msg = tm.first_loss(
                    question=question, pnl=pnl, roi=roi, bankroll=bankroll,
                )
            else:
                msg = tm.settled_loss(
                    question=question, side=side, outcome=outcome,
                    pnl=pnl, roi=roi, bankroll=bankroll,
                )
        await self.send(user_id, msg)

    # ── Error alerts ────────────────────────────────────────────────────────
    async def notify_error(self, user_id: str, context: str, detail: str) -> None:
        await self.send(user_id, tm.generic_error(context=context, detail=detail))

    async def broadcast_error(self, context: str, detail: str) -> None:
        """Operator-style alert fanned out to every configured user."""
        await self._broadcast(tm.generic_error(context=context, detail=detail))

    # ── Startup ──────────────────────────────────────────────────────────────
    async def notify_startup(self, user_id: str) -> None:
        if self._get_creds(user_id) is None:
            return
        try:
            stats = self._executor.get_portfolio_stats() if self._executor else {}
            simulated = stats.get("mode", "simulation") != "live"
            balance = float(stats.get("bankroll", 0.0))
            open_n = int(stats.get("open_positions", 0))
            at_risk = float(stats.get("open_cost", 0.0))
            resolved = int(stats.get("settled_total", 0))
            wins = int(stats.get("settled_wins", 0))
            win_pct = (wins / resolved * 100.0) if resolved else 0.0
            await self.send(user_id, tm.startup_full(
                balance=balance, open_n=open_n, at_risk=at_risk,
                win_pct=win_pct, resolved=resolved, simulated=simulated,
            ))
        except Exception as exc:
            print(f"[telegram] notify_startup failed: {exc}", file=sys.stderr)
            await self.send(user_id, tm.startup_fallback())

    async def broadcast_startup(self) -> None:
        for uid in list_users_with_telegram():
            await self.notify_startup(uid)

    # ── Daily / weekly summaries ────────────────────────────────────────────
    async def send_daily_summary(self) -> None:
        if self._executor is None:
            return
        for uid in list_users_with_telegram():
            await self._send_daily_summary_for(uid)

    async def _send_daily_summary_for(self, user_id: str) -> None:
        if self._get_creds(user_id) is None:
            return
        try:
            stats = self._executor.get_portfolio_stats()
            mode  = stats.get("mode", "simulation")

            def _daily_db():
                with get_engine().begin() as conn:
                    row = conn.execute(text(
                        "SELECT "
                        "  COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 day'), "
                        "  COUNT(*) FILTER (WHERE resolved_at >= NOW() - INTERVAL '1 day') "
                        "FROM predictions WHERE source = 'polymarket'"
                    )).fetchone()
                    pnl_settle = conn.execute(text(
                        "SELECT "
                        "  COALESCE(SUM(realized_pnl_usd), 0), "
                        "  COUNT(*) FILTER (WHERE status = 'settled' AND realized_pnl_usd > 0), "
                        "  COUNT(*) FILTER (WHERE status = 'settled' AND realized_pnl_usd <= 0) "
                        "FROM pm_positions "
                        "WHERE mode = :m AND settled_at >= NOW() - INTERVAL '1 day'"
                    ), {"m": mode}).fetchone()
                return (
                    int(row[0]), int(row[1]),
                    float(pnl_settle[0] or 0.0),
                    int(pnl_settle[1] or 0), int(pnl_settle[2] or 0),
                )

            loop = asyncio.get_running_loop()
            cnt24, resolved24, pnl24, wins24, losses24 = await loop.run_in_executor(
                None, _daily_db
            )
            settled_total = int(stats.get("settled_total", 0))
            settled_wins = int(stats.get("settled_wins", 0))
            win_pct = (settled_wins / settled_total * 100.0) if settled_total else 0.0

            await self.send(user_id, tm.daily_summary(
                bankroll=float(stats.get("bankroll", 0.0)),
                pnl24=pnl24,
                resolved24=resolved24,
                wins24=wins24,
                losses24=losses24,
                win_pct=win_pct,
                open_positions=int(stats.get("open_positions", 0)),
                open_cost=float(stats.get("open_cost", 0.0)),
                cnt24=cnt24,
            ))
        except Exception as exc:
            print(f"[telegram] _send_daily_summary_for({user_id}) failed: {exc}",
                  file=sys.stderr)

    async def send_weekly_summary(self) -> None:
        if self._executor is None:
            return
        for uid in list_users_with_telegram():
            await self._send_weekly_summary_for(uid)

    async def _send_weekly_summary_for(self, user_id: str) -> None:
        if self._get_creds(user_id) is None:
            return
        try:
            stats = self._executor.get_portfolio_stats()
            mode  = stats.get("mode", "simulation")

            def _weekly_db():
                with get_engine().begin() as conn:
                    row = conn.execute(text(
                        "SELECT "
                        "  COALESCE(SUM(realized_pnl_usd), 0), "
                        "  COUNT(*) FILTER (WHERE status = 'settled'), "
                        "  COUNT(*) FILTER (WHERE status = 'settled' AND realized_pnl_usd > 0) "
                        "FROM pm_positions "
                        "WHERE mode = :m AND settled_at >= NOW() - INTERVAL '7 days'"
                    ), {"m": mode}).fetchone()
                return float(row[0] or 0.0), int(row[1] or 0), int(row[2] or 0)

            loop = asyncio.get_running_loop()
            pnl7, total7, wins7 = await loop.run_in_executor(None, _weekly_db)
            losses7 = max(0, total7 - wins7)
            win_pct7 = (wins7 / total7 * 100.0) if total7 else 0.0

            settled_total = int(stats.get("settled_total", 0))
            settled_wins = int(stats.get("settled_wins", 0))
            win_pct_all = (settled_wins / settled_total * 100.0) if settled_total else 0.0

            await self.send(user_id, tm.weekly_summary(
                bankroll=float(stats.get("bankroll", 0.0)),
                pnl7=pnl7,
                wins7=wins7,
                losses7=losses7,
                win_pct7=win_pct7,
                win_pct_all=win_pct_all,
                pnl_all=float(stats.get("realized_pnl", 0.0)),
                settled_total=settled_total,
            ))
        except Exception as exc:
            print(f"[telegram] _send_weekly_summary_for({user_id}) failed: {exc}",
                  file=sys.stderr)

    # ── Shutdown / crash broadcasts ─────────────────────────────────────────
    def broadcast_restart_sync(self, message: str) -> None:
        self._broadcast_sync(message)

    # ── Polling thread ───────────────────────────────────────────────────────
    def start_polling_for_all(self) -> None:
        """
        Spawn one daemon polling thread per configured user.

        Each thread is bound to (user_id, bot_token, chat_id) — that user's
        own Telegram bot handle. If a user adds creds later via the dashboard,
        the poller won't see them until the next bot restart; non-polling
        sends are picked up on the next cache TTL refresh.

        Locked user command set (Delfi Messages Spec v1):
          /status /pause /resume /apply /reject /help
        """
        for uid in list_users_with_telegram():
            creds = get_user_telegram_creds(uid)
            if creds is None:
                continue
            token, chat_id = creds
            self._spawn_poller(uid, token, chat_id)

    def _spawn_poller(self, user_id: str, token: str, chat_id: str) -> None:
        url_base = f"https://api.telegram.org/bot{token}"
        allowed_chat_id = str(chat_id)

        def _poll_loop() -> None:
            offset = 0
            try:
                init_url = (f"{url_base}/getUpdates"
                            f"?offset=-1&timeout=0&allowed_updates=%5B%22message%22%5D")
                with urllib.request.urlopen(urllib.request.Request(init_url), timeout=10) as resp:
                    init_data = json.loads(resp.read())
                if init_data.get("ok"):
                    results = init_data.get("result", [])
                    if results:
                        offset = results[-1]["update_id"] + 1
                        print(f"[telegram] polling user={user_id} starting at offset {offset} "
                              f"(skipped prior messages).", flush=True)
            except Exception as exc:
                print(f"[telegram] polling init warning (user={user_id}): {exc}",
                      file=sys.stderr)

            handlers = {
                "/status": lambda: self._send_status(user_id),
                "/pause":  lambda: self._handle_pause(user_id),
                "/resume": lambda: self._handle_resume(user_id),
                "/apply":  lambda: self._handle_apply(user_id),
                "/reject": lambda: self._handle_reject(user_id),
                "/help":   lambda: self._handle_help(user_id),
            }

            while True:
                try:
                    url = (f"{url_base}/getUpdates"
                           f"?offset={offset}&timeout=5&allowed_updates=%5B%22message%22%5D")
                    with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as resp:
                        data = json.loads(resp.read())
                    if not data.get("ok"):
                        time.sleep(5)
                        continue
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        try:
                            msg      = update.get("message", {})
                            msg_text = msg.get("text", "").strip().lower()
                            sender   = str(msg.get("chat", {}).get("id", ""))
                            if sender != allowed_chat_id:
                                continue
                            loop = self._loop
                            if loop is None or not loop.is_running():
                                continue
                            fn = handlers.get(msg_text)
                            if fn is not None:
                                asyncio.run_coroutine_threadsafe(fn(), loop)
                        except Exception as exc:
                            print(f"[telegram] command dispatch error (user={user_id}): "
                                  f"{exc}", file=sys.stderr)
                except Exception as exc:
                    print(f"[telegram] poll loop error (user={user_id}): {exc}",
                          file=sys.stderr)
                    time.sleep(5)

        threading.Thread(
            target=_poll_loop, daemon=True,
            name=f"telegram-poll-{user_id[:8]}",
        ).start()

    # ── Command handlers ─────────────────────────────────────────────────────
    async def _handle_help(self, user_id: str) -> None:
        await self.send(user_id, tm.help_text())

    async def _handle_pause(self, user_id: str) -> None:
        set_trading_paused(True)
        await self.send(user_id, tm.paused())

    async def _handle_resume(self, user_id: str) -> None:
        set_trading_paused(False)
        await self.send(user_id, tm.resumed())

    async def _handle_apply(self, user_id: str) -> None:
        if self._bot_api is None:
            print("[telegram][admin] /apply received but BotAPI not wired",
                  file=sys.stderr)
            await self.send(user_id, tm.nothing_pending())
            return
        try:
            result = self._bot_api.apply_pending_config()
        except Exception as exc:
            print(f"[telegram][admin] apply failed: {exc}", file=sys.stderr)
            await self.send(user_id, tm.generic_error(
                context="Applying change", detail=str(exc),
            ))
            return
        status = result.get("status")
        if status == "applied":
            await self.send(user_id, tm.calibration_applied(
                key=result["key"],
                previous=result.get("previous"),
                value=result.get("value"),
                restart_required=bool(result.get("restart_required")),
            ))
        else:
            await self.send(user_id, tm.nothing_pending())

    async def _handle_reject(self, user_id: str) -> None:
        if self._bot_api is None:
            print("[telegram][admin] /reject received but BotAPI not wired",
                  file=sys.stderr)
            await self.send(user_id, tm.nothing_pending())
            return
        try:
            result = self._bot_api.reject_pending_config()
        except Exception as exc:
            print(f"[telegram][admin] reject failed: {exc}", file=sys.stderr)
            await self.send(user_id, tm.generic_error(
                context="Declining change", detail=str(exc),
            ))
            return
        if result.get("status") == "rejected":
            await self.send(user_id, tm.calibration_declined())
        else:
            await self.send(user_id, tm.nothing_pending())

    async def _send_status(self, user_id: str) -> None:
        if self._executor is None:
            print("[telegram][admin] /status received but executor not wired",
                  file=sys.stderr)
            await self.send(user_id, tm.generic_error(
                context="Status", detail="not ready yet",
            ))
            return
        try:
            stats = self._executor.get_portfolio_stats()
            uptime = "n/a"
            if self._bot_start_time:
                secs = int((datetime.now(timezone.utc) - self._bot_start_time).total_seconds())
                hrs, rem = divmod(secs, 3600)
                mins, _ = divmod(rem, 60)
                uptime = f"{hrs}h {mins}m"

            open_rows = self._executor.get_open_positions()
            pos_lines = []
            for p in open_rows[:10]:
                pos_lines.append(
                    f"• {p['side']} ${p['cost_usd']:.2f} — {p['question'][:60]}"
                )
            if len(open_rows) > 10:
                pos_lines.append(f"…and {len(open_rows) - 10} more.")
            positions_block = "\n".join(pos_lines) or "(none)"

            settled_total = int(stats.get("settled_total", 0))
            wins = int(stats.get("settled_wins", 0))
            losses = max(0, settled_total - wins)
            win_pct = (wins / settled_total * 100.0) if settled_total else 0.0

            await self.send(user_id, tm.status(
                uptime=uptime,
                bankroll=float(stats.get("bankroll", 0.0)),
                open_positions=int(stats.get("open_positions", 0)),
                open_cost=float(stats.get("open_cost", 0.0)),
                wins=wins,
                losses=losses,
                win_pct=win_pct,
                realized_pnl=float(stats.get("realized_pnl", 0.0)),
                positions_block=positions_block,
            ))
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"[telegram][admin] _send_status failed: {tb}", file=sys.stderr)
            await self.send(user_id, tm.generic_error(context="Status", detail=str(exc)))


# Module-level singleton — every other module imports this directly.
notifier = TelegramNotifier()
