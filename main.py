"""
Entry point for the Polymarket prediction-market bot.

Architecture at a glance:

    PolymarketFeed   — Gamma API client, pulls candidate markets
            │
            ▼
    PMAnalyst        — skips stale markets, fetches research, asks Claude
            │           for a calibrated probability, sizes via quarter-Kelly
            │
            ▼
    PMExecutor       — opens shadow (or live) positions, settles them
                        after resolution, feeds the calibration ledger

Scheduled jobs (APScheduler):
    PM scan          — every PM_SCAN_INTERVAL_MINUTES
    PM resolve       — every PM_RESOLVE_INTERVAL_HOURS
    Daily summary    — 08:30 MYT
    Weekly summary   — Monday 08:30 MYT
    Self-improvement — Sunday 08:30 MYT
    Monthly report   — 1st of month 08:30 MYT

Side services:
    bot_api.BotAPI  — localhost HTTP API for the dashboard
    TelegramNotifier poll thread for /status /scan /apply /skip /confirm-config

Fatal-at-import-time: .env must provide DATABASE_URL, ANTHROPIC_API_KEY,
BOT_API_SECRET. TELEGRAM_* is optional.
"""

# load_dotenv() must run before any module that reads os.getenv() at import.
from dotenv import load_dotenv
load_dotenv(override=True)

import asyncio
import faulthandler
import os
import signal
import sys
import traceback

# Dump all thread stacks on SIGUSR1 — invaluable for diagnosing hangs.
faulthandler.enable()
faulthandler.register(signal.SIGUSR1)

# ── Disable macOS App Nap ─────────────────────────────────────────────────
# App Nap suspends background processes, delaying time.sleep() and asyncio
# timers indefinitely. This kills the heartbeat and watchdog — the process
# appears alive but all timers freeze. Must be disabled at process level.
_app_nap_activity = None
if sys.platform == "darwin":
    try:
        import Foundation
        _NSActivityUserInitiated = 0x00FFFFFF
        _NSActivityLatencyCritical = 0xFF00000000
        _app_nap_activity = (
            Foundation.NSProcessInfo.processInfo()
            .beginActivityWithOptions_reason_(
                _NSActivityUserInitiated | _NSActivityLatencyCritical,
                "Trading bot: heartbeat and watchdog timers must fire on schedule",
            )
        )
        print("[main] App Nap disabled via NSProcessInfo", flush=True)
    except Exception as exc:
        print(f"[main] WARNING: Could not disable App Nap: {exc}",
              file=sys.stderr, flush=True)
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron     import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import calibration
from db.models                import create_all_tables
from feeds.feed_health_monitor import monitor
from feeds.news_feed           import NewsFeed
from feeds.macro_calendar      import MacroCalendar
from feeds.telegram_notifier   import notifier
from execution.pm_executor     import PMExecutor
from engine.pm_analyst         import PMAnalyst
from engine.memory             import MemoryManager
from engine.self_improvement   import SelfImprovementAnalyser
from bot_api                   import BotAPI
from polymarket_runner         import (
    scan_and_analyze,
    scan_via_subprocess,
    resolve_positions,
    reconcile_overdue_positions,
)
from engine.markout_tracker    import check_markouts
from process_health            import health as proc_health


def _rotate_logs(max_bytes: int = 5_000_000) -> None:
    """Rotate log files if they exceed max_bytes. Keep 1 backup."""
    from pathlib import Path
    for name in ("logs/bot.log", "logs/bot_error.log"):
        p = Path(name)
        if not p.exists():
            continue
        try:
            if p.stat().st_size > max_bytes:
                backup = p.with_suffix(".log.1")
                if backup.exists():
                    backup.unlink()
                p.rename(backup)
                p.touch()
        except Exception:
            pass


