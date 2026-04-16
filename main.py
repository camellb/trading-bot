# load_dotenv() MUST run before any other import that reads os.getenv() at
# module level (e.g. feeds/telegram_notifier.py creates its singleton on import).
from dotenv import load_dotenv
load_dotenv(override=True)

import asyncio
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from db.models import create_all_tables
from feeds.feed_health_monitor import monitor
from feeds.okx_ws import OKXWebSocketManager
from feeds.book_manager import BookManager
from feeds.news_feed import NewsFeed
from feeds.macro_calendar import MacroCalendar
from feeds.telegram_notifier import notifier
from execution.order_manager import OrderManager
from execution.position_monitor import PositionMonitor
from engine.self_improvement import SelfImprovementAnalyser
from engine.macro_context import MacroContextEngine
from engine.portfolio_advisor import PortfolioAdvisor
from feeds.deribit_feed import DeribitFeed
from engine.memory import MemoryManager
from engine.scanner import Scanner
from engine.strategist import Strategist
from bot_api import BotAPI
import config


async def main():
    bot_start_time = datetime.now(timezone.utc)

    print("Trading bot starting...")
    print(f"PAPER_MODE: {config.PAPER_MODE}")
    print(f"EXCHANGE: {config.EXCHANGE}")
    print(f"TRADING_PAIRS: {config.TRADING_PAIRS}")

    create_all_tables()
    print("Database tables verified.")

    # ── Feeds ─────────────────────────────────────────────────────────────────
    ws_manager = OKXWebSocketManager(monitor)
    book_manager = BookManager(ws_manager)
    news_feed = NewsFeed(monitor)
    macro_calendar = MacroCalendar(monitor)

    # ── Feeds (overlay) ───────────────────────────────────────────────────────
    deribit_feed = DeribitFeed(monitor)

    # ── Execution ─────────────────────────────────────────────────────────────
    order_manager = OrderManager(monitor)

    position_monitor = PositionMonitor(
        order_manager, ws_manager, None, monitor, None
    )

    # ── Macro context engine ──────────────────────────────────────────────────
    macro_context = MacroContextEngine(news_feed, macro_calendar, notifier)
    macro_context.set_ws_manager(ws_manager)

    # ── Startup ───────────────────────────────────────────────────────────────
    await macro_calendar.start()

    # ── Memory + Strategist + Scanner ────────────────────────────────────────
    memory     = MemoryManager()
    strategist = Strategist(
        order_manager    = order_manager,
        memory           = memory,
        notifier         = notifier,
        macro_context    = macro_context,
        position_monitor = position_monitor,
        health_monitor   = monitor,
    )
    scanner = Scanner(
        ws_manager     = ws_manager,
        news_feed      = news_feed,
        macro_calendar = macro_calendar,
        deribit_feed   = deribit_feed,
        health_monitor = monitor,
        memory         = memory,
        strategist     = strategist,
    )
    scanner.macro_context = macro_context

    # Wire Strategist ↔ PositionMonitor (enables thesis-driven exits and Obsidian
    # post-mortems for mechanical closes).  Must happen after both are constructed.
    # scanner.position_monitor enables large-loss and critical-news urgent reviews.
    position_monitor.set_strategist(strategist)
    scanner.position_monitor = position_monitor

    # ── Self-improvement analyser + portfolio advisor ─────────────────────────
    self_improvement = SelfImprovementAnalyser(notifier)
    self_improvement.set_strategist(strategist)   # closes the learning loop
    portfolio_advisor = PortfolioAdvisor(notifier)

    # Pass the running event loop to the notifier so the polling thread can
    # schedule coroutines back onto it via run_coroutine_threadsafe.
    notifier._loop = asyncio.get_running_loop()

    # Wire live references into the notifier for rich /status and startup notice
    notifier._bot_start_time  = bot_start_time
    notifier._monitor         = monitor
    notifier._ws_manager      = ws_manager
    notifier._macro_calendar  = macro_calendar
    notifier._order_manager      = order_manager      # enables /resume command
    notifier._position_monitor  = position_monitor   # enables reconciliation checks in /resume
    order_manager._notifier      = notifier           # enables CRITICAL alerts from OrderManager
    order_manager._ws_manager    = ws_manager         # enables mark-price fallback on paper close

    notifier.start_polling(self_improvement)

    # ── Pre-flight checks (live mode only) ───────────────────────────────────
    if not config.PAPER_MODE:
        ok = await order_manager.verify_and_set_leverage()
        if not ok:
            raise RuntimeError(
                "Leverage/margin verification failed. "
                "Check OKX account settings before trading live."
            )

    # Check for unresolved fill records from previous sessions.
    # Must run after notifier is wired so alerts can be sent.
    await order_manager.check_reconciliation_log()

    # ── Telegram summary + self-improvement + macro context scheduler ─────────
    summary_scheduler = AsyncIOScheduler()
    summary_scheduler.add_job(
        notifier.send_daily_summary,
        CronTrigger(hour=8, minute=30, timezone="Asia/Kuala_Lumpur"),
        id="daily_summary",
    )
    summary_scheduler.add_job(
        notifier.send_weekly_summary,
        CronTrigger(day_of_week="sun", hour=8, minute=30, timezone="Asia/Kuala_Lumpur"),
        id="weekly_summary",
    )
    summary_scheduler.add_job(
        self_improvement.analyse_and_report,
        CronTrigger(day_of_week="sun", hour=8, minute=30, timezone="Asia/Kuala_Lumpur"),
        id="self_improvement",
    )
    # Daily macro brief — 00:30 UTC = 08:30 MYT
    summary_scheduler.add_job(
        macro_context.generate_daily_brief,
        CronTrigger(hour=0, minute=30, timezone="UTC"),
        id="macro_daily_brief",
    )
    # Portfolio advisory — Monday 08:30 MYT (Mon 00:30 UTC), after weekly summary
    async def _weekly_with_advisory() -> None:
        await notifier.send_weekly_summary()
        await portfolio_advisor.check_and_advise(order_manager)

    summary_scheduler.add_job(
        _weekly_with_advisory,
        CronTrigger(day_of_week="mon", hour=0, minute=30, timezone="UTC"),
        id="weekly_with_advisory",
    )
    # Monthly performance report — 1st of each month 00:30 UTC
    summary_scheduler.add_job(
        self_improvement.generate_monthly_report,
        CronTrigger(day=1, hour=0, minute=30, timezone="UTC"),
        id="monthly_report",
    )
    # One-time startup brief — fires 45s after launch so feeds are healthy first
    summary_scheduler.add_job(
        macro_context.generate_daily_brief,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=45)),
        id="macro_startup_brief",
    )
    summary_scheduler.start()
    print(
        "[main] Scheduled: daily summary 08:30 MYT, "
        "weekly summary Sun 08:30 MYT, "
        "self-improvement review Sun 08:30 MYT, "
        "macro brief 00:30 UTC daily (startup brief in 45s), "
        "portfolio advisory Mon 08:30 MYT, "
        "monthly report 1st of month 00:30 UTC",
        flush=True,
    )

    # ── Run ───────────────────────────────────────────────────────────────────
    async def _startup_notification() -> None:
        """Wait 30s for feeds to initialise, then send BOT STARTED/RESTARTED."""
        await asyncio.sleep(30)
        await notifier.notify_startup()

    async def _deribit_polling() -> None:
        """
        Poll Deribit DVOL every DERIBIT_IV_CACHE_SECONDS seconds.
        Forces cache expiry before each fetch so _get_history() always refetches.
        """
        while True:
            for ccy in ("BTC", "ETH"):
                # Expire cache so _get_history() triggers a real fetch
                deribit_feed._cache.pop(ccy, None)
                await deribit_feed._get_history(ccy)
            await asyncio.sleep(config.DERIBIT_IV_CACHE_SECONDS)

    # ── Dashboard HTTP API (localhost only, X-Bot-Secret auth) ───────────────
    bot_api = BotAPI(
        scanner          = scanner,
        order_manager    = order_manager,
        position_monitor = position_monitor,
        notifier         = notifier,
        strategist       = strategist,
        ws_manager       = ws_manager,
    )
    # Wire onto notifier so /confirm-config and /reject-config can reach it.
    notifier._bot_api = bot_api

    await asyncio.gather(
        ws_manager.start(config.TRADING_PAIRS),
        news_feed.start(),
        position_monitor.start(),
        scanner.start(),
        _startup_notification(),
        _deribit_polling(),
        bot_api.start(),
    )


if __name__ == "__main__":
    asyncio.run(main())
