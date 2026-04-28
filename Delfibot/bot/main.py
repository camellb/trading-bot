"""
Entry point for the Delfi Python sidecar.

The Tauri shell launches this as a subprocess. The shell:
  1. Spawns this process with `DELFI_PORT=<picked port>` and `DELFI_DB_PATH`
     pointing at the OS-bundle data directory.
  2. Reads stdout for the line `DELFI_LOCAL_API_READY <port>` so it knows
     when to start loading the React UI in the webview.
  3. Sends SIGTERM on app quit. We catch it and shut down gracefully.

Architecture in this single process:

    PolymarketFeed -> PMAnalyst -> PMExecutor
       (gamma)        (forecast)    (sim/live)

    APScheduler runs scan / resolve / fast-resolve / markout jobs at
    config-driven intervals. local_api.py serves an aiohttp HTTP API on
    127.0.0.1:<port> so the React UI can read state and post commands.

No Telegram. No process-global API keys. Anthropic key + Polymarket private
key live in the OS keychain (engine.user_config). DB lives in the platform
app-data directory (db.engine).
"""

from __future__ import annotations

import asyncio
import faulthandler
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
from db.models import create_all_tables
from engine.markout_tracker import check_markouts
from engine.pm_analyst import PMAnalyst
from engine.user_config import ensure_default_user_config
from feeds.feed_health_monitor import monitor
from feeds.news_feed import NewsFeed
from feeds.macro_calendar import MacroCalendar
from local_api import LocalAPI
from polymarket_runner import resolve_positions, scan_and_analyze
from process_health import health as proc_health


# Dump all thread stacks on SIGUSR1 - useful for diagnosing hangs in the
# sidecar from a separate terminal (`kill -USR1 <pid>`). Skipped on
# Windows where SIGUSR1 doesn't exist.
faulthandler.enable()
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1)


def _seed_env_from_keychain() -> None:
    """
    Copy keychain-stored API keys into os.environ at startup.

    Existing research code (feeds/news_feed.py, research/fetcher.py)
    reads these keys via os.environ.get(...) - that path predates the
    keychain. Rather than refactor every reader, we hydrate the env
    once here so the existing code Just Works with whatever the user
    saved in Settings → Connections. Missing keys are no-ops; their
    consumers degrade gracefully (e.g. news_feed falls back to raw
    RSS titles when GEMINI_API_KEY is unset).
    """
    pairs = (
        ("ANTHROPIC_API_KEY",  get_anthropic_api_key()),
        ("NEWS_API_KEY",       get_newsapi_key()),
        ("CRYPTOPANIC_API_KEY", get_cryptopanic_key()),
    )
    seeded = []
    for env_name, value in pairs:
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value
            seeded.append(env_name)
    if seeded:
        print(f"[delfi] seeded env from keychain: {', '.join(seeded)}", flush=True)


