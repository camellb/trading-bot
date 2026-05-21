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
#
# Belt-and-suspenders defense, since the bot has wedged on this
# even WITH the prior DefaultResolver patch — some library was
# still constructing AsyncResolver directly:
#
#   1. Point AsyncResolver -> ThreadedResolver. Any code that
#      explicitly does `aiohttp.resolver.AsyncResolver()` now gets
#      a thread-pool resolver instead of c-ares.
#   2. Also pin DefaultResolver (legacy path).
#
# The PyInstaller spec also excludes aiodns + pycares from the
# bundle entirely so the c-ares C library is not even present at
# runtime. The combination guarantees DNS never runs on the
# asyncio loop.
import aiohttp.resolver as _aiohttp_resolver  # noqa: E402
_aiohttp_resolver.AsyncResolver = _aiohttp_resolver.ThreadedResolver
_aiohttp_resolver.DefaultResolver = _aiohttp_resolver.ThreadedResolver
# Also nuke any module-level c-ares import sitting in sys.modules
# from a prior import (defensive — should not exist in a fresh
# Python, but guards against test/dev-mode reload). If aiodns
# was imported, replace its Resolver with a wrapper that uses
# socket-based resolution.
import sys as _sys_for_dns_patch  # noqa: E402
if "aiodns" in _sys_for_dns_patch.modules:
    # aiodns being imported means c-ares is loaded. We can't
    # cleanly unload it, but we can stop callers from using its
    # resolver. The aiohttp patch above is the main protection;
    # this is a log signal so we know if it ever happens.
    print(
        "[delfi] WARNING: aiodns imported before main.py monkey-patch — "
        "PyInstaller spec excludes should have prevented this; check the bundle.",
        flush=True,
    )

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
from polymarket_runner import (
    evaluate_open_positions,
    resolve_positions,
    resolve_skipped_evaluations,
    scan_and_analyze,
)
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

    # The GIL switch interval defaults to 5ms. With many threads doing
    # sync work (SQLite, SSL handshakes, requests.get, etc.), the
    # asyncio main loop can wait 50ms+ for the GIL on each turn - long
    # enough that the kernel's listen-socket backlog fills up before
    # accept() runs. Dropping to 1ms hands the GIL back to the main
    # loop more aggressively. Trade-off: ~3-5% more context-switch
    # overhead. Worth it to keep /api/* responsive under load.
    sys.setswitchinterval(0.001)

    loop = asyncio.get_running_loop()
    # 32 workers, down from 100. Each worker that's actively running
    # holds the GIL during its Python-level code. With 100 of them
    # competing, the asyncio main loop starves of GIL ticks and
    # accept() falls behind - the recurring wedge cause this user
    # has hit for weeks. 32 is enough headroom for the GUI's
    # ~20-panel mount burst plus a couple of slow handlers without
    # creating excessive contention.
    loop.set_default_executor(ThreadPoolExecutor(
        max_workers=32, thread_name_prefix="delfi"))

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
    # Tightened wedge-detection thresholds: self-probe every 20s
    # (was 30s) and SIGKILL after 2 consecutive failures (was 3).
    # Net effect: a wedged daemon is detected and respawned in
    # ~40s instead of ~90s, so the dashboard sees the "stuck"
    # banner for much less time. Per-probe budget kept at 5s.
    watchdog = LoopHeartbeat(
        loop,
        api_port_getter=lambda: _api_port_holder["port"],
        self_probe_interval_s=20.0,
        self_probe_max_failures=2,
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

    # ── Pre-warm signer cache in a BACKGROUND THREAD ───────────────────
    # The prewarm makes a chain of CLOB SSL round-trips. When CLOB is
    # unreachable (DNS wedge, transient outage, slow network) each
    # call sits in an SSL read for up to 10s before raising. The
    # original synchronous version blocked the daemon boot for up to
    # 60s+ during a CLOB outage and the user saw the GUI sit on
    # "Starting Delfi..." indefinitely while launchd kept respawning
    # processes that never completed boot.
    #
    # Async prewarm: spawn a daemon thread, return immediately. The
    # API server starts accepting connections right away. The first
    # GUI poll arrives in 1-2s; if the cache isn't warm yet, the
    # lock-free getters return None and downstream callers fall back
    # to DB-derived bankroll. By the time the second poll arrives
    # 5s later the prewarm has almost always landed, and even if
    # CLOB is down forever the daemon stays responsive.
    #
    # The non-blocking lock on _POLY_SIGNER_LOCK guarantees only ONE
    # probe runs at a time, so the prewarm thread doesn't race against
    # the scheduled pm_balance_refresh job.
    def _prewarm_poly_caches() -> None:
        try:
            from engine.user_config import get_user_polymarket_creds
            from feeds.polymarket_wallet import (
                refresh_live_balance_cache,
                get_poly_signer_info,
                get_total_open_positions_value,
            )
            _prewarm_creds = get_user_polymarket_creds()
            _prewarm_pk = (_prewarm_creds or {}).get("private_key")
            if _prewarm_pk:
                print("[delfi] pre-warming Polymarket caches (bg)...",
                      flush=True)
                refresh_live_balance_cache(_prewarm_pk)
                try:
                    _info = get_poly_signer_info(_prewarm_pk)
                    _funder = (_info or {}).get("funder")
                    if _funder:
                        get_total_open_positions_value(_funder)
                except Exception as _exc2:
                    print(f"[delfi] positions cache pre-warm failed: "
                          f"{_exc2} - non-fatal",
                          flush=True)
                print("[delfi] Polymarket caches warm", flush=True)
        except Exception as _exc:
            print(f"[delfi] cache pre-warm failed: {_exc} - "
                  "proceeding; background refresh will retry",
                  flush=True)

    try:
        import threading as _t
        _prewarm_thread = _t.Thread(
            target=_prewarm_poly_caches,
            name="poly-prewarm",
            daemon=True,
        )
        _prewarm_thread.start()
    except Exception as _exc:
        print(f"[delfi] failed to spawn prewarm thread: {_exc}",
              flush=True)

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

    # Wall-clock ceiling for each scheduled job. APScheduler has
    # max_instances=1 per job, so if ONE call sticks forever (e.g.
    # pycares getaddrinfo wedge in a Wikipedia fetch, observed
    # 2026-05-14), every subsequent scheduled run gets dropped and
    # the bot silently stops trading. Wrapping in asyncio.wait_for
    # with a hard ceiling guarantees the coroutine is cancelled
    # after N seconds and the worker thread is freed even when a
    # downstream library is unresponsive to graceful cancellation.
    async def _bounded(coro, timeout_s: int, label: str):
        try:
            await _asyncio_module.wait_for(coro, timeout=timeout_s)
        except _asyncio_module.TimeoutError:
            print(f"[delfi] {label} exceeded {timeout_s}s wall-clock "
                  f"ceiling - aborted to keep the scheduler healthy",
                  file=sys.stderr, flush=True)
            raise

    def _run_scan():
        if not bool(getattr(config, "PM_SCAN_ENABLED", True)):
            return
        try:
            _asyncio_module.run(_bounded(
                scan_and_analyze(
                    limit          = int(getattr(config, "PM_SCAN_LIMIT", 100)),
                    min_volume_24h = float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
                    analyst        = analyst,
                ),
                timeout_s=240,
                label="scan",
            ))
            proc_health.record_job_ok("pm_scan")
        except Exception as exc:
            proc_health.record_job_error("pm_scan")
            print(f"[delfi] scan failed: {exc}", file=sys.stderr, flush=True)

    def _run_resolve():
        try:
            _asyncio_module.run(_bounded(
                resolve_positions(), timeout_s=180, label="resolve",
            ))
            proc_health.record_job_ok("pm_resolve")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve")
            print(f"[delfi] resolve failed: {exc}", file=sys.stderr, flush=True)

    def _run_resolve_fast():
        try:
            _asyncio_module.run(_bounded(
                resolve_positions(short_horizon_only=True),
                timeout_s=45, label="resolve_fast",
            ))
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

    def _run_evaluate_exits():
        """Tick the exit-policy decision engine across every open
        position. Cheap when the policy is disabled (per-user) or no
        positions are open. Runs at 60s cadence so a take-profit or
        stop-loss reacts within a minute of the bid moving."""
        try:
            _asyncio_module.run(_bounded(
                evaluate_open_positions(), timeout_s=60,
                label="evaluate_exits",
            ))
            proc_health.record_job_ok("pm_evaluate_exits")
        except Exception as exc:
            proc_health.record_job_error("pm_evaluate_exits")
            print(f"[delfi] evaluate_exits failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_balance_refresh():
        """Refresh the cached Polymarket live-balance every 60s.

        Without this, /api/summary's live-balance overlay had to
        call the wallet probe on every dashboard poll (every 5s).
        A slow DNS or SSL response on that probe could block the
        thread for 30s+ and - because the probe holds a process-
        level lock - queue every subsequent /api/summary behind it.
        The dashboard then showed "sidecar may be stuck" until the
        loop watchdog SIGKILLed the daemon. Moving the refresh
        into a scheduled job decouples user requests from network
        latency: /api/summary always reads the cache instantly;
        this job is the only path that touches the network. If
        THIS job wedges, the cache just goes slightly stale - no
        user-visible impact.

        No-op in simulation mode (no live key). Failure swallowed
        so a network blip doesn't cascade.
        """
        try:
            from engine.user_config import (
                get_user_config, get_user_polymarket_creds,
            )
            from feeds.polymarket_wallet import refresh_live_balance_cache
            cfg = get_user_config()
            if (cfg.mode or "").lower() != "live":
                proc_health.record_job_ok("pm_balance_refresh")
                return
            creds = get_user_polymarket_creds()
            pk = (creds or {}).get("private_key") if creds else None
            if not pk:
                proc_health.record_job_ok("pm_balance_refresh")
                return
            refresh_live_balance_cache(pk)
            proc_health.record_job_ok("pm_balance_refresh")
        except Exception as exc:
            proc_health.record_job_error("pm_balance_refresh")
            print(f"[delfi] balance refresh failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_activate_legacy_balance():
        """Auto-swap any legacy USDC.e at the funder into pUSD so
        the funds become tradeable Cash without a Polymarket-UI
        click. Idempotent: when funder USDC.e is 0 the function
        returns immediately after one cheap balanceOf eth_call.

        Why this matters: a market that settled in V1 USDC.e pays
        out USDC.e to the funder. Polymarket's V2 trading uses
        pUSD only, so the user's UI shows "Confirm pending deposit"
        until they manually click through. The user has explicitly
        called this out as user-hostile - the bot should redeem
        winnings AND make those winnings spendable for trading,
        not just deposit them in a frozen tier.

        Same RELAYER_API_KEY auth as the redeem sweeper, same
        relayer-v2.polymarket.com/submit endpoint. The 2-call batch
        (USDC.e.approve(wrapper) + wrapper.wrap) replicates exactly
        what polymarket.com's "Confirm pending deposit" sends.
        """
        try:
            from execution.pm_redeemer import activate_legacy_collateral_balance
            activate_legacy_collateral_balance()
            proc_health.record_job_ok("pm_activate_legacy")
        except Exception as exc:
            proc_health.record_job_error("pm_activate_legacy")
            print(f"[delfi] legacy-balance activator failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_pm_reconcile():
        """Pull on-chain Polymarket positions, import any missing ones.

        Safety net for the executor's poll-timeout class of bug: when
        `_open_live`'s fill poll times out before the CLOB matcher
        broadcasts the fill, the order STILL fills on-chain but no
        pm_positions row gets written. The user then sees fewer
        positions in Delfi than on Polymarket. The reconciler walks
        data-api/positions every 2 minutes, looks each entry up by
        (condition_id, side), and INSERTs anything missing using the
        on-chain truth. Conservative: only ADDS rows, never deletes
        or modifies. Cheap when in sync (single HTTPS GET + a
        SELECT-and-set diff).
        """
        try:
            from engine.pm_reconciler import reconcile_positions
            reconcile_positions()
            proc_health.record_job_ok("pm_reconcile")
        except Exception as exc:
            proc_health.record_job_error("pm_reconcile")
            print(f"[delfi] reconciler failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_redeem_sweeper():
        """Retry on-chain redemption for live winners that never
        completed their first redeem attempt.

        settle_position only fires redeem_winning_position once per
        position. If that one attempt fails (transient RPC 401, no
        MATIC at the moment, Builder creds not yet pasted, daemon
        crash mid-call) the row sits forever with redeem_tx_hash=NULL
        and the on-chain CTF tokens never reach the user's pUSD
        balance. The sweeper rescans on every tick so once the
        blocker is removed (MATIC funded, Builder API keys pasted)
        the backlog clears automatically.

        Cheap when there's nothing stuck: a single indexed-by-status
        SELECT that returns 0 rows.
        """
        try:
            from execution.pm_redeemer import sweep_unredeemed_winners
            sweep_unredeemed_winners(max_per_run=25)
            proc_health.record_job_ok("pm_redeem_sweep")
        except Exception as exc:
            proc_health.record_job_error("pm_redeem_sweep")
            print(f"[delfi] redeem sweeper failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_resolve_skipped():
        """Back-fill outcomes for skipped market_evaluations.

        Runs slow (every 15 min by default) because skipped markets
        are not time-critical for the user — they're a "would I have
        won?" audit surface, not a trading hot path. 120s budget is
        plenty for a 200-row batch against gamma."""
        try:
            _asyncio_module.run(_bounded(
                resolve_skipped_evaluations(), timeout_s=120,
                label="resolve_skipped",
            ))
            proc_health.record_job_ok("pm_resolve_skipped")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve_skipped")
            print(f"[delfi] resolve_skipped failed: {exc}",
                  file=sys.stderr, flush=True)

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
    scheduler.add_job(
        _run_evaluate_exits, IntervalTrigger(seconds=60),
        id="pm_evaluate_exits",
        # First fire 90s after boot — give the position resolver and
        # market scan a head start so we're never racing them on the
        # first tick after a daemon restart.
        next_run_time=now_utc + timedelta(seconds=90),
        # coalesce=False mirrors `pm_resolve_fast` — missing a tick on
        # a fast-moving market could push a take-profit far past the
        # threshold. Correctness > cost on this cadence.
        max_instances=1, coalesce=False,
        executor="threadpool",
        # misfire_grace_time=None: same reasoning as pm_activate_legacy
        # and pm_redeem_sweep. This job ALSO writes per-position
        # current_value_usd (mark-to-market) which feeds the Dashboard
        # "Locked Capital" + "Total Equity" tiles and every settlement
        # Telegram message. Default 1s grace caused most fires to be
        # silently dropped under threadpool load, leaving market values
        # NULL in the DB and the dashboard stuck on cost basis.
        misfire_grace_time=None,
    )
    scheduler.add_job(
        _run_resolve_skipped, IntervalTrigger(minutes=15),
        id="pm_resolve_skipped",
        # First fire 2 minutes after boot — give the position resolver
        # priority on a fresh start, then catch up on skipped evals.
        next_run_time=now_utc + timedelta(minutes=2),
        max_instances=1, coalesce=True,
        executor="threadpool",
    )
    scheduler.add_job(
        _run_activate_legacy_balance, IntervalTrigger(minutes=10),
        id="pm_activate_legacy",
        # Fire 4 min after boot — one minute after the redeem sweeper,
        # so freshly-redeemed USDC.e gets activated on the next 10-min
        # tick (or this one, if the redeem already landed). Cheap when
        # there's nothing to activate (single balanceOf eth_call).
        next_run_time=now_utc + timedelta(minutes=4),
        max_instances=1, coalesce=True,
        executor="threadpool",
        # misfire_grace_time=None: ALWAYS run, no matter how late.
        # APScheduler's default 1-second grace caused this job to be
        # silently dropped for hours under load (analyst LLM calls
        # held threadpool slots, every fire missed the 1s window and
        # was discarded). Wrapping USDC.e to pUSD is a 'must
        # eventually happen' job; we never want to drop a fire.
        # coalesce=True means N missed fires still result in one run.
        misfire_grace_time=None,
    )
    scheduler.add_job(
        _run_redeem_sweeper, IntervalTrigger(minutes=10),
        id="pm_redeem_sweep",
        # First fire 3 minutes after boot — well after the position
        # resolver and exit-policy jobs have had their first ticks,
        # so a freshly-settled row from this boot is in the table by
        # the time the sweeper looks. 10 minute cadence is fast
        # enough that a stuck winner clears within ~5 minutes of the
        # user funding MATIC or pasting Builder API keys, slow
        # enough that it doesn't hammer Polygon RPC when the queue
        # is empty.
        next_run_time=now_utc + timedelta(minutes=3),
        max_instances=1, coalesce=True,
        executor="threadpool",
        # misfire_grace_time=None: same reasoning as pm_activate_legacy
        # above. Redeeming a winner is a 'must eventually happen' job.
        # The user's money is sitting at the CTF contract; we don't
        # want APScheduler dropping fires just because the threadpool
        # was busy with analyst LLM work.
        misfire_grace_time=None,
    )
    scheduler.add_job(
        _run_pm_reconcile, IntervalTrigger(minutes=2),
        id="pm_reconcile",
        # First fire 5s after boot. Earlier than balance-refresh on
        # purpose: any in-flight order from a previous daemon
        # incarnation (crash, restart, install.sh kill) might have
        # filled while we were down. We want that backfilled BEFORE
        # the dashboard's first /api/positions poll lands, so the
        # user never sees a "missing position" flash. The 5s gives
        # the aiohttp accept loop time to bind without competing
        # for the threadpool.
        next_run_time=now_utc + timedelta(seconds=5),
        max_instances=1, coalesce=True,
        executor="threadpool",
        # misfire_grace_time=None: missing a tick (e.g. busy threadpool)
        # would mean a freshly-filled position sits invisible in Delfi
        # for an extra 2 min and the analyst could double-open into
        # the same market. Like pm_redeem_sweep, this is a
        # 'must eventually happen' job.
        misfire_grace_time=None,
    )
    scheduler.add_job(
        _run_balance_refresh, IntervalTrigger(seconds=60),
        id="pm_balance_refresh",
        # First fire immediately (1s after boot) so the signer cache
        # is populated before /api/summary's first poll. Without
        # this the dashboard hits a cold cache and Balance shows $0
        # (post-2026-05-20 fix) or, in the buggy version that fix
        # replaced, the SIM default $1000. 60s cadence afterwards is
        # fast enough that a fresh deposit shows up within a minute.
        next_run_time=now_utc + timedelta(seconds=1),
        max_instances=1, coalesce=True,
        executor="threadpool",
    )

    scheduler.start()
    print(
        f"[delfi] scheduler started -- scan {scan_interval_min}min, "
        f"resolve {resolve_interval_min}min (fast {fast_resolve_sec}s), "
        f"exit-policy 60s, markouts 1h",
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


def _install_log_file_tee() -> None:
    """Mirror every print() to <app-data>/logs/sidecar.log.

    On macOS the LaunchAgent plist captures stdout/stderr via
    StandardOutPath. On Windows there's no equivalent: the GUI-spawned
    sidecar has its stdout piped into the Tauri shell process, which
    re-prints to its own stdout/stderr - but Windows GUI apps don't
    show those anywhere. Without this tee, a Windows user has NO
    way to read sidecar output for troubleshooting.

    The tee writes to the file AND to the original stream (so Tauri's
    capture still works on every platform). Failures writing to the
    file are swallowed so a permission glitch can't take the daemon
    down.
    """
    try:
        # Import here, not at module top, because db.engine pulls in
        # SQLAlchemy and we want the log file open BEFORE anything
        # heavyweight has a chance to print.
        from db.engine import app_data_dir
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Rotate at boot: rename old sidecar.log to sidecar.log.prev
        # so the user always has the previous session's full log
        # available without the file growing unboundedly. Two files
        # only (current + previous) keeps disk usage trivial.
        log_path = log_dir / "sidecar.log"
        prev_path = log_dir / "sidecar.log.prev"
        if log_path.exists():
            try:
                if prev_path.exists():
                    prev_path.unlink()
                log_path.rename(prev_path)
            except Exception:
                pass
        fh = open(log_path, "a", encoding="utf-8", buffering=1)
    except Exception as exc:
        # If we can't open the file, don't die - just print and
        # carry on without the tee.
        try:
            print(f"[delfi] log file tee disabled: {exc}",
                  file=sys.stderr, flush=True)
        except Exception:
            pass
        return

    class _Tee:
        def __init__(self, original, file):
            self._original = original
            self._file = file
        def write(self, data):
            try:
                self._original.write(data)
            except Exception:
                pass
            try:
                self._file.write(data)
            except Exception:
                pass
        def flush(self):
            try:
                self._original.flush()
            except Exception:
                pass
            try:
                self._file.flush()
            except Exception:
                pass
        # Some libraries (e.g. faulthandler) check isatty() on the
        # underlying stream. Delegate to original so they see a real
        # answer.
        def isatty(self):
            try:
                return bool(self._original.isatty())
            except Exception:
                return False
        # fileno is needed by subprocess for inheriting stdio. Return
        # the ORIGINAL fd so child processes write to the same console
        # as before - the tee only captures Python-level writes.
        def fileno(self):
            return self._original.fileno()

    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    try:
        print(f"[delfi] log tee writing to {log_path}", flush=True)
    except Exception:
        pass


if __name__ == "__main__":
    _install_log_file_tee()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
