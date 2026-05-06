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
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
from apscheduler.triggers.interval import IntervalTrigger
import asyncio as _asyncio_module  # alias for the sync job wrappers below

# Force aiohttp's DNS resolver onto a thread pool BEFORE any
# ClientSession is constructed anywhere else in the codebase.
# Background: when ccxt was added as a dep it pulled aiodns in
# transitively. aiohttp auto-detects aiodns at import time and
# switches its default resolver from ThreadedResolver (socket-based,
# runs in a thread, can never block the loop) to AsyncResolver
# (c-ares, runs ON the event loop). A single slow / dropped DNS
# packet from c-ares would then wedge the entire sidecar event loop
# in `_cffi_f_ares_getaddrinfo` and every aiohttp endpoint
# (/api/state, /api/summary, /api/archetypes, etc.) would time out
# at 30s even though the handler bodies don't touch DNS at all.
# Pinning the default back to ThreadedResolver keeps DNS off the
# loop. Verified by sample(1): without this patch a wedged sidecar
# showed the main thread stuck inside _cffi_f_ares_getaddrinfo.
import aiohttp.resolver as _aiohttp_resolver  # noqa: E402
_aiohttp_resolver.DefaultResolver = _aiohttp_resolver.ThreadedResolver

import config
from db.models import create_all_tables
from engine.loop_watchdog import LoopHeartbeat
from engine.markout_tracker import check_markouts
from engine.pm_analyst import PMAnalyst
from engine.user_config import (
    ensure_default_user_config,
    get_anthropic_api_key,
    get_cryptopanic_key,
    get_newsapi_key,
)
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


def _start_parent_death_watchdog_REMOVED() -> None:
    """REMOVED 2026-04-30. The sidecar is a 24/7 daemon now (managed
    by launchd via the LaunchAgent installed at first launch). It
    must NOT die when the Tauri shell quits - the user closes the
    GUI but expects the bot to keep trading. Replacement: launchd's
    KeepAlive directive auto-restarts the sidecar if it crashes,
    independent of whether the GUI is open.
    """
    return  # noqa


def _start_parent_death_watchdog_OLD() -> None:
    """Exit the sidecar when the Tauri shell dies.

    Without this, a Tauri crash leaves the sidecar re-parented to
    launchd where it survives across subsequent app launches. Two
    sidecars on the same SQLite DB then contend for the writer lock
    and the GIL on shared state, which manifests in the UI as
    `/api/state` and `/api/summary` timing out at 30s.

    Tauri passes its own PID via `DELFI_PARENT_PID` because watching
    `os.getppid()` is wrong here: the immediate parent of the running
    Python interpreter is the PyInstaller bootstrapper, not the Tauri
    shell. The bootstrapper survives a Tauri crash because nothing
    closes its stdio, so polling getppid() would never see PPID=1
    and the sidecar would stay alive as an orphan.

    We poll once every two seconds via `os.kill(pid, 0)`, which
    raises ProcessLookupError when the PID no longer exists. On the
    first lookup miss we hard-exit. Daemon thread so it doesn't block
    normal shutdown.

    Falls back to `os.getppid()` watching when DELFI_PARENT_PID is
    unset (running outside Tauri, e.g. dev mode via `python main.py`).
    """
    import threading as _threading

    parent_pid_env = os.environ.get("DELFI_PARENT_PID")
    target_pid: Optional[int]
    target_kind: str
    if parent_pid_env and parent_pid_env.isdigit():
        target_pid = int(parent_pid_env)
        target_kind = "DELFI_PARENT_PID"
    else:
        target_pid = os.getppid()
        target_kind = "getppid()"
        if target_pid <= 1:
            return  # already orphaned at boot

    def _watch() -> None:
        import time as _t
        while True:
            try:
                # signal 0 = "does this PID exist?". Raises if not.
                os.kill(target_pid, 0)
            except ProcessLookupError:
                print(
                    f"[delfi] parent ({target_kind}={target_pid}) died - "
                    f"exiting sidecar",
                    flush=True,
                )
                os._exit(0)
            except Exception:
                # Permission errors etc - ignore, try again next tick.
                pass
            try:
                _t.sleep(2)
            except Exception:
                return

    t = _threading.Thread(
        target=_watch, name="delfi-parent-watchdog", daemon=True,
    )
    t.start()