def _detect_crash_loop(marker_path: str = "logs/.start_times",
                       window_seconds: int = 300,
                       max_starts: int = 5,
                       cooldown_seconds: int = 60) -> None:
    """Record this start and check for rapid restarts.

    Appends the current timestamp to a small marker file. If we see
    >= max_starts within window_seconds, we're in a crash loop.
    Sleep for cooldown_seconds to break it and let transient issues
    (DB, network, rate-limits) clear.

    The marker file is separate from the log — immune to log rotation
    and doesn't require timestamp parsing from log lines.
    """
    from pathlib import Path

    marker = Path(marker_path)

    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Read existing timestamps (one ISO timestamp per line)
        existing: list[str] = []
        if marker.exists():
            existing = marker.read_text().strip().splitlines()

        # Append current start
        existing.append(now_iso)

        # Parse and filter to window
        cutoff = now - timedelta(seconds=window_seconds)
        recent: list[str] = []
        for line in existing:
            line = line.strip()
            if not line:
                continue
            try:
                ts = datetime.fromisoformat(line)
                if ts >= cutoff:
                    recent.append(line)
            except ValueError:
                continue  # skip malformed lines

        # Write back only recent timestamps (keeps file small)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("\n".join(recent) + "\n")

        if len(recent) >= max_starts:
            print(
                f"[main] CRASH LOOP DETECTED: {len(recent)} starts "
                f"in the last {window_seconds}s (threshold: {max_starts}). "
                f"Sleeping {cooldown_seconds}s to break the loop.",
                flush=True,
            )
            print(
                f"[main] CRASH LOOP DETECTED: {len(recent)} starts in "
                f"{window_seconds}s — cooling down {cooldown_seconds}s",
                file=sys.stderr, flush=True,
            )
            try:
                notifier.send_sync(
                    f"🔄 <b>Crash loop detected</b> — {len(recent)} restarts "
                    f"in {window_seconds}s. Cooling down for {cooldown_seconds}s."
                )
            except Exception:
                pass
            import time
            time.sleep(cooldown_seconds)
        else:
            print(f"[main] Startup #{len(recent)} in last {window_seconds}s "
                  f"(crash loop threshold: {max_starts})", flush=True)
    except Exception as exc:
        # Never let crash-loop detection itself prevent startup
        print(f"[main] crash-loop check failed (non-fatal): {exc}",
              file=sys.stderr, flush=True)


def _start_self_watchdog(interval: int = 60, grace: int = 300) -> dict:
    """
    Background THREAD that monitors event-loop liveness and writes the
    heartbeat file.

    The event loop updates _watchdog_state["last_ping"] via loop.call_later
    every 30s. This thread:
      1. Writes logs/.heartbeat every `interval` seconds (keeps the
         external launchd watchdog happy regardless of event loop state).
      2. Checks the in-memory ping — if the event loop hasn't pinged in
         `grace` seconds, the event loop is truly frozen and we os._exit.

    This design decouples the heartbeat FILE (always fresh, written by
    this thread) from the event-loop liveness CHECK (in-memory ping).
    """
    import threading, time
    from pathlib import Path

    _watchdog_state = {"last_ping": time.monotonic()}
    _heartbeat_file = Path("logs/.heartbeat")

    def _watchdog_thread():
        check_count = 0
        while True:
            time.sleep(interval)
            check_count += 1
            try:
                # Always write heartbeat file — keeps external watchdog happy.
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    _heartbeat_file.write_text(now_iso + "\n")
                except Exception:
                    pass

                # Check event loop liveness via in-memory ping.
                dict_gap = time.monotonic() - _watchdog_state["last_ping"]

                if check_count <= 3 or check_count % 10 == 0 or dict_gap > grace * 0.5:
                    print(
                        f"[watchdog #{check_count}] "
                        f"event_loop_ping={int(dict_gap)}s ago "
                        f"(kill threshold={grace}s)",
                        file=sys.stderr, flush=True,
                    )

                if dict_gap > grace:
                    # Event loop hasn't pinged in >grace seconds — frozen.
                    # KILL FIRST — spawn hard-kill thread before any I/O.
                    def _hard_kill():
                        time.sleep(3)
                        os._exit(70)
                    threading.Thread(target=_hard_kill, daemon=True,
                                     name="watchdog-kill").start()

                    msg = (
                        f"[watchdog] event loop frozen for {int(dict_gap)}s "
                        f"(threshold={grace}s) — forcing restart"
                    )
                    try:
                        print(msg, file=sys.stderr, flush=True)
                        print(msg, flush=True)
                    except Exception:
                        pass
                    try:
                        notifier.send_sync(
                            f"💀 <b>Bot self-killed</b> — event loop frozen for "
                            f"{int(dict_gap)}s. launchd will restart."
                        )
                    except Exception:
                        pass
                    os._exit(70)

            except Exception as exc:
                try:
                    print(f"[watchdog] CHECK FAILED: {exc}",
                          file=sys.stderr, flush=True)
                except Exception:
                    pass

    t = threading.Thread(target=_watchdog_thread, daemon=True, name="self-watchdog")
    t.start()
    return _watchdog_state


