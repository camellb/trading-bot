"""
Asyncio event-loop watchdog.

Why this exists
===============
The sidecar is a launchd-managed daemon (LaunchAgent at
~/Library/LaunchAgents/com.delfi.bot.plist). launchd's `KeepAlive`
auto-restarts the process on a non-zero exit, but it has no signal
that the process is *alive but useless*: an asyncio loop that has
been wedged by a sync call still keeps the parent process running,
so KeepAlive never fires. From the user's POV the GUI shows
"Delfi could not start" / "/api/credentials timed out after 30s"
indefinitely.

The fix is a heartbeat. The asyncio loop schedules a callback that
updates a monotonic timestamp every few seconds. A separate daemon
thread (NOT subject to the asyncio scheduler) polls that timestamp.
If the gap exceeds `max_silence_s`, the watchdog dumps Python
tracebacks via `faulthandler.dump_traceback` (so the next run can
diagnose the wedge from `sidecar.err`) and SIGKILL's the process.
launchd then respawns within `ThrottleInterval` seconds (10s) and
the GUI's port-refresh logic reconnects without user action.

Why a thread, not another asyncio task: an asyncio task to monitor
the asyncio loop is exactly as wedged as the loop it's monitoring.
A POSIX thread runs independently of the loop and can't be blocked
by sync work happening elsewhere in the process.

Threshold tuning
================
`heartbeat_interval_s = 5` and `max_silence_s = 120` together mean
we tolerate up to 24 missed pumps before exiting. That's deliberately
loose: legitimate sync work (PyInstaller cold-start, large SQLite
checkpoint, slow keychain prompt) can stall the loop briefly. A
120s threshold is well past anything legitimate but well short of
the 5-hour wedge observed 2026-05-06.
"""

from __future__ import annotations

import asyncio
import faulthandler
import os
import signal
import sys
import threading
import time


class LoopHeartbeat:
    """Detects asyncio event-loop wedges and SIGKILL's the process.

    Usage:
        wd = LoopHeartbeat(loop)
        wd.start()  # call once after the loop is running
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        heartbeat_interval_s: float = 5.0,
        early_warning_s: float = 30.0,
        max_silence_s: float = 120.0,
        check_interval_s: float = 5.0,
    ) -> None:
        self._loop = loop
        self._heartbeat_interval_s = heartbeat_interval_s
        self._early_warning_s = early_warning_s
        self._max_silence_s = max_silence_s
        self._check_interval_s = check_interval_s
        self._last_pump = time.monotonic()
        # Track whether we've already emitted an early-warning dump for
        # the current "slow period". Reset when the loop pumps again.
        self._warned_for_period = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def silence_seconds(self) -> float:
        """How long since the last loop pump (in seconds).

        Surfaced via /api/health so the dashboard / external probes can
        measure how close we are to a wedge. A healthy loop pumps every
        ~5s, so silence > 10s is already suspicious.
        """
        return time.monotonic() - self._last_pump

    def _pump(self) -> None:
        self._last_pump = time.monotonic()
        # Loop is alive again - reset the warning gate so the next slow
        # period can produce a fresh early-warning dump.
        self._warned_for_period = False
        if not self._stop.is_set():
            self._loop.call_later(self._heartbeat_interval_s, self._pump)

    def _watch(self) -> None:
        while not self._stop.is_set():
            time.sleep(self._check_interval_s)
            silence = time.monotonic() - self._last_pump
            if silence > self._max_silence_s:
                self._abort(silence)
                return
            if silence > self._early_warning_s and not self._warned_for_period:
                self._warn(silence)
                self._warned_for_period = True

    def _warn(self, silence: float) -> None:
        """Slow-but-not-dead: dump tracebacks WITHOUT killing.

        Most wedges develop over seconds, not instantly. By dumping at
        30s silence we capture the live frame of whatever sync code is
        blocking the loop while the daemon is still alive and the user
        can recover. If the silence keeps growing past max_silence_s,
        _abort takes over."""
        try:
            print(
                f"[watchdog] event loop silent for {silence:.0f}s "
                f"(early warning at {self._early_warning_s:.0f}s). "
                "Dumping tracebacks; daemon still running.",
                file=sys.stderr, flush=True,
            )
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except Exception:
            pass

    def _abort(self, silence: float) -> None:
        msg = (
            f"[watchdog] event loop silent for {silence:.0f}s "
            f"(threshold {self._max_silence_s:.0f}s). "
            "Dumping Python tracebacks then exiting; launchd will respawn."
        )
        try:
            print(msg, file=sys.stderr, flush=True)
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGKILL)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop.call_soon(self._pump)
        t = threading.Thread(
            target=self._watch,
            name="delfi-loop-watchdog",
            daemon=True,
        )
        t.start()
        self._thread = t

    def stop(self) -> None:
        self._stop.set()