_LAUNCHD_PATH_MARKER = "Library/Daemon/DelfiSidecar.app"


def _singleton_lock_path():
    """Canonical path for the singleton lock, INVARIANT across env vars.

    Critical: this MUST NOT depend on DELFI_DB_PATH. The launchd
    daemon's plist sets DELFI_DB_PATH to the Tauri AppData
    (`com.delfi.desktop/`). A sidecar spawned without that env (a
    Tauri-shell fallback, a manual `python main.py`) would otherwise
    fall back to the legacy default (`Delfi/`) and lock a DIFFERENT
    inode, letting two daemons coexist - the 2026-05-06 incident.
    Anchor the lock to the canonical Tauri bundle identifier dir
    regardless of env so every sidecar contends for the same flock.
    """
    import platform
    from pathlib import Path
    home = Path.home()
    sysname = platform.system()
    if sysname == "Darwin":
        d = home / "Library" / "Application Support" / "com.delfi.desktop"
    elif sysname == "Windows":
        base = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        d = base / "com.delfi.desktop"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        d = base / "com.delfi.desktop"
    d.mkdir(parents=True, exist_ok=True)
    return d / "sidecar.lock"


def _exec_path_of(pid: int) -> str:
    """Best-effort read of a process's executable path.

    Uses `ps -p PID -o comm=` which returns the binary path on macOS.
    Returns "" on any failure - the caller treats unknown paths as
    "not launchd-managed" so we err on the side of NOT killing anything
    we can't positively identify as an orphan.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _kill_orphan_sidecars() -> None:
    """Kill every delfi-sidecar process whose executable path is NOT
    inside the launchd-managed `.../Library/Daemon/DelfiSidecar.app/...`
    bundle.

    The launchd-managed daemon (this very binary, when launched via the
    LaunchAgent) is the ONLY canonical sidecar. Anything else - a
    Tauri-spawned orphan from the legacy fallback, a manual
    `python main.py`, a sidecar started under the wrong path - is by
    definition wrong and gets SIGKILL'd here.

    This runs at every sidecar startup. The flock that follows handles
    the corner case of two launchd respawns racing (e.g. during install
    bootout/bootstrap). End state: at most one daemon alive at any time.
    """
    import subprocess
    my_pid = os.getpid()
    try:
        my_pgid = os.getpgrp()
    except Exception:
        my_pgid = None

    try:
        out = subprocess.run(
            ["pgrep", "-x", "delfi-sidecar"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:
        print(f"[delfi] orphan-kill pgrep failed: {exc}", flush=True)
        return

    for tok in out.stdout.split():
        try:
            pid = int(tok.strip())
        except ValueError:
            continue
        if pid == my_pid:
            continue
        # Skip our own process group (PyInstaller bootloader + child).
        try:
            their_pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        if my_pgid is not None and their_pgid == my_pgid:
            continue
        # The discriminator: is this process running from the launchd
        # path? If yes, leave it alone - the flock that follows will
        # serialise the race. If no, it's an orphan from the Tauri
        # fallback or a manual run - kill it.
        path = _exec_path_of(pid)
        if _LAUNCHD_PATH_MARKER in path:
            continue
        try:
            import signal as _sig
            os.kill(pid, _sig.SIGKILL)
            print(
                f"[delfi] killed non-launchd-path sidecar pid={pid} "
                f"path={path!r}",
                flush=True,
            )
        except (ProcessLookupError, PermissionError):
            pass


def _acquire_singleton_lock() -> Optional[object]:
    """Take an exclusive flock so a second sidecar can't start.

    The lock file lives in the app data directory next to the SQLite
    DB. We keep the file descriptor open for the lifetime of the
    process; the kernel releases the lock when the FD is closed (or
    the process dies). If another sidecar already holds the lock, we
    exit immediately rather than try to share a DB with an orphan.

    Returns the file descriptor (None on failure to acquire). Caller
    keeps the reference alive so the lock stays held.
    """
    try:
        import fcntl
    except ImportError:
        return None  # Windows: no fcntl, skip the lock
    try:
        # Canonical path INDEPENDENT of DELFI_DB_PATH. Without this,
        # the launchd daemon (env-DELFI_DB_PATH=com.delfi.desktop/) and
        # a manual sidecar (no env, fallback to Delfi/) would lock
        # different inodes, letting two daemons coexist.
        lock_path = _singleton_lock_path()
    except Exception as exc:
        print(f"[delfi] singleton lock open failed: {exc}", flush=True)
        return None

    def _try_acquire(retry: bool = True):
        # r+ so we can READ the previous PID before truncating; fall
        # back to w+ when the file doesn't exist yet.
        try:
            f = open(str(lock_path), "r+")
        except FileNotFoundError:
            f = open(str(lock_path), "w+")
        except Exception as exc:
            print(f"[delfi] singleton lock open failed: {exc}", flush=True)
            return None

        print(
            f"[delfi] singleton lock attempt path={lock_path} "
            f"fd={f.fileno()} pid={os.getpid()}",
            flush=True,
        )
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Lock held - exit politely. The lock holder is the
            # launchd-managed daemon sidecar (the bot proper); we are
            # a duplicate spawn from the Tauri GUI's old behaviour or
            # a manual `python main.py` invocation. We must NOT kill
            # the daemon: it's the bot itself, doing 24/7 trading.
            # Tauri reads `<app-data>/sidecar.port` to find the
            # daemon's port - it doesn't need us.
            stale_pid = None
            try:
                f.seek(0)
                contents = f.read().strip()
                if contents.isdigit():
                    stale_pid = int(contents)
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass

            print(
                f"[delfi] daemon sidecar already running "
                f"(pid={stale_pid}) - this duplicate exiting cleanly. "
                f"The daemon is owned by launchd; it auto-restarts on "
                f"crash via KeepAlive=true.",
                flush=True,
            )
            os._exit(0)
        except Exception as exc:
            print(f"[delfi] singleton lock acquire failed: {exc}",
                  flush=True)
            try:
                f.close()
            except Exception:
                pass
            return None

        # Got the lock. Stamp our PID inside so future launches can
        # tell whether the holder is a legit process or an orphan.
        print(
            f"[delfi] singleton lock ACQUIRED pid={os.getpid()}",
            flush=True,
        )
        try:
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()))
            f.flush()
        except Exception as exc:
            print(f"[delfi] singleton lock pid-stamp failed: {exc}",
                  flush=True)
        return f

    return _try_acquire()


def _ppid_of(pid):
    """Return the parent PID of a running process, or None if it's
    gone. Used by the singleton-lock orphan reaper to decide whether
    a stale lock-holder is an orphan worth killing."""
    if not pid:
        return None
    try:
        import subprocess
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True, text=True, timeout=2,
        )
        s = out.stdout.strip()
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def _migrate_legacy_keychain_secrets() -> None:
    """One-time migration: copy every legacy macOS keychain entry to
    the file-backed secrets store, then delete the keychain entry.

    Why this exists: secrets used to live in the OS keychain. Each
    sidecar rebuild produced a new code signature, and macOS keychain
    ACLs are per-binary-signature, so every rebuild made the user
    type their login password 7 times to re-grant access. While the
    SecurityAgent prompts were on screen the keychain mutex was held
    and any other thread hitting Security.framework wedged the
    asyncio event loop. We've moved storage to a chmod-600 JSON file.

    This function reads each known legacy key once, copies whatever
    it finds to the file, and deletes the keychain entry. After it
    runs, future boots find the secrets in the file and never touch
    keychain again.

    Called BEFORE the aiohttp server starts so handler requests can't
    race with an in-flight prompt. Runs synchronously on the main
    thread for the same reason - if we did it in a background thread
    the rest of boot would race.

    Idempotent: a second call (or an already-migrated key) is a no-op
    because the file values short-circuit the legacy read.
    """
    from engine.user_config import (
        KEYRING_SERVICE,
        KEYRING_POLYMARKET_KEY,
        KEYRING_ANTHROPIC_KEY,
        KEYRING_LLM_BACKUP_KEY,
        KEYRING_NEWSAPI_KEY,
        KEYRING_CRYPTOPANIC_KEY,
        KEYRING_LICENSE_KEY,
        KEYRING_TELEGRAM_TOKEN,
        _read_secrets,
        _write_secrets,
    )
    keys = (
        KEYRING_POLYMARKET_KEY,
        KEYRING_ANTHROPIC_KEY,
        KEYRING_LLM_BACKUP_KEY,
        KEYRING_NEWSAPI_KEY,
        KEYRING_CRYPTOPANIC_KEY,
        KEYRING_LICENSE_KEY,
        KEYRING_TELEGRAM_TOKEN,
    )
    secrets = _read_secrets()
    if all(secrets.get(k) for k in keys):
        return  # everything already in file - nothing to do

    try:
        import keyring
    except Exception as exc:
        print(f"[delfi] keychain migration: keyring import failed: {exc}",
              flush=True)
        return

    migrated = 0
    for k in keys:
        if secrets.get(k):
            continue
        try:
            v = keyring.get_password(KEYRING_SERVICE, k)
        except Exception as exc:
            print(f"[delfi] keychain migration: read({k}) failed: {exc}",
                  flush=True)
            continue
        if not v:
            continue
        secrets[k] = v
        migrated += 1
        try:
            keyring.delete_password(KEYRING_SERVICE, k)
        except Exception:
            pass

    if migrated:
        try:
            _write_secrets(secrets)
            print(f"[delfi] migrated {migrated} secret(s) from keychain to file",
                  flush=True)
        except Exception as exc:
            print(f"[delfi] keychain migration: write failed: {exc}",
                  flush=True)


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
    # Step 1: kill any delfi-sidecar process whose executable path is
    # NOT inside the launchd-managed Daemon bundle. The launchd-managed
    # daemon is the canonical sidecar; anything else is a Tauri orphan,
    # a manual run, or a leftover from a legacy install. We err on the
    # side of NOT killing - if we can't read a process's exec path we
    # leave it alone, and the flock below handles the rest.
    _kill_orphan_sidecars()

    # Step 2: acquire the flock. Two cases left after step 1:
    #   - We're a launchd-managed daemon and the flock is free: we get
    #     it, we run.
    #   - We're a launchd-managed daemon and another launchd-managed
    #     daemon is already alive (race during install bootout->bootstrap):
    #     flock blocks, we exit cleanly via _acquire_singleton_lock().
    #   - We're a non-launchd sidecar (manual `python main.py`) and the
    #     launchd daemon is alive: flock blocks, we exit cleanly. The
    #     manual user has to bootout the LaunchAgent first if they want
    #     to take over.
    _SINGLETON_LOCK_FD = _acquire_singleton_lock()  # noqa: F841 - keep ref

    # Watch the parent (Tauri shell). Exit if it dies, so a Tauri
    # crash can't leave us re-parented to launchd as an orphan.
    # Parent-death watchdog removed 2026-04-30. The sidecar is now a
    # launchd-managed daemon meant to run 24/7. It survives the Tauri
    # GUI closing, crashes get auto-restarted by launchd, and it
    # starts on user login via RunAtLoad. See LaunchAgent at
    # ~/Library/LaunchAgents/com.delfi.bot.plist.
    _start_parent_death_watchdog_REMOVED()

    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    # 100 workers, not 20: the GUI loads 20+ panels simultaneously on
    # mount and each panel hits at least one /api endpoint that goes
    # through `_offload(...)`. With only 20 workers and a single slow
    # call, the pool saturates, queued handlers block, the client
    # gives up at its 30s ceiling, and we drown in CLOSE_WAIT
    # leaks. 100 workers is generous - sync work is short in steady
    # state (file reads, SQLite reads), and the threads are cheap
    # (~16KB stack each). Verified 2026-05-06: a 27-request burst
    # against the 20-worker pool wedged the daemon; the same burst
    # at 100 workers absorbs cleanly.
    loop.set_default_executor(ThreadPoolExecutor(
        max_workers=100, thread_name_prefix="delfi"))

    # Arm the loop-wedge watchdog as soon as the loop is alive. If the
    # asyncio loop ever stops pumping for >120s (the 5-hour wedge of
    # 2026-05-06 was the trigger), the watchdog dumps tracebacks to
    # sidecar.err and SIGKILL's the process. launchd's KeepAlive
    # respawns within ~10s and the GUI auto-reconnects via the
    # refresh_api_port Tauri command. The handle is forwarded to
    # LocalAPI so /api/health can surface pump latency for monitoring.
    #
    # The watchdog also self-probes /api/health every 30s. If three
    # consecutive probes time out, that's the "loop alive but accept
    # stopped" wedge - the heartbeat alone misses it. The self-probe
    # needs the bound port; we publish it via a mutable holder that
    # the LocalAPI section below populates after binding.
    _api_port_holder = {"port": 0}
    watchdog = LoopHeartbeat(
        loop, api_port_getter=lambda: _api_port_holder["port"],
    )
    watchdog.start()

    bot_start_time = datetime.now(timezone.utc)
    monitor.set_bot_start_time(bot_start_time)
    proc_health.set_start_time(bot_start_time)

    print("[delfi] starting...", flush=True)

    create_all_tables()
    ensure_default_user_config()
    print("[delfi] DB ready", flush=True)

    # Surface live-trading killswitch state at boot. If
    # `DELFI_LIVE_KILLSWITCH_OFF=1` is set, real CLOB orders will
    # fire on every live-mode trade. Default is unset (kill-switch
    # ON); the boot line lets the operator confirm intent on every
    # restart. If a poisoned plist or env injection ever flips
    # this without the user knowing, the alert shows up here.
    _ks_off = os.environ.get("DELFI_LIVE_KILLSWITCH_OFF", "").strip() in ("1", "true", "True")
    if _ks_off:
        print(
            "[delfi] LIVE KILLSWITCH IS OFF: real-money orders WILL fire "
            "in live mode. If you didn't set DELFI_LIVE_KILLSWITCH_OFF=1 "
            "yourself, audit your LaunchAgent plist + environment.",
            flush=True,
        )
    else:
        print("[delfi] live killswitch on (live mode falls through to simulation)",
              flush=True)

    # One-time migration: copy any legacy macOS keychain secrets to
    # the new file-backed store. This fires the SecurityAgent prompt
    # cascade ONCE per upgrade boot, then never again - subsequent
    # reads short-circuit at the file. Runs synchronously here, BEFORE
    # the aiohttp server starts, so handler requests never race with
    # an in-flight prompt and the event loop never wedges.
    _migrate_legacy_keychain_secrets()

    # Pull optional API keys out of the new file store into os.environ
    # so the legacy env-reading code in feeds/news_feed.py and
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
    api = LocalAPI(
        analyst=analyst, host=api_host, port=api_port, watchdog=watchdog,
    )
    bound_port = await api.start()
    # Tell the watchdog where to self-probe. Until this is set the
    # self-probe is a no-op; safe during the cold-start window.
    _api_port_holder["port"] = bound_port
    # Two channels for the Tauri shell to find us:
    #   1. stdout line - works in dev mode where Tauri spawns us
    #      directly and reads our stdout via piped CommandEvent.
    #   2. port file at <app-data>/sidecar.port - works when we run
    #      as a launchd daemon (no parent reading stdout). Tauri
    #      polls this file at startup. The file is removed on
    #      graceful shutdown so a stale file from a crashed sidecar
    #      doesn't mislead the GUI; if launchd respawns us, we
    #      rewrite it.
    try:
        from db.engine import app_data_dir
        port_file = app_data_dir() / "sidecar.port"
        port_file.write_text(str(bound_port))
    except Exception as exc:
        print(f"[delfi] port file write failed: {exc}", flush=True)
    print(f"DELFI_LOCAL_API_READY {bound_port}", flush=True)

    # ── Scheduler ────────────────────────────────────────────────────────────
    #
    # Why a separate threadpool executor: APScheduler's AsyncIOScheduler
    # default runs `async def` jobs ON the same event loop as aiohttp.
    # If a job does ANY blocking sync I/O (sqlite read, anthropic call,
    # urllib fetch in research/fetcher) the loop is blocked - new
    # /api/state requests can't be dispatched and the GUI shows
    # "Delfi could not start" after its 30s wait. Confirmed in production
    # 2026-05-03: `/api/state` hung indefinitely while a 165-quota scan
    # was mid-flight.
    #
    # Fix: register a ThreadPoolExecutor and route the heavy jobs to it.
    # Each job becomes a sync wrapper that calls asyncio.run() to spin
    # up its own short-lived event loop on the worker thread, isolated
    # from the API loop. The scan can take its full sweet time and
    # /api/state stays responsive throughout.
    #
    # The aiohttp client sessions used inside scan_and_analyze /
    # resolve_positions are created INSIDE the async body via
    # `async with PolymarketFeed() as feed:` so they're bound to the
    # job's own loop - they don't leak across loops. Same for the
    # sqlalchemy sessions (per-thread connections by default).
    scheduler = AsyncIOScheduler(executors={
        "default":    AsyncIOExecutor(),
        "threadpool": APThreadPoolExecutor(max_workers=4),
    })
    api.set_scheduler(scheduler)

    scan_interval_min = int(getattr(config, "PM_SCAN_INTERVAL_MINUTES", 5))
    resolve_interval_min = int(getattr(config, "PM_RESOLVE_INTERVAL_MINUTES", 15))
    fast_resolve_sec = int(getattr(config, "PM_RESOLVE_FAST_INTERVAL_SECONDS", 60))

    def _run_scan():
        if not bool(getattr(config, "PM_SCAN_ENABLED", True)):
            return
        try:
            _asyncio_module.run(scan_and_analyze(
                limit          = int(getattr(config, "PM_SCAN_LIMIT", 100)),
                min_volume_24h = float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
                analyst        = analyst,
            ))
            proc_health.record_job_ok("pm_scan")
        except Exception as exc:
            proc_health.record_job_error("pm_scan")
            print(f"[delfi] scan failed: {exc}", file=sys.stderr, flush=True)

    def _run_resolve():
        try:
            _asyncio_module.run(resolve_positions())
            proc_health.record_job_ok("pm_resolve")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve")
            print(f"[delfi] resolve failed: {exc}", file=sys.stderr, flush=True)

    def _run_resolve_fast():
        try:
            _asyncio_module.run(resolve_positions(short_horizon_only=True))
            proc_health.record_job_ok("pm_resolve_fast")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve_fast")
            print(f"[delfi] fast resolve failed: {exc}", file=sys.stderr, flush=True)

    def _run_markouts():
        try:
            _asyncio_module.run(check_markouts())
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
        executor="threadpool",
    )
    scheduler.add_job(
        _run_resolve, IntervalTrigger(minutes=resolve_interval_min),
        id="pm_resolve",
        next_run_time=now_utc + timedelta(minutes=5),
        max_instances=1, coalesce=True,
        executor="threadpool",
    )
    scheduler.add_job(
        _run_resolve_fast, IntervalTrigger(seconds=fast_resolve_sec),
        id="pm_resolve_fast",
        next_run_time=now_utc + timedelta(seconds=30),
        # coalesce=False: the fast resolver targets sub-1h markets,
        # so a missed 60s tick during a stall could mean a settlement
        # goes unrecorded past actual resolution. Correctness > cost
        # on this one. The slow `pm_resolve` keeps coalesce=True
        # because a 5min late re-fire on a multi-day market doesn't
        # matter.
        max_instances=1, coalesce=False,
        executor="threadpool",
    )
    scheduler.add_job(
        _run_markouts, IntervalTrigger(hours=1),
        id="markout_check",
        next_run_time=now_utc + timedelta(minutes=7),
        max_instances=1, coalesce=True,
        executor="threadpool",
    )

    scheduler.start()
    print(
        f"[delfi] scheduler started -- scan {scan_interval_min}min, "
        f"resolve {resolve_interval_min}min (fast {fast_resolve_sec}s), "
        f"markouts 1h",
        flush=True,
    )

    # Start the Telegram inbound command listener. No-op when Telegram
    # isn't configured. Restartable: local_api routes call this again
    # after save/test/disconnect so the listener picks up new creds.
    try:
        from feeds.telegram_notifier import start_command_listener as _tg_start
        if _tg_start():
            print("[delfi] telegram command listener started", flush=True)
    except Exception as exc:
        print(f"[delfi] telegram listener init failed: {exc}", flush=True)

    # ── Shutdown ─────────────────────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _handle_signal(sig, _frame):
        try:
            name = signal.Signals(sig).name
        except Exception:
            name = str(sig)
        print(f"[delfi] received {name} - shutting down", flush=True)
        # Clear the port file so a fresh launchd respawn (or the next
        # Tauri spawn) writes a new one and the GUI doesn't connect to
        # a stale port from a previous run.
        try:
            from db.engine import app_data_dir
            port_file = app_data_dir() / "sidecar.port"
            port_file.unlink(missing_ok=True)
        except Exception:
            pass
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
