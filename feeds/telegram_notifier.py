"""
Telegram Notifier — sends trade and system alerts to a configured Telegram chat.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment.
If either is missing, self.enabled = False and all methods are no-ops.

Alerts:
  - Trade opened / closed
  - Kill switch triggered
  - Feed degraded (only after 60s continuous degradation)
  - Feed recovered
  - Daily summary   (scheduled 08:00 UTC every day — covers previous day)
  - Weekly summary  (scheduled Monday 08:00 UTC — covers previous 7 days)

The notifier is entirely non-blocking — every method catches all exceptions
and returns silently. A Telegram failure must never affect trading logic.
"""

import asyncio
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timezone, timedelta
from typing import Optional

import aiohttp
from sqlalchemy import create_engine, text

import config
from db.models import daily_pnl as daily_pnl_table, trades as trades_table

_TELEGRAM_BASE = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """
    Sends formatted HTML messages to a Telegram chat via the Bot API.

    All public methods are async. All are safe to call unconditionally —
    they check self.enabled as the first step.
    """

    def __init__(self) -> None:
        self._token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

        if not self._token or not self._chat_id:
            self.enabled = False
            print(
                "[telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — "
                "Telegram notifications disabled.",
                file=sys.stderr,
            )
        else:
            self.enabled = True
            print(
                f"[telegram] Notifications enabled. Chat ID: {self._chat_id}",
                flush=True,
            )

        # Kill switch dedup — only notify once per calendar day
        self._kill_switch_notified_date: Optional[date] = None

        # Loss warning dedup — only notify once per calendar day
        self._loss_warning_sent_date: Optional[date] = None

        # Event loop reference — set by main.py so the polling thread can
        # schedule coroutines onto the running loop via run_coroutine_threadsafe.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # References wired in by main.py after all components are created
        self._bot_start_time: Optional[datetime] = None
        self._monitor = None        # FeedHealthMonitor singleton
        self._ws_manager = None     # OKXWebSocketManager (for live mark prices)
        self._order_manager    = None  # OrderManager — wired for /resume command
        self._position_monitor = None  # PositionMonitor — wired for /resume reconciliation check

    # ── Core send ─────────────────────────────────────────────────────────────

    async def send(self, message: str) -> None:
        """
        POST a message to the Telegram Bot API.

        parse_mode = HTML. Emojis and <b> tags are supported.
        Catches all exceptions silently — never raises.
        """
        if not self.enabled:
            return
        url = _TELEGRAM_BASE.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        print(
                            f"[telegram] send failed HTTP {resp.status}: {body[:200]}",
                            file=sys.stderr,
                        )
        except Exception as exc:
            print(f"[telegram] send error: {exc}", file=sys.stderr)

    # ── Trade alerts ──────────────────────────────────────────────────────────

    async def notify_trade_opened(
        self, cycle_result: dict, fill_result: dict
    ) -> None:
        """Send alert when a new position is opened."""
        if not self.enabled:
            return
        mode = "PAPER" if config.PAPER_MODE else "LIVE"
        rr = cycle_result.get("rr_ratio") or 0.0
        msg = (
            f"🟢 <b>TRADE OPENED</b>\n"
            f"Pair: {cycle_result.get('pair')} | {cycle_result.get('signal')}\n"
            f"Entry: {fill_result.get('filled_price', cycle_result.get('entry_price'))}\n"
            f"Stop: {cycle_result.get('stop_loss')} | TP: {cycle_result.get('take_profit')}\n"
            f"Size: USD {cycle_result.get('order_size_usd', 0.0):.2f}\n"
            f"Regime: {cycle_result.get('regime')}\n"
            f"R:R: {rr:.2f}\n"
            f"Mode: {mode}"
        )
        await self.send(msg)

    async def notify_trade_closed(self, trade: dict) -> None:
        """
        Send alert when a position is closed.

        trade dict must include: pair, direction, entry_price, exit_price,
        pnl_usd, close_reason.
        """
        if not self.enabled:
            return
        pnl = trade.get("pnl_usd", 0.0) or 0.0
        emoji = "🟢" if pnl > 0 else "🔴"
        msg = (
            f"{emoji} <b>TRADE CLOSED</b>\n"
            f"Pair: {trade.get('pair')} | {trade.get('direction')}\n"
            f"Entry: {trade.get('entry_price')} -> Exit: {trade.get('exit_price')}\n"
            f"PnL: {pnl:+.2f} USD\n"
            f"Reason: {trade.get('close_reason')}"
        )
        await self.send(msg)

    # ── System alerts ─────────────────────────────────────────────────────────

    async def notify_kill_switch(self, daily_pnl: float) -> None:
        """
        Send kill switch alert. Deduped — fires at most once per calendar day.
        """
        if not self.enabled:
            return
        today = datetime.now(timezone.utc).date()
        if self._kill_switch_notified_date == today:
            return
        self._kill_switch_notified_date = today
        msg = (
            f"🚨 <b>KILL SWITCH ACTIVE</b>\n"
            f"Daily loss cap hit: USD {daily_pnl:.2f}\n"
            f"No new trades until tomorrow."
        )
        await self.send(msg)

    async def notify_loss_warning(self, daily_pnl: float) -> None:
        """
        Send a loss warning when today's P&L exceeds 50% of the daily cap.
        Deduped — fires at most once per calendar day.
        """
        if not self.enabled:
            return
        today = datetime.now(timezone.utc).date()
        if self._loss_warning_sent_date == today:
            return
        self._loss_warning_sent_date = today
        pct = abs(daily_pnl) / config.DAILY_LOSS_CAP_USD * 100
        msg = (
            f"⚠️ <b>LOSS WARNING</b>\n"
            f"Daily P&amp;L: {daily_pnl:.2f} USD\n"
            f"({pct:.0f}% of {config.DAILY_LOSS_CAP_USD:.0f} USD daily cap)\n"
            f"Bot is still trading."
        )
        await self.send(msg)

    async def notify_feed_degraded(self, feed_name: str, detail: str) -> None:
        """
        Send feed degradation alert.

        All gating (startup grace period, 60s continuous-degradation threshold,
        dedup) is enforced by FeedHealthMonitor before calling this method.
        This method is a pure send — no internal timing logic.
        """
        if not self.enabled:
            return
        msg = (
            f"⚠️ <b>FEED DEGRADED</b>\n"
            f"Feed: {feed_name}\n"
            f"Detail: {detail}\n"
            f"Bot running at reduced capacity."
        )
        await self.send(msg)

    async def notify_feed_recovered(self, feed_name: str) -> None:
        """
        Send feed recovery alert.

        Only called by FeedHealthMonitor when a degradation alert was previously
        sent for this feed (i.e. no orphaned recovery messages possible).
        This method is a pure send — no internal state.
        """
        if not self.enabled:
            return
        msg = (
            f"✅ <b>FEED RECOVERED</b>\n"
            f"{feed_name} is healthy again."
        )
        await self.send(msg)

    # ── Scheduled summaries ───────────────────────────────────────────────────

    def _db_engine(self):
        """Return a fresh SQLAlchemy engine for the configured DATABASE_URL."""
        return create_engine(os.environ["DATABASE_URL"])

    async def send_daily_summary(self) -> None:
        """
        Comprehensive morning brief sent at 08:30 MYT.
        Waits 30 s so generate_daily_brief() has already written to macro_context_log.
        Combines macro outlook, portfolio state, yesterday's performance, live prices,
        upcoming events, and Claude's current thesis into one message.
        """
        if not self.enabled:
            return
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return

        # Give macro_context.generate_daily_brief() time to write to DB first
        await asyncio.sleep(30)

        now_myt = datetime.now(timezone(timedelta(hours=8)))
        date_str = now_myt.strftime("%d %b %Y")
        report_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        mode = "PAPER" if config.PAPER_MODE else "LIVE"

        # ── DB queries ────────────────────────────────────────────────────────
        try:
            engine = self._db_engine()
            with engine.begin() as conn:
                p = {"paper": config.PAPER_MODE, "date": report_date}

                # Yesterday's P&L
                pnl_row = conn.execute(text(
                    "SELECT pnl_usd, trade_count FROM daily_pnl "
                    "WHERE date = :date AND paper = :paper"
                ), p).fetchone()
                day_pnl    = float(pnl_row[0]) if pnl_row else 0.0
                day_trades = int(pnl_row[1])   if pnl_row else 0

                # All-time total P&L
                total_row = conn.execute(text(
                    "SELECT COALESCE(SUM(pnl_usd), 0) FROM daily_pnl WHERE paper = :paper"
                ), {"paper": config.PAPER_MODE}).fetchone()
                total_pnl = float(total_row[0]) if total_row else 0.0

                # Open positions with full detail
                open_rows = conn.execute(text(
                    "SELECT pair, direction, entry_price, size_usd, "
                    "       stop_loss, take_profit, thesis "
                    "FROM trades WHERE paper = :paper AND timestamp_close IS NULL "
                    "ORDER BY timestamp_open DESC"
                ), {"paper": config.PAPER_MODE}).fetchall()

                # Latest macro context
                macro_row = conn.execute(text(
                    "SELECT sentiment, confidence, risk_multiplier, "
                    "       reasoning, watch_for, key_events "
                    "FROM macro_context_log "
                    "ORDER BY generated_at DESC LIMIT 1"
                )).fetchone()

        except Exception as exc:
            print(f"[telegram] send_daily_summary DB error: {exc}", file=sys.stderr)
            return

        # ── Live OKX prices ───────────────────────────────────────────────────
        live_prices: dict[str, dict] = {}
        try:
            async with aiohttp.ClientSession() as sess:
                tasks = [
                    sess.get(
                        f"https://www.okx.com/api/v5/market/ticker?instId={pair}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    )
                    for pair in config.TRADING_PAIRS
                ]
                responses = await asyncio.gather(*tasks, return_exceptions=True)
                for pair, resp in zip(config.TRADING_PAIRS, responses):
                    if isinstance(resp, Exception):
                        continue
                    async with resp as r:
                        data = await r.json(content_type=None)
                    row = (data.get("data") or [{}])[0]
                    last   = float(row.get("last",   0) or 0)
                    open24 = float(row.get("open24h", 0) or 0)
                    chg    = (last - open24) / open24 * 100 if open24 else 0.0
                    live_prices[pair] = {"last": last, "change_24h": chg}
        except Exception as exc:
            print(f"[telegram] send_daily_summary prices error: {exc}", file=sys.stderr)

        # ── Fear & Greed ──────────────────────────────────────────────────────
        fg_val: int | None = None
        fg_lab = ""
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    "https://api.alternative.me/fng/?limit=1",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    fg_data = await r.json(content_type=None)
            fg_row = (fg_data.get("data") or [{}])[0]
            fg_val = int(fg_row.get("value", 0))
            fg_lab = fg_row.get("value_classification", "")
        except Exception:
            pass

        # ── Upcoming events ───────────────────────────────────────────────────
        upcoming_lines: list[str] = []
        try:
            cal = getattr(self, "_macro_calendar", None)
            if cal is not None and hasattr(cal, "get_upcoming_events"):
                events = cal.get_upcoming_events(days=3)
                for ev in events[:5]:
                    upcoming_lines.append(f"  • {ev}")
        except Exception:
            pass
        if not upcoming_lines and macro_row:
            raw_events = macro_row[5]  # key_events column
            if raw_events:
                evs = raw_events if isinstance(raw_events, list) else [
                    e.strip() for e in raw_events.split(",") if e.strip()
                ]
                upcoming_lines = [f"  • {e}" for e in evs[:5]]

        # ── Obsidian thesis ───────────────────────────────────────────────────
        thesis_text = ""
        try:
            vault = os.path.expanduser(config.OBSIDIAN_VAULT_PATH)
            with open(os.path.join(vault, "strategy/current-thesis.md"), encoding="utf-8") as f:
                thesis_text = f.read().strip()
        except Exception:
            pass

        # ── Format sections ───────────────────────────────────────────────────

        # MACRO section
        if macro_row:
            sentiment, confidence, risk_mult, reasoning, watch_for, _ = macro_row
            sent_upper = (sentiment or "").upper()
            sent_emoji = "🐂" if "BULL" in sent_upper else "🐻" if "BEAR" in sent_upper else "➡️"
            conf_pct   = float(confidence or 0)
            risk_str   = f"  Risk multiplier: {float(risk_mult):.1f}x\n" if risk_mult else ""
            short_reasoning = (reasoning or "")[:180].rstrip()
            if len(reasoning or "") > 180:
                short_reasoning += "…"
            fg_macro = (
                f"  Fear &amp; Greed: {fg_val} ({fg_lab})\n" if fg_val is not None else ""
            )
            macro_section = (
                f"{sent_emoji} {sentiment} ({conf_pct:.0f}% confidence)\n"
                f"  {short_reasoning}\n"
                f"  Watch today: {watch_for or '—'}\n"
                f"{fg_macro}"
                f"{risk_str}"
            ).rstrip()
        else:
            fg_macro = (
                f"  Fear &amp; Greed: {fg_val} ({fg_lab})\n" if fg_val is not None else ""
            )
            macro_section = f"  No macro data yet\n{fg_macro}".rstrip()

        # PORTFOLIO section
        deployed_usd = sum(float(r[3] or 0) for r in open_rows)
        balance_usd  = config.STARTING_CAPITAL_USD + total_pnl
        deployed_pct = deployed_usd / balance_usd * 100 if balance_usd else 0.0

        pos_lines: list[str] = []
        for r in open_rows:
            pair, direction, entry_p, size_usd, sl, tp, thesis = r
            entry  = float(entry_p or 0)
            size   = float(size_usd or 0)
            lp     = live_prices.get(pair, {}).get("last", 0.0)
            if entry and lp:
                unreal = (lp - entry) / entry * size if direction == "LONG" \
                         else (entry - lp) / entry * size
                unreal_str = f"{unreal:+.2f} USD"
            else:
                unreal_str = "—"
            short_name = pair.replace("-USDT-SWAP", "")
            sl_str = f"{float(sl):.2f}" if sl else "—"
            tp_str = f"{float(tp):.2f}" if tp else "—"
            pos_lines.append(
                f"  {direction} {short_name} @ {entry:.2f} | unrealised: {unreal_str}\n"
                f"    SL {sl_str} / TP {tp_str} | size ${size:.0f}"
            )
        portfolio_section = (
            f"  Balance: ${balance_usd:,.2f} | Deployed: ${deployed_usd:,.2f} "
            f"({deployed_pct:.0f}%)\n"
            + ("\n".join(pos_lines) if pos_lines else "  No open positions")
        )

        # MARKET section
        market_parts: list[str] = []
        for pair in config.TRADING_PAIRS:
            short = pair.replace("-USDT-SWAP", "")
            px    = live_prices.get(pair, {})
            last  = px.get("last", 0.0)
            chg   = px.get("change_24h", 0.0)
            if last:
                market_parts.append(f"{short}: ${last:,.2f} ({chg:+.1f}%)")
        market_section = "  " + " | ".join(market_parts) if market_parts else "  No price data"

        # UPCOMING EVENTS section
        events_section = "\n".join(upcoming_lines) if upcoming_lines \
                         else "  No major events in next 3 days"

        # THESIS section
        if thesis_text:
            thesis_trimmed = thesis_text[:400]
            if len(thesis_text) > 400:
                thesis_trimmed += "…"
            thesis_section = f"  {thesis_trimmed}"
        else:
            thesis_section = "  No thesis recorded yet"

        msg = (
            f"🌅 <b>DAILY BRIEF — {date_str} MYT</b>  [{mode}]\n"
            f"\n"
            f"<b>🌍 MACRO</b>\n"
            f"{macro_section}\n"
            f"\n"
            f"<b>📊 PORTFOLIO</b>\n"
            f"{portfolio_section}\n"
            f"\n"
            f"<b>📈 YESTERDAY</b>\n"
            f"  P&amp;L: {day_pnl:+.2f} USD | Trades closed: {day_trades}\n"
            f"  All-time P&amp;L: {total_pnl:+.2f} USD\n"
            f"\n"
            f"<b>📡 MARKET</b>\n"
            f"{market_section}\n"
            f"\n"
            f"<b>📅 UPCOMING EVENTS</b>\n"
            f"{events_section}\n"
            f"\n"
            f"<b>🧠 CLAUDE'S THESIS</b>\n"
            f"{thesis_section}"
        )
        await self.send(msg)

    async def send_weekly_summary(self) -> None:
        """
        Query the last 7 completed days and send a full weekly performance report.
        Scheduled at Monday 08:00 UTC via APScheduler in main.py.
        """
        if not self.enabled:
            return
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return

        mode = "PAPER" if config.PAPER_MODE else "LIVE"
        today = datetime.now(timezone.utc).date()
        # Covers the 7 days ending yesterday (the last fully completed day)
        end_date   = today - timedelta(days=1)
        start_date = today - timedelta(days=7)

        try:
            engine = self._db_engine()
            with engine.begin() as conn:
                p = {"paper": config.PAPER_MODE,
                     "start": start_date, "end": end_date}

                # 1. Daily P&L for each of the 7 days
                daily_rows = conn.execute(text(
                    "SELECT date, pnl_usd, trade_count FROM daily_pnl "
                    "WHERE paper = :paper "
                    "  AND date >= :start AND date <= :end "
                    "ORDER BY date ASC"
                ), p).fetchall()

                # 2. Closed trades in the window
                closed_rows = conn.execute(text(
                    "SELECT pair, direction, pnl_usd, close_reason, regime_at_entry "
                    "FROM trades "
                    "WHERE paper = :paper "
                    "  AND timestamp_close >= :start "
                    "  AND timestamp_close IS NOT NULL"
                ), p).fetchall()

                # 3. Regime distribution
                regime_rows = conn.execute(text(
                    "SELECT regime, COUNT(*) AS cnt FROM ticks "
                    "WHERE timestamp >= :start "
                    "GROUP BY regime ORDER BY cnt DESC"
                ), p).fetchall()

                # 4. Layer rejection breakdown
                layer_rows = conn.execute(text(
                    "SELECT "
                    "  CASE "
                    "    WHEN decision_reason LIKE 'Layer A%' THEN 'Layer A' "
                    "    WHEN decision_reason LIKE 'Layer B%' THEN 'Layer B' "
                    "    WHEN decision_reason LIKE 'Layer C%' THEN 'Layer C' "
                    "    WHEN decision_reason LIKE 'Layer D%' THEN 'Layer D' "
                    "    WHEN decision_reason LIKE 'Layer E%' THEN 'Layer E' "
                    "    WHEN decision_reason LIKE 'KILL SWITCH%' THEN 'Kill Switch' "
                    "    WHEN decision_reason LIKE 'max simultaneous%' THEN 'Max Positions' "
                    "    ELSE 'Other' "
                    "  END AS layer, "
                    "  COUNT(*) AS cnt "
                    "FROM ticks "
                    "WHERE timestamp >= :start "
                    "  AND decision = 'REJECT' "
                    "GROUP BY layer ORDER BY cnt DESC"
                ), p).fetchall()

        except Exception as exc:
            print(f"[telegram] send_weekly_summary DB error: {exc}", file=sys.stderr)
            return

        # ── Compute aggregates ────────────────────────────────────────────────

        total_pnl   = sum(float(r[1]) for r in daily_rows)
        pnls        = [float(r[2]) for r in closed_rows if r[2] is not None]
        wins        = sum(1 for p in pnls if p > 0)
        losses      = sum(1 for p in pnls if p <= 0)
        total_closed = len(pnls)
        win_rate    = (wins / total_closed * 100) if total_closed else 0.0

        # Best / worst day from daily_pnl rows
        if daily_rows:
            best_row  = max(daily_rows, key=lambda r: float(r[1]))
            worst_row = min(daily_rows, key=lambda r: float(r[1]))
            best_str  = f"{best_row[0]} ({float(best_row[1]):+.2f} USD)"
            worst_str = f"{worst_row[0]} ({float(worst_row[1]):+.2f} USD)"
        else:
            best_str = worst_str = "n/a"

        # R:R — use closed trade P&L vs stop distance (not available here, skip)
        # Use simplified win-avg / loss-avg as proxy
        win_vals  = [p for p in pnls if p > 0]
        loss_vals = [abs(p) for p in pnls if p <= 0]
        avg_win   = sum(win_vals)  / len(win_vals)  if win_vals  else 0.0
        avg_loss  = sum(loss_vals) / len(loss_vals) if loss_vals else 0.0
        avg_rr    = avg_win / avg_loss if avg_loss > 0 else 0.0

        # ── Daily breakdown section ───────────────────────────────────────────
        # Build a dict keyed by date for quick lookup
        pnl_by_date = {r[0]: (float(r[1]), int(r[2])) for r in daily_rows}
        daily_lines = []
        for i in range(7):
            d = start_date + timedelta(days=i)
            if d in pnl_by_date:
                p_usd, tc = pnl_by_date[d]
                dot = "🟢" if p_usd > 0 else ("🔴" if p_usd < 0 else "⚪")
                daily_lines.append(
                    f"  {dot} {d}: {p_usd:+.2f} USD ({tc} trades)"
                )
            else:
                daily_lines.append(f"  ⚪ {d}: no data")
        daily_section = "\n".join(daily_lines)

        # ── Layer rejection section ───────────────────────────────────────────
        total_rejects = sum(int(r[1]) for r in layer_rows)
        if layer_rows:
            layer_lines = [
                f"  {r[0]}: {int(r[1])} ({int(r[1])/total_rejects*100:.0f}%)"
                for r in layer_rows
            ]
            layer_section = "\n".join(layer_lines)
        else:
            layer_section = "  No rejections this week"

        # ── Regime distribution section ───────────────────────────────────────
        total_cycles = sum(int(r[1]) for r in regime_rows)
        if regime_rows:
            reg_lines = [
                f"  {r[0]}: {int(r[1])} ({int(r[1])/total_cycles*100:.0f}%)"
                for r in regime_rows
            ]
            reg_section = "\n".join(reg_lines)
        else:
            reg_section = "  No data"

        # ── Advisory notes ────────────────────────────────────────────────────
        notes = []
        layer_b_cnt = next(
            (int(r[1]) for r in layer_rows if r[0] == "Layer B"), 0
        )
        if total_rejects > 0 and layer_b_cnt / total_rejects > 0.60:
            notes.append(
                "⚠️ Layer B is rejecting most signals. VWAP confirmation "
                "may be too strict — review before going live."
            )
        if total_closed == 0:
            notes.append(
                "⚠️ Zero trades fired this week. Thresholds may be too "
                "conservative — review ADX and Layer B settings."
            )
        notes_section = ("\n\n<b>💡 Notes:</b>\n" + "\n".join(notes)) if notes else ""

        msg = (
            f"📈 <b>WEEKLY REPORT</b>\n"
            f"Period: {start_date} → {end_date}\n"
            f"Mode: {mode}\n"
            f"\n"
            f"<b>💰 Performance</b>\n"
            f"Total P&amp;L: {total_pnl:+.2f} USD\n"
            f"Total trades: {total_closed}\n"
            f"Win rate: {win_rate:.0f}% ({wins}W / {losses}L)\n"
            f"Avg R:R: {avg_rr:.2f}\n"
            f"Best day: {best_str}\n"
            f"Worst day: {worst_str}\n"
            f"\n"
            f"<b>📅 Daily breakdown:</b>\n"
            f"{daily_section}\n"
            f"\n"
            f"<b>🤖 Rejection analysis:</b>\n"
            f"{layer_section}\n"
            f"\n"
            f"<b>📡 Regime distribution (7 days):</b>\n"
            f"{reg_section}"
            f"{notes_section}"
        )
        await self.send(msg)


    # ── Telegram command polling ───────────────────────────────────────────────

    def start_polling(self, self_improvement) -> None:
        """
        Start a background daemon thread that polls the Telegram Bot API for
        incoming commands every ~5 seconds.

        Supported commands (from the configured TELEGRAM_CHAT_ID only):
          /apply  — apply pending config suggestions
          /skip   — skip pending suggestions
          /status — current regime, positions, P&L, feed health
          /help   — list available commands

        Security: messages from any other chat_id are silently ignored.
        """
        if not self.enabled:
            return

        token    = self._token
        chat_id  = str(self._chat_id)
        url_base = f"https://api.telegram.org/bot{token}"

        def _poll_loop() -> None:
            # BUG 1 FIX: skip all messages that arrived before the bot started.
            # Call getUpdates with offset=-1 to get only the latest update_id,
            # then advance past it so historical /apply commands are never replayed.
            offset = 0
            try:
                init_url = (
                    f"{url_base}/getUpdates"
                    f"?offset=-1&timeout=0&allowed_updates=%5B%22message%22%5D"
                )
                req = urllib.request.Request(init_url)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    init_data = json.loads(resp.read())
                if init_data.get("ok"):
                    results = init_data.get("result", [])
                    if results:
                        offset = results[-1]["update_id"] + 1
                        print(
                            f"[telegram] Polling starting at offset {offset} "
                            f"(skipped all prior messages).",
                            flush=True,
                        )
                    else:
                        print(
                            "[telegram] Polling starting at offset 0 (no prior messages).",
                            flush=True,
                        )
            except Exception as exc:
                print(
                    f"[telegram] Warning: could not initialise polling offset: {exc}",
                    file=sys.stderr,
                )

            while True:
                try:
                    url = (
                        f"{url_base}/getUpdates"
                        f"?offset={offset}&timeout=5&allowed_updates=%5B%22message%22%5D"
                    )
                    req = urllib.request.Request(url)
                    with urllib.request.urlopen(req, timeout=15) as resp:
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

                            # Security: reject messages from any other chat
                            if sender != chat_id:
                                continue

                            loop = self._loop
                            if loop is None or not loop.is_running():
                                continue

                            if msg_text == "/apply":
                                asyncio.run_coroutine_threadsafe(
                                    self_improvement.apply_suggestions(), loop
                                )
                            elif msg_text == "/skip":
                                asyncio.run_coroutine_threadsafe(
                                    self_improvement.skip_suggestions(), loop
                                )
                            elif msg_text == "/status":
                                asyncio.run_coroutine_threadsafe(
                                    self._send_status(), loop
                                )
                            elif msg_text == "/resume":
                                if self._order_manager is not None:
                                    asyncio.run_coroutine_threadsafe(
                                        self._handle_resume(), loop
                                    )
                                else:
                                    asyncio.run_coroutine_threadsafe(
                                        self.send("❌ /resume: order_manager not wired."),
                                        loop,
                                    )
                            elif msg_text.startswith("/reconciled"):
                                # Targeted per-trade reconciliation.
                                # Syntax: /reconciled <trade_id> <exit_price>
                                # Updates the specific trade with the confirmed exit
                                # price, recomputes P&L, and clears its pending flag.
                                # Does NOT auto-resume — use /resume after all trades
                                # are reconciled.
                                asyncio.run_coroutine_threadsafe(
                                    self._handle_reconciled(msg_text), loop
                                )
                            elif msg_text in ("/help", "/start"):
                                asyncio.run_coroutine_threadsafe(
                                    self.send(
                                        "📋 <b>Available commands:</b>\n"
                                        "/apply — apply pending config suggestions\n"
                                        "/skip — skip pending suggestions\n"
                                        "/status — current bot status\n"
                                        "/resume — resume trading (blocked if reconciliation pending)\n"
                                        "/reconciled &lt;trade_id&gt; &lt;exit_price&gt; — update exit price for a pending trade\n"
                                        "/help — show this message"
                                    ),
                                    loop,
                                )

                        except Exception as exc:
                            print(
                                f"[telegram] polling update processing error: {exc}",
                                file=sys.stderr,
                            )

                except Exception as exc:
                    print(
                        f"[telegram] polling loop error: {exc}", file=sys.stderr
                    )
                    time.sleep(5)

        t = threading.Thread(
            target=_poll_loop, daemon=True, name="telegram-poll"
        )
        t.start()
        print("[telegram] Command polling started (daemon thread).", flush=True)

    async def _handle_reconciled(self, msg_text: str) -> None:
        """
        Targeted per-trade reconciliation.

        Syntax: /reconciled <trade_id> <exit_price>

        Branches on trade state:
          Ghost trade (timestamp_close IS NULL):
            Full close finalization — sets timestamp_close, exit_price, pnl_usd,
            close_reason='manual_reconciled_ghost', reconciliation_pending=FALSE.
            Removes trade from PositionMonitor._positions if still tracked.
            Updates daily_pnl aggregate.

          Already-closed trade (e.g. price_recon_pending):
            Updates exit_price, pnl_usd, reconciliation_pending=FALSE.
            close_reason is NOT changed (preserved as historical record).
            Updates daily_pnl aggregate.

        Clears reconciliation_log rows for this trade.
        Does NOT auto-resume — use /resume after all trades are reconciled.
        """
        import os as _os
        from datetime import datetime, timezone, date as _date
        from sqlalchemy import create_engine as _ce, text as _t

        parts = msg_text.strip().split()
        if len(parts) != 3:
            await self.send(
                "⚠️ Usage: /reconciled &lt;trade_id&gt; &lt;exit_price&gt;\n"
                "Example: /reconciled 42 45678.50\n\n"
                "Updates exit price for a trade with reconciliation_pending = TRUE.\n"
                "Use /resume after all trades are reconciled."
            )
            return

        try:
            req_trade_id = int(parts[1])
            req_exit     = float(parts[2])
        except ValueError:
            await self.send("❌ /reconciled: trade_id must be an integer and exit_price a number.")
            return

        if req_exit <= 0:
            await self.send("❌ /reconciled: exit_price must be > 0.")
            return

        db_url = _os.environ.get("DATABASE_URL")
        if not db_url:
            await self.send("❌ /reconciled: DATABASE_URL not set.")
            return

        try:
            _eng = _ce(db_url)
            with _eng.begin() as _c:
                # Load all needed fields including timestamp_close (open vs closed branch)
                # and close_client_order_id so we can look up the actual OKX close time.
                row = _c.execute(_t(
                    "SELECT id, direction, entry_price, size_usd, "
                    "       reconciliation_pending, timestamp_close, paper, "
                    "       pair, close_client_order_id "
                    "FROM trades WHERE id = :tid"
                ), {"tid": req_trade_id}).fetchone()

                if not row:
                    await self.send(f"❌ /reconciled: trade_id {req_trade_id} not found.")
                    return
                if not row[4]:  # reconciliation_pending
                    await self.send(
                        f"ℹ️ trade_id {req_trade_id} has reconciliation_pending = FALSE. "
                        f"No action taken."
                    )
                    return

                direction           = row[1]
                entry_price         = float(row[2] or 0)
                size_usd            = float(row[3] or 0)
                timestamp_close     = row[5]   # None if ghost (trade still open in DB)
                paper               = bool(row[6])
                pair                = row[7]
                close_client_oid    = row[8]
                is_ghost            = timestamp_close is None

                if entry_price <= 0:
                    await self.send(f"❌ /reconciled: trade {req_trade_id} has no valid entry_price.")
                    return

                if direction == "LONG":
                    actual_pnl = (req_exit - entry_price) / entry_price * size_usd
                else:
                    actual_pnl = (entry_price - req_exit) / entry_price * size_usd

                now_utc = datetime.now(timezone.utc)

                # Try to fetch the actual OKX close timestamp so daily_pnl lands on
                # the correct UTC date (trade may have closed just before midnight).
                actual_close_dt: datetime | None = None
                if (
                    not config.PAPER_MODE
                    and self._order_manager is not None
                    and pair
                    and close_client_oid
                ):
                    try:
                        ccxt_sym = (
                            f"{pair.split('-')[0]}/{pair.split('-')[1]}"
                            f":{pair.split('-')[1]}"
                        )
                        closed = self._order_manager._exchange.fetch_closed_orders(
                            ccxt_sym, limit=20
                        )
                        for o in (closed or []):
                            o_info    = o.get("info", {}) or {}
                            id_match  = (
                                o.get("clientOrderId") == close_client_oid
                                or o_info.get("clOrdId") == close_client_oid
                            )
                            is_reduce = o.get("reduceOnly", False)
                            if id_match and is_reduce:
                                ts_ms = o.get("timestamp")
                                if ts_ms:
                                    actual_close_dt = datetime.fromtimestamp(
                                        int(ts_ms) / 1000, tz=timezone.utc
                                    )
                                break
                    except Exception as _fetch_exc:
                        # Non-fatal — fall back to now() for timestamp; operator
                        # has provided the exit price manually already.
                        print(
                            f"[telegram] /reconciled: OKX timestamp fetch failed: {_fetch_exc}",
                            file=sys.stderr,
                        )

                # Fallback order for timestamp attribution:
                #   1. actual OKX close timestamp (most accurate)
                #   2. existing timestamp_close from DB row (already-closed trades only —
                #      price_recon_pending trades have the correct close day recorded)
                #   3. now_utc (last resort — ghost trades with no DB or OKX timestamp)
                db_close_dt: datetime | None = None
                if not is_ghost and timestamp_close is not None:
                    # timestamp_close may be a naive or aware datetime from SQLAlchemy
                    db_close_dt = (
                        timestamp_close.replace(tzinfo=timezone.utc)
                        if timestamp_close.tzinfo is None
                        else timestamp_close
                    )
                close_dt   = actual_close_dt or db_close_dt or now_utc
                close_date = close_dt.date()

                if is_ghost:
                    # Ghost: trade is still "open" in DB — perform full close finalization.
                    # Set timestamp_close so it exits the open-trades queries.
                    _c.execute(_t(
                        "UPDATE trades "
                        "SET exit_price = :ep, pnl_usd = :pnl, "
                        "    timestamp_close = :tc, close_reason = 'manual_reconciled_ghost', "
                        "    reconciliation_pending = FALSE "
                        "WHERE id = :tid AND reconciliation_pending = TRUE"
                    ), {"ep": req_exit, "pnl": actual_pnl, "tc": close_dt, "tid": req_trade_id})
                else:
                    # Already closed (e.g. price_recon_pending): update price/P&L only.
                    # Preserve existing close_reason as historical record.
                    _c.execute(_t(
                        "UPDATE trades "
                        "SET exit_price = :ep, pnl_usd = :pnl, reconciliation_pending = FALSE "
                        "WHERE id = :tid AND reconciliation_pending = TRUE"
                    ), {"ep": req_exit, "pnl": actual_pnl, "tid": req_trade_id})

                # Update daily_pnl aggregate using actual close date, not now().
                # Prevents mis-attribution when reconciliation runs after UTC midnight.
                _c.execute(_t(
                    """
                    INSERT INTO daily_pnl (date, pnl_usd, trade_count, paper)
                    VALUES (:d, :pnl, 1, :paper)
                    ON CONFLICT (date, paper)
                    DO UPDATE SET
                        pnl_usd     = daily_pnl.pnl_usd     + EXCLUDED.pnl_usd,
                        trade_count = daily_pnl.trade_count  + 1
                    """
                ), {"d": close_date, "pnl": actual_pnl, "paper": paper})

                # Clear matching reconciliation_log records for this trade
                _c.execute(_t(
                    "UPDATE reconciliation_log "
                    "SET status = 'reconciled' "
                    "WHERE trade_id = :tid "
                    "  AND status IN ("
                    "    'price_recon_pending', 'ghost_pending',"
                    "    'fill_confirmed_pending_log', 'emergency_close_required'"
                    "  )"
                ), {"tid": req_trade_id})

                # Count remaining pending trades
                remaining = _c.execute(_t(
                    "SELECT COUNT(*) FROM trades WHERE reconciliation_pending = TRUE"
                )).fetchone()
                still_pending = int(remaining[0]) if remaining else 0

            # Remove from in-memory position tracking if it is a ghost trade still present
            if is_ghost and self._position_monitor is not None:
                if req_trade_id in self._position_monitor._positions:
                    del self._position_monitor._positions[req_trade_id]
                    print(
                        f"[telegram] /reconciled: removed ghost trade_id={req_trade_id} "
                        f"from PositionMonitor._positions",
                        flush=True,
                    )

            # Clear in-memory reconciliation flag if no pending trades remain
            if still_pending == 0 and self._position_monitor is not None:
                self._position_monitor._reconciliation_pending = False

            trade_type  = "ghost trade" if is_ghost else "trade"
            status_note = (
                f"\n{still_pending} other trade(s) still pending reconciliation."
                if still_pending > 0
                else "\nNo other trades pending — you can now /resume."
            )
            await self.send(
                f"✅ <b>{trade_type.capitalize()} {req_trade_id} reconciled</b>\n"
                f"Exit price: {req_exit:,.4f} | P&L: {actual_pnl:+.4f} USD"
                f"{status_note}"
            )

        except Exception as exc:
            await self.send(f"❌ /reconciled failed: {exc}")

    async def _handle_resume(self) -> None:
        """
        Pre-flight checks before clearing _trading_halted via /resume.

        Blocks resume if:
          1. Unresolved reconciliation_log records exist (fill_confirmed_pending_log
             or emergency_close_required status).
          2. Any trades row has reconciliation_pending = TRUE (ghost exit unrecoverable).
          3. PositionMonitor._reconciliation_pending is True (in-memory flag).

        Clears _trading_halted only when all checks pass.
        Note: _db_load_failed is intentionally NOT cleared here — only a successful
        reconcile_with_exchange() on next restart should clear it.
        """
        import os as _os
        from sqlalchemy import create_engine as _ce, text as _t

        # ── Check 1: unresolved reconciliation_log records ────────────────────
        db_url = _os.environ.get("DATABASE_URL")
        if db_url:
            try:
                _eng = _ce(db_url)
                with _eng.connect() as _c:
                    row = _c.execute(_t(
                        "SELECT COUNT(*) FROM reconciliation_log "
                        "WHERE status IN ("
                        "  'fill_confirmed_pending_log',"
                        "  'emergency_close_required',"
                        "  'price_recon_pending',"
                        "  'ghost_pending'"
                        ")"
                    )).fetchone()
                    unresolved_count = int(row[0]) if row else 0
                if unresolved_count > 0:
                    await self.send(
                        f"⚠️ <b>Cannot resume</b> — {unresolved_count} unresolved "
                        f"reconciliation record(s) in DB.\n"
                        f"Send /reconciled after manually verifying fills on OKX."
                    )
                    return
            except Exception as exc:
                await self.send(f"⚠️ /resume: reconciliation_log check failed: {exc}")
                return

        # ── Check 2: trades with unrecovered exit prices ──────────────────────
        if db_url:
            try:
                _eng2 = _ce(db_url)
                with _eng2.connect() as _c2:
                    row2 = _c2.execute(_t(
                        "SELECT COUNT(*) FROM trades WHERE reconciliation_pending = TRUE"
                    )).fetchone()
                    recon_trades = int(row2[0]) if row2 else 0
                if recon_trades > 0:
                    await self.send(
                        f"⚠️ <b>Cannot resume</b> — {recon_trades} trade(s) have\n"
                        f"unrecovered exit prices (reconciliation_pending).\n"
                        f"Verify exit prices on OKX, then send /reconciled."
                    )
                    return
            except Exception as exc:
                await self.send(
                    f"⚠️ /resume: trades reconciliation_pending check failed: {exc}"
                )
                return

        # ── Check 3: ghost position pending reconciliation (in-memory flag) ──
        if getattr(self._position_monitor, "_reconciliation_pending", False):
            await self.send(
                "⚠️ <b>Cannot resume</b> — exit price reconciliation pending.\n"
                "A ghost position's exit price could not be recovered from OKX.\n"
                "Send /reconciled after manually verifying the exit price."
            )
            return

        # ── All clear ─────────────────────────────────────────────────────────
        self._order_manager.resume_trading()
        await self.send(
            "✅ <b>All reconciliation checks passed. Trading resumed.</b>"
        )

    async def _send_status(self) -> None:
        """
        Send a rich snapshot of current bot state in response to /status.

        Sections: account balance, P&L, performance stats, open positions
        with unrealised P&L, current regime + ADX, feed health, countdown
        to next decision, and uptime.
        """
        if not self.enabled:
            return

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            await self.send("Status unavailable — DATABASE_URL not configured.")
            return

        now_utc = datetime.now(timezone.utc)
        myt_now = now_utc + timedelta(hours=8)
        timestamp_str = myt_now.strftime("%Y-%m-%d %H:%M MYT")
        mode = "PAPER" if config.PAPER_MODE else "LIVE"
        p = {"paper": config.PAPER_MODE}

        try:
            engine = self._db_engine()
            with engine.begin() as conn:

                # 1. Total P&L (all closed days) — used for balance
                total_pnl_row = conn.execute(text(
                    "SELECT COALESCE(SUM(pnl_usd), 0) FROM daily_pnl WHERE paper = :paper"
                ), p).fetchone()
                total_pnl = float(total_pnl_row[0]) if total_pnl_row else 0.0

                # 2. Today P&L — use explicit UTC date to match kill-switch accounting
                today_pnl_row = conn.execute(text(
                    "SELECT COALESCE(SUM(pnl_usd), 0) FROM daily_pnl "
                    "WHERE date = :today AND paper = :paper"
                ), {"today": now_utc.date(), **p}).fetchone()
                today_pnl = float(today_pnl_row[0]) if today_pnl_row else 0.0

                # 3. Trade stats — closed, excluding cleanup rows
                stats_row = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) AS total, "
                    "  COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins, "
                    "  COALESCE(AVG(CASE "
                    "    WHEN entry_price > 0 AND ABS(entry_price - stop_loss) > 0 "
                    "    THEN ABS(take_profit - entry_price) / ABS(entry_price - stop_loss) "
                    "    ELSE NULL END), 0.0) AS avg_rr "
                    "FROM trades "
                    "WHERE paper = :paper "
                    "  AND timestamp_close IS NOT NULL "
                    "  AND (close_reason IS NULL OR close_reason NOT IN "
                    "       ('test_cleanup', 'manual_cleanup'))"
                ), p).fetchone()
                total_trades = int(stats_row[0]) if stats_row else 0
                wins         = int(stats_row[1]) if stats_row else 0
                losses       = total_trades - wins
                avg_rr       = float(stats_row[2]) if stats_row and stats_row[2] else 0.0
                win_rate     = (wins / total_trades * 100) if total_trades > 0 else 0.0

                # 4. Open positions (with stop/TP for display)
                open_rows = conn.execute(text(
                    "SELECT pair, direction, entry_price, stop_loss, take_profit, size_usd "
                    "FROM trades "
                    "WHERE timestamp_close IS NULL AND paper = :paper "
                    "ORDER BY timestamp_open DESC"
                ), p).fetchall()

                # 5. Latest regime + ADX per SWAP pair
                regime_rows = conn.execute(text(
                    "SELECT pair, regime, adx FROM ticks "
                    "WHERE id IN (SELECT MAX(id) FROM ticks GROUP BY pair) "
                    "  AND pair LIKE '%-SWAP' "
                    "ORDER BY pair"
                )).fetchall()

                # 6. Feed health from DB (fallback when live monitor unavailable)
                health_rows = conn.execute(text(
                    "SELECT feed_name, state FROM feed_health_log "
                    "WHERE id IN (SELECT MAX(id) FROM feed_health_log GROUP BY feed_name)"
                )).fetchall()

        except Exception as exc:
            await self.send(f"❌ /status query error: {exc}")
            return

        balance = config.STARTING_CAPITAL_USD + total_pnl

        # ── Open positions section ────────────────────────────────────────────
        ws = self._ws_manager
        if open_rows:
            pos_lines = []
            for row in open_rows:
                pair, direction, entry, stop, tp, size_usd = row
                entry_f = float(entry)    if entry    else 0.0
                stop_f  = float(stop)     if stop     else 0.0
                tp_f    = float(tp)       if tp       else 0.0
                size_f  = float(size_usd) if size_usd else 0.0

                unrealised_str = "N/A"
                if ws is not None and entry_f > 0:
                    try:
                        ticker = ws.get_latest_ticker(pair)
                        if ticker:
                            mark = ticker.get("mark_price")
                            if mark:
                                contracts = size_f / entry_f
                                if direction == "LONG":
                                    unrealised = (mark - entry_f) * contracts
                                else:
                                    unrealised = (entry_f - mark) * contracts
                                unrealised_str = f"{unrealised:+.4f} USD"
                    except Exception:
                        pass

                pos_lines.append(
                    f"  {direction} {pair} @ {entry_f:.2f}\n"
                    f"  Unrealised: {unrealised_str}\n"
                    f"  Stop: {stop_f:.2f} | TP: {tp_f:.2f}"
                )
            pos_section = "\n".join(pos_lines)
        else:
            pos_section = "  None"

        # ── Regime section ────────────────────────────────────────────────────
        if regime_rows:
            reg_lines = []
            for row in regime_rows:
                pair, regime, adx = row
                adx_str = f"{float(adx):.1f}" if adx is not None else "N/A"
                reg_lines.append(f"  {pair}: {regime or 'N/A'} (ADX {adx_str})")
            reg_section = "\n".join(reg_lines)
        else:
            reg_section = "  No data"

        # ── Feed health section ───────────────────────────────────────────────
        # Use live monitor state when available; fall back to DB snapshot
        mon = self._monitor
        if mon is not None:
            try:
                degraded = mon.get_degraded_feeds()
                if degraded:
                    health_section = "\n".join(f"  ❌ {f}" for f in sorted(degraded))
                else:
                    health_section = "  ✅ All feeds healthy"
            except Exception:
                health_section = "  ⚠️ Health check unavailable"
        else:
            feed_map = {r[0]: r[1] for r in health_rows}
            core     = ["kline", "ticker", "markprice", "orderbook"]
            degraded = [f for f in core if feed_map.get(f) != "healthy"]
            health_section = (
                "\n".join(f"  ❌ {f}" for f in degraded)
                if degraded else "  ✅ All feeds healthy"
            )

        # ── Countdown to next 15m bar close ──────────────────────────────────
        next_qh_min = ((now_utc.minute // 15) + 1) * 15
        if next_qh_min >= 60:
            next_bar = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            next_bar = now_utc.replace(minute=next_qh_min, second=0, microsecond=0)
        delta_s = int((next_bar - now_utc).total_seconds())
        countdown_str = f"{delta_s // 60}m {delta_s % 60}s"

        # ── Uptime ────────────────────────────────────────────────────────────
        if self._bot_start_time:
            elapsed = int((now_utc - self._bot_start_time).total_seconds())
            uptime_str = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"
        else:
            uptime_str = "N/A"

        msg = (
            f"🤖 <b>BOT STATUS</b>\n"
            f"{timestamp_str} | Mode: {mode}\n"
            f"\n"
            f"<b>💰 Account</b>\n"
            f"Balance: ${balance:.2f}\n"
            f"Today P&amp;L: {today_pnl:+.2f} USD\n"
            f"Total P&amp;L: {total_pnl:+.2f} USD\n"
            f"\n"
            f"<b>📊 Performance</b>\n"
            f"Total trades: {total_trades}\n"
            f"Win rate: {win_rate:.0f}% ({wins}W / {losses}L)\n"
            f"Avg R:R: {avg_rr:.2f}\n"
            f"\n"
            f"<b>📈 Open positions: {len(open_rows)}</b>\n"
            f"{pos_section}\n"
            f"\n"
            f"<b>🧠 Current regime</b>\n"
            f"{reg_section}\n"
            f"\n"
            f"<b>📡 Feed health</b>\n"
            f"{health_section}\n"
            f"\n"
            f"⏱ Next decision in {countdown_str}\n"
            f"Uptime: {uptime_str}"
        )
        await self.send(msg)

    async def notify_startup(self) -> None:
        """
        Send a BOT STARTED or BOT RESTARTED notification.

        Called ~30s after startup (from main.py) once feeds have had time to
        initialise. Detects restarts by checking for recent ticks in the DB
        (gap < 10 minutes = previous session still warm = crash/restart).
        """
        if not self.enabled:
            return

        now_utc = datetime.now(timezone.utc)
        myt_now = now_utc + timedelta(hours=8)
        timestamp_str = myt_now.strftime("%Y-%m-%d %H:%M MYT")
        mode = "PAPER" if config.PAPER_MODE else "LIVE"

        # Check live feed health
        mon = self._monitor
        all_healthy = False
        health_note = ""
        if mon is not None:
            try:
                all_healthy = mon.are_core_feeds_healthy()
                if not all_healthy:
                    degraded = mon.get_degraded_feeds()
                    if degraded:
                        health_note = f"\nStill initialising: {', '.join(sorted(degraded))}"
            except Exception:
                pass

        # Gap detection — recent ticks = crash/restart rather than fresh start
        is_restart = False
        gap_str = ""
        database_url = os.environ.get("DATABASE_URL", "")
        if database_url:
            try:
                engine = self._db_engine()
                with engine.begin() as conn:
                    row = conn.execute(text(
                        "SELECT MAX(timestamp) FROM ticks"
                    )).fetchone()
                    if row and row[0]:
                        last_tick = row[0]
                        if last_tick.tzinfo is None:
                            last_tick = last_tick.replace(tzinfo=timezone.utc)
                        gap = now_utc - last_tick
                        if gap.total_seconds() < 600:   # < 10 min → recent session
                            is_restart = True
                            total_secs = int(gap.total_seconds())
                            gap_mins = total_secs // 60
                            gap_secs = total_secs % 60
                            gap_str = f"{gap_mins}m {gap_secs}s" if gap_mins > 0 else f"{gap_secs}s"
            except Exception:
                pass

        feed_status = (
            "All feeds healthy."
            if all_healthy
            else f"Feeds initialising.{health_note}"
        )

        if is_restart:
            msg = (
                f"🔄 <b>BOT RESTARTED</b>\n"
                f"{timestamp_str} | Mode: {mode}\n"
                f"Previous session ended ~{gap_str} ago.\n"
                f"{feed_status} Resuming.\n"
                f"Type /status for full details."
            )
        else:
            msg = (
                f"🟢 <b>BOT STARTED</b>\n"
                f"{timestamp_str} | Mode: {mode}\n"
                f"{feed_status} Bot is running.\n"
                f"Type /status for full details."
            )

        await self.send(msg)


# Module-level singleton — import this directly in all modules
notifier = TelegramNotifier()
