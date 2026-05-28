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
import traceback as _traceback
from datetime import datetime, timedelta, timezone


def _install_crash_log() -> None:
    """Write unhandled exceptions to a per-user crash log file.

    Anchored to the same dir Tauri uses (`%APPDATA%\\com.delfi.desktop\\`
    on Windows, `~/Library/Application Support/com.delfi.desktop/` on
    macOS, `~/.local/share/com.delfi.desktop/` on Linux). The dir is
    created on demand.

    Why this exists: Windows release builds run `delfi-sidecar.exe`
    with `console=False` (PyInstaller spec) so anything written to
    stderr goes nowhere. A bug that crashes the sidecar's event loop
    leaves the user with a "Delfi could not start" splash and zero
    diagnostic surface. With this hook, the user can attach
    `crash.log` to a support email and we get a full traceback.

    Format: each crash is a UTC timestamp + traceback + 2-line
    separator. Append-only; no truncation. A multi-MB log over years
    is fine - we'd rather have history than risk losing recent
    crashes to a rotation race.

    Best-effort: anything that goes wrong inside the hook is
    swallowed silently (we're already crashing; don't crash harder).
    """
    import platform as _platform
    from pathlib import Path as _Path

    def _log_dir() -> _Path:
        home = _Path.home()
        sysname = _platform.system()
        if sysname == "Windows":
            base = _Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
            return base / "com.delfi.desktop"
        if sysname == "Darwin":
            return home / "Library" / "Application Support" / "com.delfi.desktop"
        base = _Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        return base / "com.delfi.desktop"

    def _write_crash(text: str) -> None:
        try:
            d = _log_dir()
            d.mkdir(parents=True, exist_ok=True)
            with (d / "crash.log").open("a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def _excepthook(exc_type, exc, tb):
        try:
            ts = datetime.now(timezone.utc).isoformat()
            header = (
                f"\n==== unhandled exception {ts} pid={os.getpid()} ====\n"
            )
            body = "".join(_traceback.format_exception(exc_type, exc, tb))
            _write_crash(header + body)
        except Exception:
            pass
        # Also keep the default behaviour (stderr) for dev mode.
        try:
            _traceback.print_exception(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _excepthook


_install_crash_log()


from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
import asyncio as _asyncio_module  # alias for the sync job wrappers below

# Force aiohttp's DNS resolver onto a thread pool BEFORE any
# ClientSession is constructed anywhere else in the codebase.
#
# Background: when aiodns/pycares is installed, aiohttp auto-detects it
# at import time and switches its default resolver from ThreadedResolver
# (socket-based, runs in a thread pool, never blocks the loop) to
# AsyncResolver (c-ares, runs ON the event loop via kqueue/epoll).
#
# This causes two categories of wedge:
#
# 1. Main-loop DNS stall: a slow/dropped DNS packet in c-ares blocks
#    the main aiohttp event loop. Every /api endpoint times out even
#    though the handlers don't touch DNS at all.
#
# 2. Scanner-thread kqueue corruption (confirmed 2026-05-21 via thread
#    dump): scanner jobs run via asyncio.run() in a threadpool thread.
#    asyncio.run() creates its own event loop. pycares creates a Channel
#    (a c-ares DNS socket) associated with that loop. When asyncio.run()
#    closes the scanner loop, pycares' _run_safe_shutdown_loop background
#    thread tries to clean up by calling loop.remove_reader(). At that
#    point asyncio.get_event_loop() returns the MAIN loop (scanner's
#    loop is already closed/None). pycares removes the MAIN loop's
#    server listen FD from kqueue. The server socket stays in LISTEN
#    state but kqueue no longer fires EVFILT_READ, so Python never calls
#    accept(). Accept queue fills, new SYNs get no SYN-ACK, GUI shows
#    "timed out" indefinitely.
#
# Belt-and-suspenders defense:
#
#   1. Point AsyncResolver -> ThreadedResolver. Any code that
#      explicitly does `aiohttp.resolver.AsyncResolver()` now gets
#      a thread-pool resolver instead of c-ares.
#   2. Pin DefaultResolver in BOTH the resolver module AND
#      aiohttp.connector's own copy. The connector module imports
#      DefaultResolver by reference at module load time
#      (`from .resolver import DefaultResolver`), so patching only
#      aiohttp.resolver.DefaultResolver is insufficient -- TCPConnector's
#      __init__ still sees AsyncResolver (as confirmed by the thread dump
#      showing pycares active after the previous one-sided patch).
#
# The PyInstaller spec also excludes aiodns + pycares from the
# bundle entirely so the c-ares C library is not even present at
# runtime. The combination guarantees DNS never runs on the
# asyncio loop.
import aiohttp.resolver as _aiohttp_resolver  # noqa: E402
import aiohttp.connector as _aiohttp_connector  # noqa: E402
_aiohttp_resolver.AsyncResolver = _aiohttp_resolver.ThreadedResolver
_aiohttp_resolver.DefaultResolver = _aiohttp_resolver.ThreadedResolver
_aiohttp_connector.DefaultResolver = _aiohttp_resolver.ThreadedResolver
# Also nuke any module-level c-ares import sitting in sys.modules
# from a prior import (defensive - should not exist in a fresh
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
        "[delfi] WARNING: aiodns imported before main.py monkey-patch - "
        "PyInstaller spec excludes should have prevented this; check the bundle.",
        flush=True,
    )

import concurrent.futures as _cf  # noqa: E402
import threading as _threading    # noqa: E402

# ── Persistent job event-loop ────────────────────────────────────────────────
#
# Every _run_* job wrapper previously called asyncio.run(), which creates
# a fresh event loop, runs the coroutine, then does cleanup. The cleanup
# calls loop.shutdown_default_executor() which waits for ALL executor
# threads to finish: DNS lookups (ThreadedResolver), DDGS search threads,
# httpx initialisation. If any of those threads hang -- confirmed 2026-05-21
# via thread dump: DDGS threads stuck in logging.getLogger (global lock
# contention) and httpx.__init__, with concurrent futures thread.py:166
# (submit) blocked waiting for _shutdown_lock -- the cleanup deadlocks.
# The APScheduler worker thread stays blocked indefinitely. The accept
# queue eventually fills because no new kqueue events drain it, and the
# GUI shows the same "timed out" banner as the original pycares wedge.
#
# Fix: one persistent background event loop that lives for the process
# lifetime. There is NO cleanup cycle (shutdown_default_executor is never
# called), so NO deadlock. Executor threads that hang just time out on
# their own network timeouts and return their slots to the pool. New scans
# are unaffected because the pool has 16 named slots and is never shut down.
#
# The API loop (main asyncio.run(main()) loop) stays dedicated to aiohttp
# request handling. All scheduler jobs run on _job_loop so a slow scan
# cannot stall a /api/state call.
_job_loop: asyncio.AbstractEventLoop | None = None
_job_loop_lock = _threading.Lock()


def _ensure_job_loop() -> asyncio.AbstractEventLoop:
    """Return the singleton persistent job loop, starting it if needed."""
    global _job_loop
    # Fast path: already running.
    if _job_loop is not None and _job_loop.is_running():
        return _job_loop
    with _job_loop_lock:
        if _job_loop is not None and _job_loop.is_running():
            return _job_loop
        loop = asyncio.new_event_loop()
        # Named threads make crash dumps readable: "delfi-job-0", etc.
        loop.set_default_executor(
            _cf.ThreadPoolExecutor(
                max_workers=16,
                thread_name_prefix="delfi-job",
            )
        )
        t = _threading.Thread(
            target=loop.run_forever,
            name="delfi-job-loop",
            daemon=True,
        )
        t.start()
        _job_loop = loop
        return loop


def _submit_job(coro, outer_timeout_s: int) -> None:
    """Submit *coro* to the persistent job loop and block the calling
    APScheduler thread until the coroutine finishes or *outer_timeout_s*
    elapses.

    The coroutine is typically already wrapped with _bounded() which raises
    asyncio.TimeoutError after its own shorter deadline. The outer fence
    here is a belt-and-suspenders guard: if _bounded itself hangs (e.g.
    asyncio.CancelledError doesn't propagate out of a stuck run_in_executor
    call), the outer fence fires, we cancel the Future so the job-loop task
    gets cancelled on the next iteration, and re-raise so the caller's
    except-block can record the error.
    """
    future = asyncio.run_coroutine_threadsafe(coro, _ensure_job_loop())
    try:
        future.result(timeout=outer_timeout_s)
    except _cf.TimeoutError:
        future.cancel()
        raise TimeoutError(
            f"job hit outer {outer_timeout_s}s fence "
            "(inner wait_for did not cancel in time)"
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
    get_license_key,
    get_license_meta,
    get_newsapi_key,
    set_license_key,
    set_license_meta,
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

    macOS only. The whole logic targets launchd-vs-non-launchd
    distinction and uses pgrep / os.getpgid / os.getpgrp - none of
    which exist or apply on Windows. The Win32 named mutex acquired
    in `_acquire_singleton_lock_windows` is the equivalent
    enforcement on Windows.
    """
    import platform
    if platform.system() != "Darwin":
        return
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


def _acquire_singleton_lock_windows() -> Optional[object]:
    """Windows singleton enforcement via a named kernel mutex.

    Equivalent to the POSIX flock path below: a second sidecar that
    tries to start while one is already running calls CreateMutex,
    gets ERROR_ALREADY_EXISTS, and exits cleanly. The kernel releases
    the mutex when the holding process exits (even on hard crash), so
    no stale lock files to clean up.

    Returns the mutex HANDLE on success (caller keeps the reference
    alive so the mutex stays held). Returns None on Win32 API
    failure. Calls os._exit(0) when another instance holds it.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    except Exception as exc:
        print(f"[delfi] kernel32 binding failed: {exc}", flush=True)
        return None

    # No `Global\` prefix: per-session namespace is correct for a
    # per-user desktop app. Multi-user terminal-services hosts get one
    # sidecar per session, which is what we want anyway.
    MUTEX_NAME = "com.delfi.desktop.sidecar.singleton"
    ERROR_ALREADY_EXISTS = 183

    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    err = kernel32.GetLastError()

    if not handle:
        print(f"[delfi] CreateMutex returned NULL err={err}", flush=True)
        return None

    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        print(
            "[delfi] another sidecar already holds the singleton mutex - "
            "this duplicate exiting cleanly.",
            flush=True,
        )
        os._exit(0)

    print(
        f"[delfi] singleton mutex ACQUIRED pid={os.getpid()} "
        f"name={MUTEX_NAME!r}",
        flush=True,
    )
    return handle


def _probe_holder_responsive() -> bool:
    """Return True iff the sidecar.port file points to a daemon that
    answers an HTTP probe within 1.5 seconds.

    Used by the singleton-lock acquire path to distinguish a HEALTHY
    daemon (we should back off) from a WEDGED daemon (we should kill
    and steal). Cheap and bounded: a single connect+GET with a
    sub-2s deadline, no retries.

    Fail-closed: any error (port file missing, port not listening,
    connection refused, HTTP timeout) is treated as "holder is NOT
    responsive" so the caller takes the steal path. We'd rather
    occasionally kill a healthy-but-slow daemon than leave a wedged
    one in place forever.
    """
    try:
        port_path = _singleton_lock_path().parent / "sidecar.port"
        try:
            port_text = port_path.read_text().strip()
        except (FileNotFoundError, OSError):
            return False
        if not port_text.isdigit():
            return False
        port = int(port_text)
        import urllib.request
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health",
                timeout=1.5,
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
    except Exception:
        return False


def _acquire_singleton_lock() -> Optional[object]:
    """Take an exclusive lock so a second sidecar can't start.

    On POSIX (macOS/Linux) uses fcntl flock against a file in the app
    data dir; on Windows uses a named kernel mutex (see
    `_acquire_singleton_lock_windows`). Both auto-release on process
    exit so an orphan from a crash doesn't permanently block startup.

    Returns the lock object (file handle or mutex handle) on success;
    caller keeps the reference alive so the lock stays held. Returns
    None if the lock primitive isn't available on this platform.
    """
    import platform
    if platform.system() == "Windows":
        return _acquire_singleton_lock_windows()

    try:
        import fcntl
    except ImportError:
        return None  # exotic POSIX without fcntl - skip the lock
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
            # Lock held by another sidecar. Previously we exited
            # politely on the assumption that the holder was a healthy
            # launchd-managed daemon. In practice that assumption fails
            # when the holder is wedged on a slow synchronous call
            # (e.g. a Polymarket data-api HTTPS read that won't return
            # for 30s+): kickstart fires SIGTERM, the holder ignores
            # it because it's stuck in C-extension code, kickstart
            # spawns us, we politely exit, kickstart respawns us,
            # repeat until launchd hits ThrottleInterval and gives up.
            # The user sees a daemon that never recovers.
            #
            # New policy: the newest sidecar always wins. If we can't
            # get the lock:
            #   1. Read the holder's PID.
            #   2. Health-probe the holder's port (cheap, 1.5s budget).
            #   3. If the holder responds: exit politely (the legit
            #      already-running daemon path).
            #   4. If the holder does NOT respond: SIGTERM + 2s grace
            #      + SIGKILL the holder, then retry the lock.
            # This guarantees that a fresh launchctl kickstart or a
            # user-initiated relaunch always ends with exactly one
            # working sidecar, even if the previous one wedged.
            stale_pid = None
            try:
                f.seek(0)
                contents = f.read().strip()
                if contents.isdigit():
                    stale_pid = int(contents)
            except Exception:
                pass

            holder_alive = False
            holder_responsive = False
            if stale_pid:
                try:
                    os.kill(stale_pid, 0)
                    holder_alive = True
                except (ProcessLookupError, PermissionError):
                    holder_alive = False
                if holder_alive:
                    holder_responsive = _probe_holder_responsive()

            if holder_alive and holder_responsive:
                try:
                    f.close()
                except Exception:
                    pass
                print(
                    f"[delfi] healthy daemon already running "
                    f"(pid={stale_pid}, port-responding) - this "
                    f"duplicate exiting cleanly.",
                    flush=True,
                )
                os._exit(0)

            # Holder is wedged (or gone). Kill it and steal the lock.
            # SIGTERM first so any cleanup hooks the daemon registered
            # get a chance to run (logger flush, position cache write).
            if holder_alive and stale_pid:
                print(
                    f"[delfi] previous daemon (pid={stale_pid}) is "
                    f"unresponsive - SIGTERM, 2s grace, SIGKILL if it "
                    f"won't budge.",
                    flush=True,
                )
                try:
                    os.kill(stale_pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                import time as _t
                for _ in range(20):  # 2s in 100ms ticks
                    try:
                        os.kill(stale_pid, 0)
                    except (ProcessLookupError, PermissionError):
                        break
                    _t.sleep(0.1)
                else:
                    try:
                        os.kill(stale_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                # Drain TIME_WAIT / give the kernel a moment to release
                # the lock the dead process held. Without this the
                # retry below sometimes still sees BlockingIOError.
                _t.sleep(0.3)

            try:
                f.close()
            except Exception:
                pass

            if retry:
                return _try_acquire(retry=False)
            # Retry also failed - bail rather than loop. Whoever holds
            # the lock now is presumably racing us; let launchd respawn
            # this process and try again.
            print(
                f"[delfi] could not steal singleton lock from "
                f"pid={stale_pid} after one retry. Exiting.",
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
                refresh_pnl_caches,
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
                        # Warms BOTH the positions sum (currentValue
                        # for "locked capital") AND the user-pnl
                        # endpoint (Polymarket's authoritative
                        # all-time P&L) in one shot. Without this
                        # warm, the first ~60s of /api/summary after
                        # a daemon restart returned a local-fallback
                        # P&L that drifted from Polymarket's headline
                        # by $1-3. The dashboard showed the wrong
                        # number until pm_balance_refresh fired the
                        # first scheduled refresh — exactly the
                        # symptom in the 2026-05-23 screenshot.
                        refresh_pnl_caches(_funder)
                except Exception as _exc2:
                    print(f"[delfi] positions/pnl cache pre-warm "
                          f"failed: {_exc2} - non-fatal",
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
    # Jobs run on _job_loop (the persistent background event loop defined
    # at module level), NOT on the main API loop. This keeps two concerns
    # isolated:
    #
    #  1. The aiohttp API loop stays dedicated to HTTP request handling.
    #     A slow 240s scan cannot stall a /api/state call.
    #
    #  2. The persistent _job_loop has no cleanup cycle. The old pattern
    #     (asyncio.run() per scan) destroyed the loop after each scan and
    #     called shutdown_default_executor(), which deadlocked whenever a
    #     DDGS or httpx thread hung. See the module-level comment for the
    #     full forensics.
    #
    # APScheduler's ThreadPoolExecutor fires the _run_* wrappers in its own
    # thread pool. Each wrapper calls _submit_job() which submits the async
    # coroutine to _job_loop via run_coroutine_threadsafe and blocks the
    # APScheduler thread until the coroutine completes (or the outer fence
    # fires). max_instances=1 per job prevents overlapping runs.
    #
    # The aiohttp client sessions used inside scan_and_analyze /
    # resolve_positions are created INSIDE the async body via
    # `async with PolymarketFeed() as feed:` -- they're bound to
    # _job_loop and do not leak into the API loop. SQLAlchemy sessions
    # use per-thread connections and are unaffected by the loop split.
    scheduler = AsyncIOScheduler(executors={
        "default":    AsyncIOExecutor(),
        "threadpool": APThreadPoolExecutor(max_workers=4),
    })
    api.set_scheduler(scheduler)

    scan_interval_min = int(getattr(config, "PM_SCAN_INTERVAL_MINUTES", 5))
    resolve_interval_min = int(getattr(config, "PM_RESOLVE_INTERVAL_MINUTES", 15))
    fast_resolve_sec = int(getattr(config, "PM_RESOLVE_FAST_INTERVAL_SECONDS", 60))

    # ── License revocation poll ────────────────────────────────────────────
    #
    # See the docstring on `_run_license_revocation_check` further down.
    # Async helper lives here so the scheduler hooks closure over it.
    async def _license_revocation_check_async():
        import base64
        import json as _json
        import os as _os
        blob = get_license_key()
        if not blob:
            # No license activated yet (e.g. dev mode, owner bypass).
            # Nothing to revoke; quiet no-op.
            return
        if blob.strip() == "DELFI-OWNER-LOCAL-2026":
            # Maintainer bypass: never check.
            return

        # Pull the license id out of the signed payload locally. We
        # don't need to re-verify the signature here - verify_license
        # ran at boot and stamped license_meta.
        try:
            encoded_payload = blob.strip().split(".", 1)[0]
            pad = (-len(encoded_payload)) % 4
            raw = base64.urlsafe_b64decode(encoded_payload + ("=" * pad))
            payload = _json.loads(raw)
            license_id = payload.get("id")
        except Exception as exc:
            print(f"[delfi] revocation_check: payload decode failed: {exc}",
                  file=sys.stderr, flush=True)
            return
        if not isinstance(license_id, str) or not license_id:
            return

        base_url = _os.environ.get(
            "DELFI_LICENSE_CHECK_URL",
            "https://delfibot.com/api/license/check",
        )
        # v1.5.16+: we also send the device fingerprint so the server
        # can tell us whether the activation slot still belongs to
        # this machine. If the user force-claimed the slot on another
        # device, the response will carry device_match=false and we
        # treat it as a soft revoke ("license in use on another
        # device"). Pre-v1.5.16 clients omit device_id and the server
        # omits device_match from the response - the existing
        # revocation behavior remains unchanged for them.
        try:
            from engine.device_id import get_device_id
            device_id = get_device_id()
        except Exception:
            device_id = ""
        url = f"{base_url}?id={license_id}"
        if device_id:
            url = f"{url}&device_id={device_id}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            ) as sess:
                async with sess.get(url) as r:
                    if r.status != 200:
                        # Soft-fail on any non-200. A 5xx or a Vercel
                        # cold-start that timed out MUST NOT lock out
                        # a paying customer; we retry tomorrow.
                        print(f"[delfi] revocation_check: HTTP {r.status} - "
                              "treating as inconclusive, will retry next cycle",
                              flush=True)
                        return
                    body = await r.json()
        except Exception as exc:
            print(f"[delfi] revocation_check: network error: {exc} - "
                  "treating as inconclusive, will retry next cycle",
                  flush=True)
            return

        if not isinstance(body, dict):
            return

        # First check: licence revoked (refund, dispute, admin action).
        # Hard lock with the explicit revoke_reason from the server.
        if body.get("valid") is False:
            reason = body.get("revoke_reason") or "license revoked by issuer"
            print(f"[delfi] revocation_check: LICENSE REVOKED - {reason}. "
                  "Clearing local license key and disabling bot.",
                  file=sys.stderr, flush=True)
            try:
                set_license_key(None)
                set_license_meta({
                    "status": "revoked",
                    "reason": reason,
                    "revoked_at": body.get("revoked_at"),
                })
            except Exception as exc:
                print(f"[delfi] revocation_check: failed to clear local "
                      f"license: {exc}", file=sys.stderr, flush=True)
            try:
                from engine.user_config import update_user_config
                update_user_config(bot_enabled=False)
            except Exception as exc:
                print(f"[delfi] revocation_check: failed to disable bot: {exc}",
                      file=sys.stderr, flush=True)
            return

        # Second check: licence is valid but the activation slot has
        # been claimed by a different device. Same lock-down treatment
        # but with a different user-visible reason so the LicenseGate
        # can render "in use on <other device>" instead of "revoked".
        if body.get("device_match") is False:
            other = body.get("current_device_label") or "another device"
            reason = f"This licence is currently active on {other}."
            print(f"[delfi] revocation_check: DEVICE MISMATCH - slot "
                  f"taken by '{other}'. Locking local install.",
                  file=sys.stderr, flush=True)
            try:
                set_license_key(None)
                set_license_meta({
                    "status": "device_mismatch",
                    "reason": reason,
                    "current_device_label": other,
                })
            except Exception as exc:
                print(f"[delfi] revocation_check: failed to clear local "
                      f"license: {exc}", file=sys.stderr, flush=True)
            try:
                from engine.user_config import update_user_config
                update_user_config(bot_enabled=False)
            except Exception as exc:
                print(f"[delfi] revocation_check: failed to disable bot: {exc}",
                      file=sys.stderr, flush=True)
            return

        # All good. Stamp the meta with the latest validation timestamp
        # so the user can see "checked X minutes ago" in Settings.
        try:
            meta = get_license_meta() or {}
            from datetime import datetime as _dt, timezone as _tz
            meta["last_validated_at"] = _dt.now(_tz.utc).isoformat()
            meta["last_remote_check"] = "valid"
            set_license_meta(meta)
        except Exception:
            pass
        return

    async def _license_boot_claim_async():
        """One-shot boot task that claims the per-licence device slot.

        Why this exists separately from the revocation poll:

        The revocation poll READS the slot (via /api/license/check)
        and locks if device_match=false, but it does not CREATE the
        slot for an already-activated user who upgrades from a
        pre-v1.5.16 release. Without this boot-claim, an upgrader's
        cached licence would keep working with the server slot empty
        - which means another machine could claim it silently.

        This task POSTs to /api/license/claim-device with force=false
        once at sidecar startup. Three outcomes:

          - 200 + slot was free or already ours -> we own it now.
            Future revocation polls will see device_match=true and
            heartbeat last_seen_at.

          - 409 -> the slot is held by a different machine (e.g. user
            already activated on Machine B before upgrading Machine A
            to v1.5.16). Lock locally with status="device_mismatch"
            using the same code path as the revocation poll.

          - Network error / 5xx -> silent. The daily revocation poll
            will continue to enforce the lock; we'll retry the claim
            on the next boot.

        Owner-bypass licences (id="owner-local") skip the claim - the
        server has no row for them and would reject the UUID format.
        """
        blob = (get_license_key() or "").strip()
        if not blob:
            return  # no licence cached, nothing to claim
        try:
            encoded_payload = blob.split(".", 1)[0]
            pad = (-len(encoded_payload)) % 4
            raw = base64.urlsafe_b64decode(encoded_payload + ("=" * pad))
            payload = _json.loads(raw)
            license_id = payload.get("id")
        except Exception as exc:
            print(f"[delfi] boot_claim: payload decode failed: {exc}",
                  file=sys.stderr, flush=True)
            return
        if not isinstance(license_id, str) or not license_id:
            return
        if license_id == "owner-local":
            return  # owner bypass, no server slot

        try:
            from engine.device_id import get_device_id, get_device_label
            device_id = get_device_id()
            device_label = get_device_label()
        except Exception as exc:
            print(f"[delfi] boot_claim: device_id lookup failed: {exc}",
                  file=sys.stderr, flush=True)
            return

        base_url = _os.environ.get(
            "DELFI_LICENSE_API_BASE",
            "https://delfibot.com/api/license",
        )
        url = f"{base_url}/claim-device"
        body = {
            "license_id":   license_id,
            "device_id":    device_id,
            "device_label": device_label,
            "force":        False,
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            ) as sess:
                async with sess.post(url, json=body) as r:
                    status = r.status
                    try:
                        resp_body = await r.json()
                    except Exception:
                        resp_body = {}
        except Exception as exc:
            print(f"[delfi] boot_claim: network error: {exc} - "
                  "will retry on next boot or via daily check",
                  flush=True)
            return

        if status == 200:
            print("[delfi] boot_claim: slot claimed/heartbeated for this device",
                  flush=True)
            return

        if status == 409:
            other = (resp_body or {}).get("current_device_label") or "another device"
            reason = f"This licence is currently active on {other}."
            print(f"[delfi] boot_claim: SLOT TAKEN by '{other}'. "
                  "Locking local install.",
                  file=sys.stderr, flush=True)
            try:
                set_license_key(None)
                set_license_meta({
                    "status": "device_mismatch",
                    "reason": reason,
                    "current_device_label": other,
                })
            except Exception as exc:
                print(f"[delfi] boot_claim: failed to clear local "
                      f"license: {exc}", file=sys.stderr, flush=True)
            try:
                from engine.user_config import update_user_config
                update_user_config(bot_enabled=False)
            except Exception as exc:
                print(f"[delfi] boot_claim: failed to disable bot: {exc}",
                      file=sys.stderr, flush=True)
            return

        # 4xx (other than 409) / 5xx -> log and move on; the daily
        # revocation poll continues to enforce the lock.
        print(f"[delfi] boot_claim: HTTP {status} - {resp_body}. "
              "Will retry on next boot.",
              flush=True)

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
            _submit_job(
                _bounded(
                    scan_and_analyze(
                        limit          = int(getattr(config, "PM_SCAN_LIMIT", 100)),
                        min_volume_24h = float(getattr(config, "PM_MIN_VOLUME_24H_USD", 10_000.0)),
                        analyst        = analyst,
                    ),
                    timeout_s=240,
                    label="scan",
                ),
                outer_timeout_s=260,
            )
            proc_health.record_job_ok("pm_scan")
        except Exception as exc:
            proc_health.record_job_error("pm_scan")
            print(f"[delfi] scan failed: {exc}", file=sys.stderr, flush=True)

    def _run_resolve():
        try:
            _submit_job(
                _bounded(resolve_positions(), timeout_s=180, label="resolve"),
                outer_timeout_s=200,
            )
            proc_health.record_job_ok("pm_resolve")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve")
            print(f"[delfi] resolve failed: {exc}", file=sys.stderr, flush=True)

    def _run_resolve_fast():
        try:
            _submit_job(
                _bounded(
                    resolve_positions(short_horizon_only=True),
                    timeout_s=45, label="resolve_fast",
                ),
                outer_timeout_s=60,
            )
            proc_health.record_job_ok("pm_resolve_fast")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve_fast")
            print(f"[delfi] fast resolve failed: {exc}", file=sys.stderr, flush=True)

    def _run_markouts():
        try:
            _submit_job(
                _bounded(check_markouts(), timeout_s=120, label="markouts"),
                outer_timeout_s=140,
            )
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
            _submit_job(
                _bounded(
                    evaluate_open_positions(), timeout_s=60,
                    label="evaluate_exits",
                ),
                outer_timeout_s=75,
            )
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
            # Piggy-back the Polymarket P&L cache refresh on this
            # same 60s tick so the dashboard's All-Time P&L stays
            # current without ever doing the slow user-pnl-api
            # fetch on the request path. /api/summary reads from
            # the cache via cached_user_total_pnl(); if this job
            # ever stops running the cache goes stale within 3x
            # TTL (3 minutes) and the dashboard falls back to the
            # local realized+unrealized total - never the wedge.
            try:
                from feeds.polymarket_wallet import (
                    get_poly_signer_info, refresh_pnl_caches,
                )
                info = get_poly_signer_info(pk)
                funder = (info or {}).get("funder")
                if funder:
                    refresh_pnl_caches(funder)
            except Exception as exc:
                # Swallow: pnl refresh is best-effort. The bankroll
                # refresh above is what gates the OK/error counter.
                print(f"[delfi] pnl cache refresh failed: {exc}",
                      file=sys.stderr, flush=True)
            proc_health.record_job_ok("pm_balance_refresh")
        except Exception as exc:
            proc_health.record_job_error("pm_balance_refresh")
            print(f"[delfi] balance refresh failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_equity_snapshot():
        """Append one (bankroll, open_cost, equity) row to
        equity_snapshots for the current user+mode.

        Powers the Dashboard + Performance "Equity history" chart.
        The chart used to be reconstructed on the frontend from
        current_equity - cumulative_realized_pnl, which silently
        retconned the past whenever the wallet jumped (deposits,
        withdrawals, late payouts). Periodic real snapshots make
        deposits show up as natural step-ups at the actual time
        they happened.

        10-min cadence. Failure swallowed - missing one tick just
        leaves a small gap in the curve, never a crash.
        """
        try:
            from engine.equity_snapshot import record_equity_snapshot
            record_equity_snapshot()
            proc_health.record_job_ok("equity_snapshot")
        except Exception as exc:
            proc_health.record_job_error("equity_snapshot")
            print(f"[delfi] equity snapshot failed: {exc}",
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
            _submit_job(
                _bounded(
                    resolve_skipped_evaluations(), timeout_s=120,
                    label="resolve_skipped",
                ),
                outer_timeout_s=140,
            )
            proc_health.record_job_ok("pm_resolve_skipped")
        except Exception as exc:
            proc_health.record_job_error("pm_resolve_skipped")
            print(f"[delfi] resolve_skipped failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_daily_summary():
        """End-of-day recap to Telegram. Fires every day at 23:00 UTC.
        Aggregates the last 24h of resolved positions + the day's
        analysed-market count, formats via telegram_messages.daily_
        summary, sends via log_event so the user's notification-prefs
        toggle gates delivery (event_type='daily_summary').

        Skips the send if nothing happened in the window (no
        resolved positions AND no markets analysed). An empty
        end-of-day ping is noise.
        """
        try:
            from sqlalchemy import text
            from db.engine import get_engine
            from db.logger import log_event
            from execution.pm_executor import PMExecutor
            from engine.user_config import DEFAULT_USER_ID
            from feeds import telegram_messages as _tm

            executor = PMExecutor(DEFAULT_USER_ID)
            stats = executor.get_portfolio_stats()
            if not stats.get("ready"):
                return
            bankroll  = float(stats.get("bankroll", 0.0))
            open_cost = float(stats.get("open_cost", 0.0))
            equity    = float(stats.get("equity", bankroll + open_cost))

            eng = get_engine()
            with eng.connect() as conn:
                row24 = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) AS resolved24, "
                    "  COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins24, "
                    "  COALESCE(SUM(CASE WHEN realized_pnl_usd < 0 THEN 1 ELSE 0 END), 0) AS losses24, "
                    "  COALESCE(SUM(realized_pnl_usd), 0) AS pnl24 "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = 'live' "
                    "  AND status IN ('settled', 'closed_early') "
                    "  AND settled_at > datetime('now', '-24 hours')"
                ), {"uid": DEFAULT_USER_ID}).fetchone()
                resolved24 = int(row24[0] or 0)
                wins24     = int(row24[1] or 0)
                losses24   = int(row24[2] or 0)
                pnl24      = float(row24[3] or 0.0)
                cnt24_row = conn.execute(text(
                    "SELECT COUNT(*) FROM market_evaluations "
                    "WHERE user_id = :uid "
                    "  AND evaluated_at > datetime('now', '-24 hours')"
                ), {"uid": DEFAULT_USER_ID}).fetchone()
                cnt24 = int(cnt24_row[0] or 0)

            # Quiet days: no resolved positions AND no markets even
            # looked at. Send nothing.
            if resolved24 == 0 and cnt24 == 0:
                return

            # Win rate scoped to TODAY only - matches the "Today's
            # win rate" label in the message. Old all-time variant
            # was dropped in the 2026-05-26 message-shape rework
            # (too many different win-rate numbers in one message).
            win_pct_today = (wins24 / resolved24 * 100.0) if resolved24 else 0.0
            telegram_html = _tm.daily_summary(
                equity=equity,
                bankroll=bankroll,
                open_cost=open_cost,
                pnl_today=pnl24,
                win_pct_today=win_pct_today,
            )
            log_event(
                event_type="daily_summary",
                severity=10,
                description=(
                    f"Daily summary: {wins24}W/{losses24}L, "
                    f"P&L ${pnl24:+.2f}, {cnt24} markets analysed."
                ),
                source="main._run_daily_summary",
                telegram_html=telegram_html,
            )
            proc_health.record_job_ok("daily_summary")
        except Exception as exc:
            proc_health.record_job_error("daily_summary")
            print(f"[delfi] daily_summary failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_weekly_summary():
        """Weekly performance recap. Fires Sunday at 23:30 UTC.
        Same gating model as daily_summary: event_type='weekly_
        summary', so the toggle in Settings -> Notifications
        controls delivery.
        """
        try:
            from sqlalchemy import text
            from db.engine import get_engine
            from db.logger import log_event
            from execution.pm_executor import PMExecutor
            from engine.user_config import DEFAULT_USER_ID
            from feeds import telegram_messages as _tm

            executor = PMExecutor(DEFAULT_USER_ID)
            stats = executor.get_portfolio_stats()
            if not stats.get("ready"):
                return
            bankroll  = float(stats.get("bankroll", 0.0))
            open_cost = float(stats.get("open_cost", 0.0))
            equity    = float(stats.get("equity", bankroll + open_cost))

            eng = get_engine()
            with eng.connect() as conn:
                row7 = conn.execute(text(
                    "SELECT "
                    "  COUNT(*) AS resolved7, "
                    "  COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins7, "
                    "  COALESCE(SUM(CASE WHEN realized_pnl_usd < 0 THEN 1 ELSE 0 END), 0) AS losses7, "
                    "  COALESCE(SUM(realized_pnl_usd), 0) AS pnl7 "
                    "FROM pm_positions "
                    "WHERE user_id = :uid AND mode = 'live' "
                    "  AND status IN ('settled', 'closed_early') "
                    "  AND settled_at > datetime('now', '-7 days')"
                ), {"uid": DEFAULT_USER_ID}).fetchone()
                resolved7 = int(row7[0] or 0)
                wins7     = int(row7[1] or 0)
                losses7   = int(row7[2] or 0)
                pnl7      = float(row7[3] or 0.0)

            # Skip if the week was empty.
            if resolved7 == 0:
                return

            win_pct_week = (wins7 / resolved7 * 100.0) if resolved7 else 0.0
            telegram_html = _tm.weekly_summary(
                equity=equity,
                bankroll=bankroll,
                open_cost=open_cost,
                pnl_week=pnl7,
                win_pct_week=win_pct_week,
            )
            log_event(
                event_type="weekly_summary",
                severity=10,
                description=(
                    f"Weekly summary: {wins7}W/{losses7}L over 7d, "
                    f"P&L ${pnl7:+.2f}."
                ),
                source="main._run_weekly_summary",
                telegram_html=telegram_html,
            )
            proc_health.record_job_ok("weekly_summary")
        except Exception as exc:
            proc_health.record_job_error("weekly_summary")
            print(f"[delfi] weekly_summary failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_license_boot_claim():
        """Sync wrapper for the one-shot boot claim. See
        `_license_boot_claim_async` for the doctrine.
        """
        try:
            _submit_job(
                _bounded(
                    _license_boot_claim_async(),
                    timeout_s=20,
                    label="license_boot_claim",
                ),
                outer_timeout_s=30,
            )
        except Exception as exc:
            print(f"[delfi] license_boot_claim failed: {exc}",
                  file=sys.stderr, flush=True)

    def _run_license_revocation_check():
        """Daily call to the licensing server to see if this license
        has been revoked (refund, dispute, manual admin action).

        The local Ed25519 verifier in engine/license.py is offline-
        only and will keep validating a refunded customer's blob
        forever. Without this check, a refund doesn't actually stop
        trading until the next desktop release ships a different key.
        With it, a refund triggers a /api/license/check call within
        12h that clears the local license; the LicenseGate re-renders
        on next /api/license/status poll and the bot stops opening
        new positions.

        Soft on errors: a network blip or a server-side outage MUST
        NOT lock paying customers out. Only an explicit `revoked:
        true` response clears the license.
        """
        try:
            _submit_job(
                _bounded(
                    _license_revocation_check_async(),
                    timeout_s=20,
                    label="license_revocation_check",
                ),
                outer_timeout_s=30,
            )
        except Exception as exc:
            print(f"[delfi] license_revocation_check failed: {exc}",
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
        # First fire 90s after boot - give the position resolver and
        # market scan a head start so we're never racing them on the
        # first tick after a daemon restart.
        next_run_time=now_utc + timedelta(seconds=90),
        # coalesce=False mirrors `pm_resolve_fast` - missing a tick on
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
        # Fire 4 min after boot - one minute after the redeem sweeper,
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
        # First fire 3 minutes after boot - well after the position
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
    # Equity snapshot every 10 min. First fire 30s after boot so
    # pm_balance_refresh (fires at +1s) has time to warm the wallet
    # cache before we read it - otherwise the very first snapshot
    # would record bankroll=0 on a cold cache.
    scheduler.add_job(
        _run_equity_snapshot, IntervalTrigger(minutes=10),
        id="equity_snapshot",
        next_run_time=now_utc + timedelta(seconds=30),
        max_instances=1, coalesce=True,
        executor="threadpool",
    )
    # Daily summary at 23:00 UTC every day. Cron rather than
    # interval so the message lands at a consistent local clock
    # time across restarts. Skips its own send when the day had
    # no activity (see `_run_daily_summary`).
    scheduler.add_job(
        _run_daily_summary, CronTrigger(hour=23, minute=0),
        id="daily_summary",
        max_instances=1, coalesce=True,
        executor="threadpool",
        misfire_grace_time=3600,
    )
    # Weekly summary at 23:30 UTC every Sunday. Half-hour offset
    # from daily so they don't both fire at the same instant on
    # Sundays (small detail, but the daily would otherwise hit the
    # weekly's 7-day window in a way that conflates the two
    # message types in the user's Telegram history).
    scheduler.add_job(
        _run_weekly_summary, CronTrigger(day_of_week="sun", hour=23, minute=30),
        id="weekly_summary",
        max_instances=1, coalesce=True,
        executor="threadpool",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _run_license_revocation_check, IntervalTrigger(hours=12),
        id="license_revocation_check",
        # First fire 90s after boot - long enough that a network blip
        # at startup doesn't fight with macro-calendar / DDGS warmup,
        # short enough that a refunded customer who relaunches stops
        # trading within minutes rather than 12 hours.
        next_run_time=now_utc + timedelta(seconds=90),
        max_instances=1, coalesce=True,
        executor="threadpool",
    )
    # One-shot boot claim. Fires once at boot+30s and never again
    # (DateTrigger). Closes the upgrade-path gap: an already-activated
    # pre-v1.5.16 user who upgrades to v1.5.16 needs to populate the
    # server slot for their licence, but the desktop never re-calls
    # /api/license/activate on its own - the cached blob just verifies
    # offline forever. This task does the claim once per process.
    # If the slot is already held by another machine, the user gets
    # locked here (status="device_mismatch") instead of after the
    # 12h revocation tick.
    scheduler.add_job(
        _run_license_boot_claim,
        DateTrigger(run_date=now_utc + timedelta(seconds=30)),
        id="license_boot_claim",
        max_instances=1, coalesce=False,
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
