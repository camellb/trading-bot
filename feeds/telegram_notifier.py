"""
Telegram Notifier — Polymarket edition.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment. If
either is missing, `enabled` is False and every method is a no-op.

Responsibilities:
  * Send formatted HTML alerts for PM positions, feed health, and startup.
  * Deliver scheduled daily + weekly summaries (bankroll, open positions,
    Brier score, resolved P&L).
  * Poll the Telegram Bot API for incoming commands from the configured
    chat and dispatch /status, /apply, /skip, /confirm,
    /reject, /help onto the running asyncio loop.

Every public method is exception-safe. A broken Telegram path must never
take the bot down.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone, timedelta
from typing import Optional

import aiohttp
from sqlalchemy import text

import config
from db.engine import get_engine


_TELEGRAM_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Exponential backoff constants for polling errors
_POLL_BACKOFF_BASE = 5       # initial backoff (seconds)
_POLL_BACKOFF_MAX  = 120     # cap (seconds)
_POLL_BACKOFF_409  = 30      # longer pause for 409 Conflict


class TelegramNotifier:
    """Thin wrapper around the Telegram Bot API."""

    def __init__(self) -> None:
        self._token:   str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled: bool = bool(self._token and self._chat_id)
        if self.enabled:
            print(f"[telegram] Notifications enabled. Chat ID: {self._chat_id}",
                  flush=True)
        else:
            print("[telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — "
                  "Telegram notifications disabled.", file=sys.stderr)

        self._session: Optional[aiohttp.ClientSession] = None

        # Wired in by main.py after construction.
        self._loop:            Optional[asyncio.AbstractEventLoop] = None
        self._bot_start_time:  Optional[datetime] = None
        self._monitor                              = None   # FeedHealthMonitor
        self._executor                             = None   # PMExecutor
        self._analyst                              = None   # PMAnalyst
        self._bot_api                              = None   # BotAPI

        # Polling thread tracking — used for health monitoring
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_self_improvement = None   # stored for restart
        self._poll_restart_count: int = 0

    # ── Core send ────────────────────────────────────────────────────────────
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=aiohttp.resolver.ThreadedResolver(),
                ),
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def send(self, message: str) -> None:
        if not self.enabled:
            return
        url = _TELEGRAM_BASE.format(token=self._token)
        payload = {"chat_id": self._chat_id, "text": message, "parse_mode": "HTML"}
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[telegram] send failed {resp.status}: {body[:200]}",
                          file=sys.stderr)
        except Exception as exc:
            print(f"[telegram] send exception: {exc}", file=sys.stderr)

    # ── Feed health ──────────────────────────────────────────────────────────
    async def notify_feed_degraded(self, feed_name: str, detail: str) -> None:
        await self.send(
            f"⚠️ <b>{feed_name} data feed is down</b>\n{detail}"
        )

    async def notify_feed_recovered(self, feed_name: str) -> None:
        await self.send(f"✅ <b>{feed_name} data feed is back</b>")

    # ── Settlement ──────────────────────────────────────────────────────────
    async def notify_settlement(self, position_id: int, question: str,
                                 side: str, outcome: str, pnl: float,
                                 cost: float) -> None:
        if not self.enabled:
            return
        won = (outcome == side)
        icon = "✅" if won else ("❌" if outcome != "INVALID" else "⚠️")
        result = "WIN" if won else ("INVALID" if outcome == "INVALID" else "LOSS")
        roi = (pnl / cost * 100) if cost > 0 else 0
        payout = max(0.0, cost + pnl)
        balance_line = ""
        if self._executor:
            try:
                bankroll = await asyncio.to_thread(self._executor.get_bankroll)
                balance_line = f"\nBalance: ${bankroll:.2f}"
            except Exception:
                pass
        await self.send(
            f"{icon} <b>Bet settled</b> #{position_id} — {result}\n"
            f"{question[:120]}\n"
            f"Your side: {side}\n"
            f"Market result: {outcome}\n"
            f"Stake: ${cost:.2f}\n"
            f"Payout: ${payout:.2f}\n"
            f"Net P/L: ${pnl:+.2f} ({roi:+.0f}%)"
            f"{balance_line}"
        )

    # ── Error alerts ────────────────────────────────────────────────────────
    async def notify_error(self, context: str, detail: str) -> None:
        if not self.enabled:
            return
        await self.send(
            f"🚨 <b>Something went wrong</b>\n"
            f"{context}: {detail[:200]}"
        )

    async def notify_info(self, title: str, detail: str) -> None:
        if not self.enabled:
            return
        await self.send(
            f"ℹ️ <b>{title}</b>\n"
            f"{detail[:240]}"
        )

    def send_sync(self, message: str) -> None:
        """Blocking send for use outside the event loop (crash handler, shutdown)."""
        if not self.enabled:
            return
        url = _TELEGRAM_BASE.format(token=self._token)
        payload = json.dumps(
            {"chat_id": self._chat_id, "text": message, "parse_mode": "HTML"}
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

    # ── Startup ──────────────────────────────────────────────────────────────
    async def notify_startup(self) -> None:
        if not self.enabled:
            return
        try:
            stats = (await asyncio.to_thread(self._executor.get_portfolio_stats)
                     if self._executor else {})
            mode = stats.get("mode", "shadow")
            sim = " (simulated)" if mode == "shadow" else ""

            import calibration
            brier_report = await asyncio.to_thread(
                calibration.get_report, source="polymarket"
            )
            brier_val = brier_report.get("brier")
            accuracy = f"{brier_val:.3f}" if brier_val is not None else "n/a"
            resolved = brier_report.get("resolved", 0)

            balance = stats.get("bankroll", 0)
            open_n = stats.get("open_positions", 0)
            at_risk = stats.get("open_cost", 0)
            total_pnl = stats.get("realized_pnl", 0)

            lines = [
                f"🤖 <b>Bot started</b>{sim}",
                f"Balance: ${balance:.2f} | P&L: ${total_pnl:+.2f}",
                f"Open bets: {open_n} (${at_risk:.2f} at risk)",
                f"Accuracy: {accuracy} over {resolved} predictions",
            ]
            await self.send("\n".join(lines))
        except Exception as exc:
            print(f"[telegram] notify_startup failed: {exc}", file=sys.stderr)
            await self.send("🤖 <b>Bot started</b>")

    # ── Daily / weekly summaries ────────────────────────────────────────────
    async def send_daily_summary(self) -> None:
        if not self.enabled or self._executor is None:
            return
        try:
            stats = await asyncio.to_thread(self._executor.get_portfolio_stats)
            mode  = stats.get("mode", "shadow")

            def _daily_db():
                with get_engine().begin() as conn:
                    row = conn.execute(text(
                        "SELECT "
                        "  COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 day'), "
                        "  COUNT(*) FILTER (WHERE resolved_at >= NOW() - INTERVAL '1 day') "
                        "FROM predictions WHERE source = 'polymarket'"
                    )).fetchone()
                    pnl = conn.execute(text(
                        "SELECT COALESCE(SUM(realized_pnl_usd), 0) FROM pm_positions "
                        "WHERE mode = :m AND settled_at >= NOW() - INTERVAL '1 day'"
                    ), {"m": mode}).scalar() or 0.0
                return int(row[0]), int(row[1]), float(pnl)

            import calibration
            loop = asyncio.get_running_loop()
            (cnt24, resolved24, pnl24), brier = await asyncio.gather(
                loop.run_in_executor(None, _daily_db),
                loop.run_in_executor(None, lambda: calibration.get_report(source="polymarket")),
            )
            brier_val = brier.get("brier")
            brier_line = f"{brier_val:.3f}" if brier_val is not None else "n/a"

            wins = stats['settled_wins']
            losses = stats['settled_total'] - wins
            win_pct = (wins / stats['settled_total'] * 100
                       if stats['settled_total'] else 0)
            msg = (
                f"📊 <b>Daily update</b>\n"
                f"Balance: ${stats['bankroll']:.2f}\n"
                f"Open bets: {stats['open_positions']} "
                f"(${stats['open_cost']:.2f} at risk)\n"
                f"Record: {wins}W {losses}L ({win_pct:.0f}%) "
                f"P&L ${stats['realized_pnl']:+.2f}\n"
                f"Today: {cnt24} analysed, "
                f"{resolved24} settled, ${pnl24:+.2f}\n"
                f"Accuracy: {brier_line} "
                f"({brier.get('resolved', 0)} scored)"
            )
            await self.send(msg)
        except Exception as exc:
            print(f"[telegram] send_daily_summary failed: {exc}", file=sys.stderr)

    async def send_weekly_summary(self) -> None:
        if not self.enabled or self._executor is None:
            return
        try:
            stats = await asyncio.to_thread(self._executor.get_portfolio_stats)
            mode  = stats.get("mode", "shadow")

            def _weekly_db():
                with get_engine().begin() as conn:
                    row = conn.execute(text(
                        "SELECT COALESCE(SUM(realized_pnl_usd), 0), "
                        "  COUNT(*) FILTER (WHERE status = 'settled'), "
                        "  COUNT(*) FILTER (WHERE status = 'settled' AND realized_pnl_usd > 0) "
                        "FROM pm_positions "
                        "WHERE mode = :m AND settled_at >= NOW() - INTERVAL '7 days'"
                    ), {"m": mode}).fetchone()
                return float(row[0]), int(row[1]), int(row[2])

            import calibration
            loop = asyncio.get_running_loop()
            (pnl7, total7, wins7), brier7, brier_all = await asyncio.gather(
                loop.run_in_executor(None, _weekly_db),
                loop.run_in_executor(None, lambda: calibration.get_report(source="polymarket", since_days=7)),
                loop.run_in_executor(None, lambda: calibration.get_report(source="polymarket")),
            )
            brier_val = brier7.get("brier")
            brier_line = f"{brier_val:.3f}" if brier_val is not None else "n/a"
            wr_line = f"{wins7}/{total7}" if total7 else "n/a"

            brier_all_val = brier_all.get("brier")
            resolved_all = brier_all.get("resolved", 0)
            pnl_all = stats.get("realized_pnl", 0)
            g1 = "✅" if brier_all_val is not None and brier_all_val < config.GO_LIVE_MAX_BRIER else "⬜"
            g2 = "✅" if resolved_all >= config.GO_LIVE_MIN_RESOLVED else "⬜"
            g3 = "✅" if pnl_all > config.GO_LIVE_MIN_REALIZED_PNL else "⬜"
            brier_all_str = f"{brier_all_val:.3f}" if brier_all_val is not None else "n/a"

            msg = (
                f"📈 <b>Weekly update</b>\n"
                f"Balance: ${stats['bankroll']:.2f}\n"
                f"This week: {wr_line} wins, ${pnl7:+.2f}\n"
                f"Accuracy (7d): {brier_line}\n"
                f"All-time: ${pnl_all:+.2f} over {stats['settled_total']} bets\n\n"
                f"<b>Ready for real money?</b>\n"
                f"{g1} Accuracy: {brier_all_str} (need &lt;{config.GO_LIVE_MAX_BRIER})\n"
                f"{g2} Sample size: {resolved_all} (need {config.GO_LIVE_MIN_RESOLVED}+)\n"
                f"{g3} Profitable: ${pnl_all:+.2f} (need &gt;$0)"
            )
            await self.send(msg)
        except Exception as exc:
            print(f"[telegram] send_weekly_summary failed: {exc}", file=sys.stderr)

    # ── Polling thread ───────────────────────────────────────────────────────
    def _delete_webhook(self) -> None:
        """Call deleteWebhook on startup to prevent 409 Conflict errors.

        A stale webhook (from a previous instance or external setup) causes
        Telegram to reject getUpdates with 409, killing the polling thread.
        """
        url = f"https://api.telegram.org/bot{self._token}/deleteWebhook"
        try:
            req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            if result.get("ok"):
                print("[telegram] deleteWebhook OK — cleared any stale webhook", flush=True)
            else:
                print(f"[telegram] deleteWebhook response: {result}", file=sys.stderr)
        except Exception as exc:
            print(f"[telegram] deleteWebhook failed (non-fatal): {exc}", file=sys.stderr)

    def start_polling(self, self_improvement=None) -> None:
        """
        Daemon thread that polls getUpdates and dispatches commands.
        Accepts /apply, /skip, /status, /scan, /resolve, /confirm, /reject, /help.

        Features exponential backoff on errors (5s → 120s cap, reset on success),
        special handling for 409 Conflict, and deleteWebhook on startup.
        """
        if not self.enabled:
            return

        # Store for restart_polling()
        self._poll_self_improvement = self_improvement

        token    = self._token
        chat_id  = str(self._chat_id)
        url_base = f"https://api.telegram.org/bot{token}"

        # Clear any stale webhook before polling
        self._delete_webhook()

        def _poll_loop() -> None:
            backoff = _POLL_BACKOFF_BASE
            consecutive_errors = 0
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
                        print(f"[telegram] Polling starting at offset {offset} "
                              f"(skipped prior messages).", flush=True)
            except Exception as exc:
                print(f"[telegram] polling init warning: {exc}", file=sys.stderr)

            while True:
                try:
                    url = (f"{url_base}/getUpdates"
                           f"?offset={offset}&timeout=5&allowed_updates=%5B%22message%22%5D")
                    with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as resp:
                        data = json.loads(resp.read())

                    if not data.get("ok"):
                        # Check for 409 Conflict specifically
                        desc = str(data.get("description", ""))
                        if "409" in desc or "Conflict" in desc:
                            consecutive_errors += 1
                            if consecutive_errors <= 2:
                                # First occurrence — try clearing webhook again
                                print(f"[telegram] 409 Conflict — calling deleteWebhook "
                                      f"(attempt {consecutive_errors})", file=sys.stderr)
                                self._delete_webhook()
                            backoff = min(_POLL_BACKOFF_409 * consecutive_errors,
                                          _POLL_BACKOFF_MAX)
                            print(f"[telegram] 409 Conflict — backing off {backoff}s",
                                  file=sys.stderr)
                            time.sleep(backoff)
                        else:
                            consecutive_errors += 1
                            backoff = min(_POLL_BACKOFF_BASE * (2 ** min(consecutive_errors, 6)),
                                          _POLL_BACKOFF_MAX)
                            print(f"[telegram] getUpdates not ok: {desc} — "
                                  f"backing off {backoff}s", file=sys.stderr)
                            time.sleep(backoff)
                        continue

                    # Success — reset backoff
                    consecutive_errors = 0
                    backoff = _POLL_BACKOFF_BASE

                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        try:
                            msg      = update.get("message", {})
                            msg_text = msg.get("text", "").strip().lower()
                            sender   = str(msg.get("chat", {}).get("id", ""))
                            if sender != chat_id:
                                continue
                            loop = self._loop
                            if loop is None or not loop.is_running():
                                continue
                            if msg_text == "/apply":
                                if self_improvement is not None:
                                    asyncio.run_coroutine_threadsafe(
                                        self_improvement.apply_suggestions(), loop
                                    )
                            elif msg_text == "/skip":
                                if self_improvement is not None and hasattr(
                                        self_improvement, "skip_suggestions"):
                                    asyncio.run_coroutine_threadsafe(
                                        self_improvement.skip_suggestions(), loop
                                    )
                            elif msg_text == "/status":
                                asyncio.run_coroutine_threadsafe(
                                    self._send_status(), loop
                                )
                            elif msg_text == "/scan":
                                asyncio.run_coroutine_threadsafe(
                                    self._handle_scan(), loop
                                )
                            elif msg_text == "/resolve":
                                asyncio.run_coroutine_threadsafe(
                                    self._handle_resolve(), loop
                                )
                            elif msg_text in ("/confirm", "/confirm-config"):
                                asyncio.run_coroutine_threadsafe(
                                    self._handle_confirm_config(), loop
                                )
                            elif msg_text in ("/reject", "/reject-config"):
                                asyncio.run_coroutine_threadsafe(
                                    self._handle_reject_config(), loop
                                )
                            elif msg_text == "/help":
                                asyncio.run_coroutine_threadsafe(
                                    self._handle_help(), loop
                                )
                        except Exception as exc:
                            print(f"[telegram] command dispatch error: {exc}",
                                  file=sys.stderr)
                except urllib.error.HTTPError as exc:
                    consecutive_errors += 1
                    if exc.code == 409:
                        if consecutive_errors <= 2:
                            print(f"[telegram] HTTP 409 — calling deleteWebhook "
                                  f"(attempt {consecutive_errors})", file=sys.stderr)
                            self._delete_webhook()
                        backoff = min(_POLL_BACKOFF_409 * consecutive_errors,
                                      _POLL_BACKOFF_MAX)
                        print(f"[telegram] HTTP 409 Conflict — backing off {backoff}s",
                              file=sys.stderr)
                    else:
                        backoff = min(_POLL_BACKOFF_BASE * (2 ** min(consecutive_errors, 6)),
                                      _POLL_BACKOFF_MAX)
                        print(f"[telegram] HTTP {exc.code} — backing off {backoff}s",
                              file=sys.stderr)
                    time.sleep(backoff)
                except Exception as exc:
                    consecutive_errors += 1
                    backoff = min(_POLL_BACKOFF_BASE * (2 ** min(consecutive_errors, 6)),
                                  _POLL_BACKOFF_MAX)
                    print(f"[telegram] poll loop error: {exc} — "
                          f"backing off {backoff}s (errors: {consecutive_errors})",
                          file=sys.stderr)
                    time.sleep(backoff)

            # Should never reach here, but log if we do
            print("[telegram] WARNING: poll loop exited unexpectedly", file=sys.stderr)

        t = threading.Thread(target=_poll_loop, daemon=True, name="telegram-poll")
        t.start()
        self._poll_thread = t

    def is_polling_alive(self) -> bool:
        """Check if the polling thread is running."""
        return self._poll_thread is not None and self._poll_thread.is_alive()

    def restart_polling(self) -> None:
        """Restart the polling thread if it died.

        Called by the event-loop health monitor in main.py.
        """
        if not self.enabled:
            return
        self._poll_restart_count += 1
        print(f"[telegram] Restarting polling thread "
              f"(restart #{self._poll_restart_count})", flush=True)
        self.start_polling(self._poll_self_improvement)

    # ── Command handlers ─────────────────────────────────────────────────────
    async def _handle_help(self) -> None:
        await self.send(
            "🤖 <b>Commands</b>\n\n"
            "/status — balance, open bets, win rate\n"
            "/scan — look for new markets now\n"
            "/resolve — check for settled bets\n"
            "/apply — accept a bot suggestion\n"
            "/skip — reject a bot suggestion\n"
            "/confirm — apply a pending mode switch\n"
            "/reject — cancel a pending mode switch"
        )

    async def _send_status(self) -> None:
        if self._executor is None:
            await self.send("⚠️ Executor not wired — cannot produce status.")
            return
        try:
            stats = await asyncio.to_thread(self._executor.get_portfolio_stats)
            mode  = stats.get("mode", "shadow")
            uptime = ""
            if self._bot_start_time:
                secs = int((datetime.now(timezone.utc) - self._bot_start_time).total_seconds())
                hrs, rem = divmod(secs, 3600)
                mins, _ = divmod(rem, 60)
                uptime = f"{hrs}h {mins}m"
            degraded = self._monitor.get_degraded_feeds() if self._monitor else []
            degraded_line = ", ".join(degraded) if degraded else "all healthy"

            import calibration
            brier_report = await asyncio.to_thread(
                calibration.get_report, source="polymarket"
            )
            brier_val = brier_report.get("brier")
            brier_str = f"{brier_val:.3f}" if brier_val is not None else "n/a"

            open_rows = await asyncio.to_thread(self._executor.get_open_positions)
            pos_lines = []
            for p in open_rows[:10]:
                pos_lines.append(
                    f"• {p['side']} ${p['cost_usd']:.2f} — "
                    f"{p['question'][:60]}"
                )
            if len(open_rows) > 10:
                pos_lines.append(f"…and {len(open_rows) - 10} more.")
            positions_block = "\n".join(pos_lines) or "(none)"

            wins = stats['settled_wins']
            losses = stats['settled_total'] - wins

            # Risk state from the analyst's risk manager.
            risk_block = ""
            analyst = getattr(self, "_analyst", None)
            risk_mgr = getattr(analyst, "risk_mgr", None) if analyst else None
            if risk_mgr is not None:
                try:
                    rs = await asyncio.to_thread(risk_mgr.get_risk_state)
                    alerts = []
                    if rs.get("drawdown_halted"):
                        alerts.append(f"🛑 DRAWDOWN HALT ({rs['drawdown_pct']:.0%} drawdown)")
                    if rs.get("daily_limit_breached"):
                        alerts.append(f"⚠️ Daily loss limit hit (${rs['daily_pnl']:+.2f})")
                    if rs.get("weekly_limit_breached"):
                        alerts.append(f"⚠️ Weekly loss limit hit (${rs['weekly_pnl']:+.2f})")
                    if rs.get("heat_breached"):
                        alerts.append(f"🔥 Heat limit ({rs['heat_pct']:.0%} deployed)")
                    if rs.get("cooldown_trades_remaining", 0) > 0:
                        alerts.append(f"❄️ Streak cooldown ({rs['cooldown_trades_remaining']} trades)")
                    if alerts:
                        risk_block = "\n\n<b>Risk alerts</b>\n" + "\n".join(alerts)
                    else:
                        risk_block = f"\n\nRisk: ✅ all clear (heat {rs['heat_pct']:.0%}, dd {rs['drawdown_pct']:.0%})"
                except Exception:
                    pass

            await self.send(
                f"📊 <b>Status</b> (up {uptime or 'n/a'})\n"
                f"Balance: ${stats['bankroll']:.2f}\n"
                f"Locked capital: ${stats['open_cost']:.2f}\n"
                f"Open bets: {stats['open_positions']}\n"
                f"Record: {wins}W {losses}L\n"
                f"Realized P&L: ${stats['realized_pnl']:+.2f}\n"
                f"Accuracy: {brier_str} "
                f"({brier_report.get('resolved', 0)} scored)\n"
                f"Feeds: {degraded_line}"
                f"{risk_block}\n"
                f"\n<b>Open bets</b>\n{positions_block}"
            )
        except Exception as exc:
            print(f"[telegram] _send_status failed: {exc}", file=sys.stderr)
            await self.send(f"⚠️ /status failed: {exc}")

    async def _handle_scan(self) -> None:
        await self.send("🔎 Looking for markets — this takes 1-2 min…")
        try:
            from polymarket_runner import scan_via_subprocess
            summary = await scan_via_subprocess(
                limit=int(getattr(config, "PM_SCAN_LIMIT", 20)),
                min_volume_24h=float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
            )
            if summary.get("error"):
                await self.send(f"⚠️ Scan error: {summary['error']}")
                return
            if summary.get("skipped") is True:
                reason = str(summary.get("reason") or "scan skipped")
                if reason == "scan already in progress":
                    await self.send("⏳ Already scanning — try again in a minute.")
                elif reason == "stale positions pending settlement":
                    catchup = summary.get("catchup") or {}
                    stale = catchup.get("stale_after")
                    await self.send(
                        "⏸️ Scan paused — "
                        f"{stale or 'some'} stale markets still need settlement. "
                        "Run /resolve or wait for the resolver to catch up."
                    )
                else:
                    await self.send(f"⏸️ Scan skipped — {reason}.")
                return
            opened = summary.get('opened', 0)
            fetched = summary.get('fetched', 0)
            msg = f"✅ <b>Scan done</b> — found {fetched} markets"
            if opened:
                msg += f", placed {opened} new bet{'s' if opened != 1 else ''}"
            else:
                msg += ", no new bets"
            await self.send(msg)
            # Send per-position details.
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
                    await self.send(
                        f"🎯 <b>{oc['question'][:140]}</b>\n"
                        f"Buy {side} at {entry_c:.1f}c — "
                        f"${t['stake_usd']:.2f} stake, "
                        f"{t['edge_bps']:.0f}bps edge\n"
                        f"Position #{t['position_id']}"
                    )
                except Exception:
                    pass
        except Exception as exc:
            await self.send(f"⚠️ Scan failed: {exc}")

    async def _handle_resolve(self) -> None:
        await self.send("🔎 Checking for settled bets…")
        try:
            from polymarket_runner import resolve_positions
            risk_mgr = getattr(self._analyst, "risk_mgr", None) if self._analyst else None
            result = await resolve_positions(
                notifier=self,
                executor=self._executor,
                risk_mgr=risk_mgr,
            )
            settled = result.get("positions_settled", 0)
            checked = result.get("positions_checked", 0)
            if settled:
                await self.send(f"✅ {settled} bet{'s' if settled != 1 else ''} settled (checked {checked})")
            else:
                await self.send(f"✅ Nothing new — checked {checked} bets")
        except Exception as exc:
            await self.send(f"⚠️ Resolve failed: {exc}")

    async def _handle_confirm_config(self) -> None:
        if self._bot_api is None:
            await self.send("⚠️ BotAPI not wired.")
            return
        try:
            result = await asyncio.to_thread(self._bot_api.apply_pending_config)
        except Exception as exc:
            await self.send(f"⚠️ Config apply failed: {exc}")
            return
        if result.get("status") == "applied":
            restart_note = ""
            if result.get("restart_required"):
                restart_note = "\n⚠️ Restart needed — run: ./bot.sh restart"
            await self.send(
                f"✅ Setting changed: {result['key']}\n"
                f"{result['previous']} → {result['value']}"
                f"{restart_note}"
            )
        elif result.get("status") == "none":
            await self.send("Nothing to apply — no pending change.")
        else:
            await self.send(f"⚠️ Failed: {result.get('reason')}")

    async def _handle_reject_config(self) -> None:
        if self._bot_api is None:
            await self.send("⚠️ BotAPI not wired.")
            return
        try:
            result = self._bot_api.reject_pending_config()
        except Exception as exc:
            await self.send(f"⚠️ Config reject failed: {exc}")
            return
        if result.get("status") == "rejected":
            await self.send(
                f"❎ Cancelled: {result['key']} stays at {result.get('previous', 'current value')}"
            )
        else:
            await self.send("Nothing to cancel — no pending change.")


# Module-level singleton — every other module imports this directly.
notifier = TelegramNotifier()
