"""
Entry point for the Polymarket prediction-market bot.

Architecture at a glance:

    PolymarketFeed   — Gamma API client, pulls candidate markets
            │
            ▼
    PMAnalyst        — skips stale markets, fetches research, asks Claude
            │           for a calibrated probability, runs the risk manager,
            │           then sizes via positive-EV with flat stakes.
            │
            ▼
    PMExecutor       — opens simulation (or live) positions, settles them
                        after resolution, feeds the calibration ledger,
                        and triggers the learning cadence every 50 trades.

Scheduled jobs (APScheduler):
    PM scan          — every PM_SCAN_INTERVAL_MINUTES
    PM resolve       — every PM_RESOLVE_INTERVAL_MINUTES (fast path: PM_RESOLVE_FAST_INTERVAL_SECONDS)
    Daily summary    — 08:30 MYT
    Weekly summary   — Monday 08:30 MYT

Learning cadence is trade-volume-gated (not calendar-gated) and runs in
the settle path — see engine.learning_cadence.

Side services:
    bot_api.BotAPI  — localhost HTTP API for the dashboard
    TelegramNotifier poll thread for /status /scan /resolve /confirm-config

Fatal-at-import-time: .env must provide DATABASE_URL, ANTHROPIC_API_KEY,
BOT_API_SECRET. Telegram credentials are per-user (user_config table),
configured from the dashboard — no process-global env vars required.
"""

# load_dotenv() must run before any module that reads os.getenv() at import.
from dotenv import load_dotenv
load_dotenv(override=True)

import asyncio
import faulthandler
import signal
import sys
import traceback

# Dump all thread stacks on SIGUSR1 — invaluable for diagnosing hangs.
faulthandler.enable()
faulthandler.register(signal.SIGUSR1)
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron     import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from db.models                import create_all_tables
from feeds.feed_health_monitor import monitor
from feeds.news_feed           import NewsFeed
from feeds.macro_calendar      import MacroCalendar
from feeds.telegram_notifier   import notifier
from feeds                     import telegram_messages as tm
from engine.pm_analyst         import PMAnalyst
from engine.user_config        import ensure_default_user_config
from bot_api                   import BotAPI
from polymarket_runner         import scan_and_analyze, resolve_positions
from engine.markout_tracker    import check_markouts
from process_health            import health as proc_health