async def main() -> None:
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(
        max_workers=20, thread_name_prefix="delfi"))

    bot_start_time = datetime.now(timezone.utc)
    monitor.set_bot_start_time(bot_start_time)
    proc_health.set_start_time(bot_start_time)

    print("[delfi] starting...", flush=True)

    create_all_tables()
    ensure_default_user_config()
    print("[delfi] DB ready", flush=True)

    # Pull optional API keys out of the OS keychain into os.environ so
    # the legacy env-reading code in feeds/news_feed.py and
    # research/fetcher.py picks them up without further refactor. Each
    # is optional - missing values just mean those research feeds run
    # in degraded mode (raw RSS instead of filtered headlines, etc.).
    _seed_env_from_keychain()

    # Pull optional API keys out of the OS keychain into os.environ so
    # the legacy env-reading code in feeds/news_feed.py and
    # research/fetcher.py picks them up without further refactor. Each
    # is optional - missing values just mean those research feeds run
    # in degraded mode (raw RSS instead of filtered headlines, etc.).
    _seed_env_from_keychain()

    news_feed = NewsFeed(monitor)
    macro_calendar = MacroCalendar(monitor)
    await macro_calendar.start()

    # Single-user app: the analyst evaluates each market once and the
    # executor runs against the local user. Notifications are written
    # to the SQLite event_log table the dashboard reads, so the analyst
    # no longer needs an explicit notifier handle.
    analyst = PMAnalyst(news_feed=news_feed)

    # ── Local HTTP API ───────────────────────────────────────────────────────
    # Bind to 127.0.0.1 only. The Tauri webview is the only thing that
    # talks to this port; nothing else on the machine should reach it.
    api_host = "127.0.0.1"
    api_port = int(os.environ.get("DELFI_PORT", "0"))
    api = LocalAPI(analyst=analyst, host=api_host, port=api_port)
    bound_port = await api.start()
    # Tauri reads this line from stdout to know when to load the UI.
    print(f"DELFI_LOCAL_API_READY {bound_port}", flush=True)

    # ── Scheduler ────────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler()
    api.set_scheduler(scheduler)

    scan_interval_min = int(getattr(config, "PM_SCAN_INTERVAL_MINUTES", 5))
    resolve_interval_min = int(getattr(config, "PM_RESOLVE_INTERVAL_MINUTES", 15))
    fast_resolve_sec = int(getattr(config, "PM_RESOLVE_FAST_INTERVAL_SECONDS", 60))

    async def _run_scan():
        if not bool(getattr(config, "PM_SCAN_ENABLED", True)):
            return
        try:
            await scan_and_analyze(
                limit          = int(getattr(config, "PM_SCAN_LIMIT", 100)),
                min_volume_24h = float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
                analyst        = analyst,
            )
            proc_health.record_job_ok("pm_scan")
        except Exception as exc:
            proc_health.record_job_error("pm_scan")
            print(f"[delfi] scan failed: {exc}", file=sys.stderr, flush=True)

    async def _run_resolve():
        try:
            await resolve_positions()
            proc_health.record_job_ok("pm_resolve")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve")
            print(f"[delfi] resolve failed: {exc}", file=sys.stderr, flush=True)

    async def _run_resolve_fast():
        try:
            await resolve_positions(short_horizon_only=True)
            proc_health.record_job_ok("pm_resolve_fast")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve_fast")
            print(f"[delfi] fast resolve failed: {exc}", file=sys.stderr, flush=True)

    async def _run_markouts():
        try:
            await check_markouts()
            proc_health.record_job_ok("markout_check")
        except Exception as exc:
            proc_health.record_job_error("markout_check")
            print(f"[delfi] markout failed: {exc}", file=sys.stderr, flush=True)

    now_utc = datetime.now(timezone.utc)
    scheduler.add_job(
        _run_scan, IntervalTrigger(minutes=scan_interval_min),
        id="pm_scan",
        next_run_time=now_utc + timedelta(seconds=60),
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _run_resolve, IntervalTrigger(minutes=resolve_interval_min),
        id="pm_resolve",
        next_run_time=now_utc + timedelta(minutes=5),
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _run_resolve_fast, IntervalTrigger(seconds=fast_resolve_sec),
        id="pm_resolve_fast",
        next_run_time=now_utc + timedelta(seconds=30),
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _run_markouts, IntervalTrigger(hours=1),
        id="markout_check",
        next_run_time=now_utc + timedelta(minutes=7),
        max_instances=1, coalesce=True,
    )

    scheduler.start()
    print(
        f"[delfi] scheduler started -- scan {scan_interval_min}min, "
        f"resolve {resolve_interval_min}min (fast {fast_resolve_sec}s), "
        f"markouts 1h",
        flush=True,
    )

    # ── Shutdown ─────────────────────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _handle_signal(sig, _frame):
        try:
            name = signal.Signals(sig).name
        except Exception:
            name = str(sig)
        print(f"[delfi] received {name} - shutting down", flush=True)
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig, None)
        except NotImplementedError:
            # Windows: signal.signal works, loop.add_signal_handler doesn't.
            signal.signal(sig, _handle_signal)

    await shutdown_event.wait()
    print("[delfi] stopping scheduler", flush=True)
    scheduler.shutdown(wait=False)
    await api.stop()
    print("[delfi] bye", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