async def main() -> None:
    # Check for crash loops BEFORE doing anything expensive.
    # If we detect rapid restarts, sleep to break the loop.
    _detect_crash_loop()
    _rotate_logs()

    # Increase the default thread pool — the default (5 workers) gets
    # saturated by research/Claude/feedparser calls during scans, blocking
    # the event loop from processing API requests.
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=20,
                                                  thread_name_prefix="bot"))

    # Start self-watchdog FIRST — if anything below freezes the event loop,
    # the watchdog thread will force-kill us after 5 minutes so launchd restarts.
    import time as _time
    _watchdog_state = _start_self_watchdog(interval=60, grace=300)

    bot_start_time = datetime.now(timezone.utc)
    monitor.set_bot_start_time(bot_start_time)
    proc_health.set_start_time(bot_start_time)

    print("Polymarket bot starting…", flush=True)
    print(f"PM_MODE: {config.PM_MODE}", flush=True)
    print(
        f"Starting cash: "
        f"shadow=${config.PM_SHADOW_STARTING_CASH:.0f}, "
        f"live=${config.PM_LIVE_STARTING_CASH:.0f}",
        flush=True,
    )

    create_all_tables()
    print("Database tables verified.", flush=True)

    repair_stats = await asyncio.get_running_loop().run_in_executor(
        None, calibration.repair_polymarket_resolution_history
    )
    print(
        "Calibration history verified: "
        f"{repair_stats.get('checked', 0)} checked, "
        f"{repair_stats.get('fixed', 0)} fixed, "
        f"{repair_stats.get('skipped', 0)} skipped.",
        flush=True,
    )
    snapshot_seed = await asyncio.get_running_loop().run_in_executor(
        None, calibration.seed_polymarket_snapshot_history
    )
    print(f"Calibration snapshots verified: {snapshot_seed}", flush=True)

    # ── Core singletons ──────────────────────────────────────────────────────
    executor  = PMExecutor()
    memory    = MemoryManager()

    # ── Overlay feeds (advisory only — do not gate trading) ──────────────────
    news_feed      = NewsFeed(monitor)
    macro_calendar = MacroCalendar(monitor)
    await macro_calendar.start()

    analyst   = PMAnalyst(executor=executor, notifier=notifier, memory=memory,
                          news_feed=news_feed)

    # ── Self-improvement analyser ────────────────────────────────────────────
    self_improvement = SelfImprovementAnalyser(notifier=notifier, memory=memory)

    # ── Wire notifier references used by /status and scheduled summaries ─────
    notifier._loop            = asyncio.get_running_loop()
    notifier._bot_start_time  = bot_start_time
    notifier._monitor         = monitor
    notifier._executor        = executor
    notifier._analyst         = analyst

    # Telegram polling thread for commands (/apply /skip /status etc.)
    notifier.start_polling(self_improvement)

    # ── HTTP API (dashboard) ────────────────────────────────────────────────
    bot_api = BotAPI(analyst=analyst, executor=executor, notifier=notifier)
    notifier._bot_api = bot_api

    startup_reconcile = await reconcile_overdue_positions(
        notifier=notifier,
        executor=executor,
        max_passes=int(getattr(config, "PM_STARTUP_RECONCILE_PASSES", 3)),
    )
    stale_before = startup_reconcile.get("stale_before")
    stale_after = startup_reconcile.get("stale_after")
    awaiting_after = startup_reconcile.get("awaiting_after")
    if (stale_before or 0) > 0 or (startup_reconcile.get("awaiting_before") or 0) > 0:
        print(
            "[main] startup reconciliation complete: "
            f"stale_before={stale_before} "
            f"stale_after={stale_after} "
            f"awaiting_after={awaiting_after} "
            f"settled={startup_reconcile.get('positions_settled', 0)} "
            f"passes={startup_reconcile.get('passes', 0)}",
            flush=True,
        )
    if stale_before is None or stale_after is None:
        msg = (
            "Could not verify unresolved Polymarket positions on startup. "
            "New scans will stay blocked until reconciliation succeeds."
        )
        print(f"[main] WARNING: {msg}", file=sys.stderr, flush=True)
        if hasattr(notifier, "notify_error"):
            await notifier.notify_error("startup_reconcile", msg)
    elif stale_after > 0:
        msg = (
            f"{stale_after} Polymarket positions are past their expected end date "
            "but haven't been officially resolved yet. The resolver will settle "
            "them automatically once Polymarket posts results. Scans continue normally."
        )
        print(f"[main] INFO: {msg}", flush=True)
        # Don't send error notification — this is normal. Polymarket often
        # takes 1-3 days to resolve markets. The resolver will catch them.
    elif (awaiting_after or 0) > 0:
        print(
            f"[main] INFO: Awaiting official results for {awaiting_after} "
            "recently finished markets. This is normal and new scans remain enabled.",
            flush=True,
        )

    # ── Scheduler ───────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler()

    # PM scan — cadence from config.
    scan_interval_min = int(getattr(config, "PM_SCAN_INTERVAL_MINUTES", 60))

    async def _run_scan():
        """Run the scan in an isolated subprocess (own GIL + event loop)."""
        try:
            summary = await scan_via_subprocess(
                limit=int(getattr(config, "PM_SCAN_LIMIT", 20)),
                min_volume_24h=float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
                timeout=1500,
            )
            if summary.get("error"):
                proc_health.record_job_error("pm_scan")
                print(f"[main] scan error: {summary['error']}",
                      file=sys.stderr, flush=True)
            elif summary.get("skipped") is True:
                print(f"[main] scan skipped: {summary.get('reason', '?')}",
                      flush=True)
            else:
                proc_health.record_job_ok("pm_scan")
                opened = summary.get("opened", 0)
                fetched = summary.get("fetched", 0)
                no_trade = summary.get("no_trade", 0)
                errors = summary.get("errors", 0)
                print(
                    f"[main] scan complete: fetched={fetched} "
                    f"opened={opened} no_trade={no_trade} errors={errors}",
                    flush=True,
                )
                # Notify Telegram of each new position.
                if opened > 0:
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
                            end = t.get("end_date")
                            end_str = end.strftime("%Y-%m-%d") if hasattr(end, "strftime") else str(end or "?")[:10]
                            why = (
                                f"Bot estimate for YES ({prob_c:.1f}%) is "
                                f"{'above' if side == 'YES' else 'below'} "
                                f"the crowd price ({entry_c:.1f}c)."
                            )
                            msg = (
                                f"🎯 <b>New PM position</b> [{analyst.executor.mode}]\n"
                                f"<b>{oc['question'][:140]}</b>\n"
                                f"Bet: buy {side} at {entry_c:.1f}c\n"
                                f"Stake: ${t['stake_usd']:.2f} for {t['shares']:.1f} shares\n"
                                f"Edge: {t['edge_bps']:.0f} bps | Confidence: {t['confidence']:.2f}\n"
                                f"{why}\n"
                                f"Resolves: {end_str}\n"
                                f"Position: #{t['position_id']}"
                            )
                            await notifier.send(msg)
                        except Exception as exc:
                            print(f"[main] notify position failed: {exc}",
                                  file=sys.stderr)
        except Exception as exc:
            proc_health.record_job_error("pm_scan")
            print(f"[main] scan failed: {exc}", file=sys.stderr, flush=True)

    scheduler.add_job(
        _run_scan,
        IntervalTrigger(minutes=scan_interval_min),
        id="pm_scan",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        max_instances=1,
        coalesce=True,
    )

    # PM resolver.
    resolve_interval_h = int(getattr(config, "PM_RESOLVE_INTERVAL_HOURS", 6))

    async def _run_resolve():
        try:
            await resolve_positions(notifier=notifier, executor=executor, risk_mgr=analyst.risk_mgr)
            proc_health.record_job_ok("pm_resolve")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve")
            print(f"[main] resolve failed: {exc}", file=sys.stderr)

    scheduler.add_job(
        _run_resolve,
        IntervalTrigger(hours=resolve_interval_h),
        id="pm_resolve",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
        max_instances=1,
        coalesce=True,
    )

    # Fast resolver for short-horizon markets (every 10 min).
    fast_resolve_min = int(getattr(config, "PM_RESOLVE_FAST_INTERVAL_MINUTES", 10))

    async def _run_resolve_fast():
        try:
            await resolve_positions(short_horizon_only=True, notifier=notifier, executor=executor, risk_mgr=analyst.risk_mgr)
            proc_health.record_job_ok("pm_resolve_fast")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve_fast")
            print(f"[main] fast resolve failed: {exc}", file=sys.stderr)

    scheduler.add_job(
        _run_resolve_fast,
        IntervalTrigger(minutes=fast_resolve_min),
        id="pm_resolve_fast",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
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
    scheduler.add_job(
        self_improvement.analyse_and_report,
        CronTrigger(day_of_week="sun", hour=8, minute=30,
                    timezone="Asia/Kuala_Lumpur"),
        id="self_improvement",
    )
    scheduler.add_job(
        self_improvement.generate_monthly_report,
        CronTrigger(day=1, hour=8, minute=30, timezone="Asia/Kuala_Lumpur"),
        id="monthly_report",
    )

    # ── Watchdog — alert if core jobs stop running ────────────────────────
    _watchdog_alerted: set[str] = set()
    _WATCHDOG_THRESHOLDS = {
        "pm_scan":         timedelta(minutes=scan_interval_min * 3),
        "pm_resolve":      timedelta(hours=resolve_interval_h * 3),
        "pm_resolve_fast": timedelta(minutes=fast_resolve_min * 5),
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
                await notifier.notify_error(
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

    # ── Telegram polling health monitor ─────────────────────────────────
    async def _check_telegram_poll():
        """Detect dead Telegram polling thread and auto-restart it."""
        if not notifier.enabled:
            return
        if proc_health.uptime_seconds < 120:
            return  # give it time to start
        if not notifier.is_polling_alive():
            print("[main] Telegram polling thread is dead — restarting",
                  flush=True)
            await notifier.notify_error(
                "telegram_poll",
                "Polling thread died — auto-restarting it now."
            )
            await asyncio.get_running_loop().run_in_executor(
                None, notifier.restart_polling
            )

    scheduler.add_job(
        _check_telegram_poll,
        IntervalTrigger(minutes=2),
        id="telegram_poll_health",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    print(
        f"[main] Scheduler started: "
        f"PM scan every {scan_interval_min}min, "
        f"resolver every {resolve_interval_h}h (fast every {fast_resolve_min}min), "
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
        # Set shutdown event FIRST — never let a blocking Telegram call
        # prevent the event loop from exiting.
        shutdown_event.set()
        # Fire-and-forget the Telegram notification in a thread so the
        # signal handler returns immediately and doesn't block the loop.
        import threading
        threading.Thread(
            target=lambda: notifier.send_sync(
                "🛑 <b>Bot stopping</b> — will restart automatically"
            ),
            daemon=True,
            name="signal-notify",
        ).start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig, None)

    # ── Tasks ───────────────────────────────────────────────────────────────
    async def _startup_notification() -> None:
        await asyncio.sleep(20)
        await notifier.notify_startup()

    async def _shutdown_waiter() -> None:
        await shutdown_event.wait()
        print("[main] Shutdown event received — stopping scheduler", flush=True)
        # Hard deadline: if cleanup hangs, force-exit after 10s so
        # launchd can restart us. Runs in a daemon thread.
        import threading
        threading.Thread(
            target=lambda: (_time.sleep(10),
                            print("[main] Graceful shutdown timed out — forcing exit",
                                  flush=True),
                            os._exit(0)),
            daemon=True, name="shutdown-deadline",
        ).start()
        try:
            scheduler.shutdown(wait=False)
            # Kill scan subprocess before exiting — prevents orphaned
            # workers that could overlap with the next bot process's scan.
            from polymarket_runner import kill_scan_subprocess
            kill_scan_subprocess()
            if bot_api._runner:
                await bot_api._runner.cleanup()
        except Exception as exc:
            print(f"[main] Cleanup error (non-fatal): {exc}", flush=True)
        print("[main] Cleanup complete — exiting", flush=True)
        os._exit(0)

    # ── Heartbeat via loop.call_later ─────────────────────────────────
    # Uses call_later instead of an asyncio.Task with asyncio.sleep().
    # call_later fires directly from the timer heap in _run_once —
    # it bypasses the Task/Future/call_soon_threadsafe machinery that
    # can stall when _write_to_self blocks (observed on macOS kqueue).
    _heartbeat_file = "logs/.heartbeat"
    _heartbeat_count = [0]  # mutable container for closure

    def _write_heartbeat_file() -> None:
        """Write heartbeat file and update watchdog ping."""
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            with open(_heartbeat_file, "w") as hf:
                hf.write(now_iso + "\n")
        except Exception:
            pass

    def _heartbeat_tick() -> None:
        """Called every 30s directly by the event loop's timer heap."""
        _heartbeat_count[0] += 1
        _watchdog_state["last_ping"] = _time.monotonic()
        _write_heartbeat_file()
        n = _heartbeat_count[0]
        if n <= 5 or n % 10 == 0:
            print(f"[heartbeat] event loop alive (tick #{n})", flush=True)
        # Reschedule — even if the callback raises, call_later is
        # re-registered because we schedule BEFORE doing any work.
        loop.call_later(30, _heartbeat_tick)

    # Initial heartbeat + schedule first tick
    _write_heartbeat_file()
    _watchdog_state["last_ping"] = _time.monotonic()
    print("[heartbeat] initial heartbeat written", flush=True)
    loop.call_later(30, _heartbeat_tick)

    # Use return_exceptions=True so one crashing task doesn't kill others.
    # The shutdown_waiter keeps the gather alive; when it fires, everything
    # winds down gracefully.
    results = await asyncio.gather(
        news_feed.start(),
        bot_api.start(),
        _startup_notification(),
        _shutdown_waiter(),
        return_exceptions=True,
    )

    # If we get here, shutdown_waiter completed (shutdown signal received).
    # Log any task exceptions that occurred silently.
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            task_names = ["news_feed", "bot_api", "startup_notify", "shutdown"]
            name = task_names[i] if i < len(task_names) else f"task_{i}"
            print(f"[main] Task '{name}' had exception: {result}",
                  file=sys.stderr, flush=True)



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
            notifier.send_sync(
                f"💀 <b>Bot crashed — restarting automatically</b>\n"
                f"<pre>{tb[-500:]}</pre>"
            )
        except Exception:
            pass
        sys.exit(1)