async def main() -> None:
    # Increase the default thread pool — the default (5 workers) gets
    # saturated by research/Claude/feedparser calls during scans, blocking
    # the event loop from processing API requests.
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=20,
                                                  thread_name_prefix="bot"))

    bot_start_time = datetime.now(timezone.utc)
    monitor.set_bot_start_time(bot_start_time)
    proc_health.set_start_time(bot_start_time)

    print("Polymarket bot starting…", flush=True)
    print(f"PM_MODE: {config.PM_MODE}", flush=True)
    print(
        f"Starting cash: "
        f"simulation=${config.PM_SIMULATION_STARTING_CASH:.0f}, "
        f"live=${config.PM_LIVE_STARTING_CASH:.0f}",
        flush=True,
    )

    create_all_tables()
    print("Database tables verified.", flush=True)

    ensure_default_user_config()
    print("Default user_config row ensured.", flush=True)

    # ── Overlay feeds (advisory only — do not gate trading) ──────────────────
    news_feed      = NewsFeed(monitor)
    macro_calendar = MacroCalendar(monitor)
    await macro_calendar.start()

    # Multi-tenant from day one: the analyst holds no per-user state. Each
    # market is evaluated once (shared Claude cost) and fanned out to every
    # onboarded user via PMExecutor(user_id) in scan_and_analyze.
    analyst = PMAnalyst(notifier=notifier, news_feed=news_feed)

    # ── Wire notifier references used by /status and scheduled summaries ─────
    notifier._loop            = asyncio.get_running_loop()
    notifier._bot_start_time  = bot_start_time
    notifier._monitor         = monitor
    notifier._analyst         = analyst

    # Telegram polling threads for commands (/status /pause /resume /help etc.).
    # One thread per user with configured credentials; no creds → no poller.
    notifier.start_polling_for_all()

    # ── HTTP API (dashboard) ────────────────────────────────────────────────
    bot_api = BotAPI(analyst=analyst, notifier=notifier)
    notifier._bot_api = bot_api

    # ── Scheduler ───────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler()

    # PM scan — cadence from config.
    scan_interval_min = int(getattr(config, "PM_SCAN_INTERVAL_MINUTES", 60))

    async def _run_scan():
        try:
            await scan_and_analyze(
                limit          = int(getattr(config, "PM_SCAN_LIMIT", 20)),
                min_volume_24h = float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
                notifier       = notifier,
                analyst        = analyst,
            )
            proc_health.record_job_ok("pm_scan")
        except Exception as exc:
            proc_health.record_job_error("pm_scan")
            print(f"[main] scan failed: {exc}", file=sys.stderr)

    scheduler.add_job(
        _run_scan,
        IntervalTrigger(minutes=scan_interval_min),
        id="pm_scan",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
        max_instances=1,
        coalesce=True,
    )

    # PM resolver — checks for settled markets and finalises P&L.
    # Resolution checks are free Polymarket REST reads, so we poll aggressively.
    resolve_interval_min = int(getattr(config, "PM_RESOLVE_INTERVAL_MINUTES", 15))

    async def _run_resolve():
        try:
            await resolve_positions(notifier=notifier)
            proc_health.record_job_ok("pm_resolve")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve")
            print(f"[main] resolve failed: {exc}", file=sys.stderr)

    scheduler.add_job(
        _run_resolve,
        IntervalTrigger(minutes=resolve_interval_min),
        id="pm_resolve",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
        max_instances=1,
        coalesce=True,
    )

    # Near-live resolver for short-horizon markets (< 24h to end).
    fast_resolve_sec = int(getattr(config, "PM_RESOLVE_FAST_INTERVAL_SECONDS", 60))

    async def _run_resolve_fast():
        try:
            await resolve_positions(short_horizon_only=True, notifier=notifier)
            proc_health.record_job_ok("pm_resolve_fast")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve_fast")
            print(f"[main] fast resolve failed: {exc}", file=sys.stderr)

    scheduler.add_job(
        _run_resolve_fast,
        IntervalTrigger(seconds=fast_resolve_sec),
        id="pm_resolve_fast",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )

    # Markout tracker — check T+1h / T+6h / T+24h price moves.
    async def _run_markouts():
        try:
            await check_markouts()
            proc_health.record_job_ok("markout_check")
        except Exception as exc:
            proc_health.record_job_error("markout_check")
            print(f"[main] markout check failed: {exc}", file=sys.stderr)

    scheduler.add_job(
        _run_markouts,
        IntervalTrigger(hours=1),
        id="markout_check",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=7),
        max_instances=1,
        coalesce=True,
    )

    # Telegram summaries (MYT = UTC+8).
    scheduler.add_job(
        notifier.send_daily_summary,
        CronTrigger(hour=8, minute=30, timezone="Asia/Kuala_Lumpur"),
        id="daily_summary",
    )
    scheduler.add_job(
        notifier.send_weekly_summary,
        CronTrigger(day_of_week="mon", hour=8, minute=30,
                    timezone="Asia/Kuala_Lumpur"),
        id="weekly_summary",
    )
    # Learning cadence is trade-volume-gated, not calendar-gated — see
    # engine.learning_cadence.maybe_run_learning_cycle (hooked into settlement).

    # ── Watchdog — alert if core jobs stop running ────────────────────────
    _watchdog_alerted: set[str] = set()
    _WATCHDOG_THRESHOLDS = {
        "pm_scan":         timedelta(minutes=scan_interval_min * 3),
        "pm_resolve":      timedelta(minutes=resolve_interval_min * 3),
        "pm_resolve_fast": timedelta(seconds=fast_resolve_sec * 5),
    }

    async def _run_watchdog():
        if proc_health.uptime_seconds < 300:
            return
        now = datetime.now(timezone.utc)
        for job_name, max_gap in _WATCHDOG_THRESHOLDS.items():
            last_ok = proc_health.last_ok(job_name)
            if last_ok is None:
                continue
            gap = now - last_ok
            if gap > max_gap and job_name not in _watchdog_alerted:
                _watchdog_alerted.add(job_name)
                mins = int(gap.total_seconds() / 60)
                await notifier.broadcast_error(
                    "watchdog",
                    f"Job '{job_name}' has not completed in {mins}min "
                    f"(threshold: {int(max_gap.total_seconds()/60)}min)"
                )
            elif gap <= max_gap and job_name in _watchdog_alerted:
                _watchdog_alerted.discard(job_name)

    scheduler.add_job(
        _run_watchdog,
        IntervalTrigger(minutes=10),
        id="watchdog",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    print(
        f"[main] Scheduler started: "
        f"PM scan every {scan_interval_min}min, "
        f"resolver every {resolve_interval_min}min (fast every {fast_resolve_sec}s), "
        f"markouts every 1h, watchdog every 10min, "
        f"daily 08:30 MYT, weekly Mon 08:30 MYT, "
        f"self-improve Sun 08:30 MYT, monthly 1st 08:30 MYT.",
        flush=True,
    )

    # ── Signal handling ────────────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    # Ignore SIGHUP — we're a launchd daemon, not a terminal app.
    # Without this, closing Claude Code (or any controlling terminal)
    # sends SIGHUP which kills the bot unnecessarily.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def _handle_signal(sig, _frame):
        sig_name = signal.Signals(sig).name
        print(f"[main] Received {sig_name} — shutting down gracefully", flush=True)
        notifier.broadcast_restart_sync(tm.restart_planned())
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig, None)

    # ── Tasks ───────────────────────────────────────────────────────────────
    async def _startup_notification() -> None:
        await asyncio.sleep(20)
        await notifier.broadcast_startup()

    async def _shutdown_waiter() -> None:
        await shutdown_event.wait()
        print("[main] Shutdown event received — stopping scheduler", flush=True)
        scheduler.shutdown(wait=False)
        if bot_api._runner:
            await bot_api._runner.cleanup()
        raise SystemExit(0)

    # Liveness heartbeat consumed by the external watchdog (watchdog.sh
    # restarts the bot if logs/.heartbeat mtime is older than 360s).
    async def _heartbeat_writer() -> None:
        from pathlib import Path
        path = Path("logs/.heartbeat")
        path.parent.mkdir(parents=True, exist_ok=True)
        while not shutdown_event.is_set():
            try:
                path.write_text(datetime.now(timezone.utc).isoformat())
            except Exception as exc:
                print(f"[heartbeat] write failed: {exc}",
                      file=sys.stderr, flush=True)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    await asyncio.gather(
        news_feed.start(),
        bot_api.start(),
        _startup_notification(),
        _heartbeat_writer(),
        _shutdown_waiter(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        print("[main] KeyboardInterrupt — exiting", flush=True)
    except Exception:
        tb = traceback.format_exc()
        print(f"[main] FATAL CRASH:\n{tb}", file=sys.stderr, flush=True)
        try:
            notifier.broadcast_restart_sync(tm.restart_crash())
        except Exception:
            pass
        sys.exit(1)
