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


def _dump_asyncio_tasks() -> None:
    """Print every pending asyncio task's traceback to stderr.

    Runs on the asyncio loop thread (call_soon_threadsafe schedules
    it). faulthandler can only walk thread frames; pending tasks
    aren't thread-attached. This fills the gap.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    try:
        tasks = list(asyncio.all_tasks(loop))
    except Exception:
        return
    print(
        f"[watchdog] {len(tasks)} pending asyncio tasks at warning time:",
        file=sys.stderr, flush=True,
    )
    for i, t in enumerate(tasks, 1):
        try:
            coro_name = getattr(t.get_coro(), "__qualname__", "?")
            print(
                f"  [task {i}/{len(tasks)}] {coro_name} done={t.done()}",
                file=sys.stderr, flush=True,
            )
            t.print_stack(file=sys.stderr)
        except Exception as exc:
            print(f"  [task {i}] dump failed: {exc!r}",
                  file=sys.stderr, flush=True)


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
        api_port_getter=None,
        # Self-probe cadence + tolerance. Tightened 2026-05-20 after a
        # user-visible wedge that left the GUI in a "timed out" state
        # for ~2 min before the old 30s/3-failure config could
        # SIGKILL+respawn. 10s/2-failure means wedge -> recovery in
        # ~30s, which is faster than the user can articulate "it's
        # broken again".
        self_probe_interval_s: float = 10.0,
        self_probe_timeout_s: float = 5.0,
        self_probe_max_failures: int = 2,
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
        # Self-probe: detects the "loop alive but accept stopped"
        # wedge. The asyncio loop can be in selectors.select (idle,
        # heartbeat happily firing) while aiohttp's listener has
        # stalled - new TCP connects time out, GUI sees "Delfi could
        # not start". The heartbeat alone misses this because the
        # loop IS pumping. The self-probe makes an actual HTTP
        # request to /api/health from the watchdog thread (NOT the
        # asyncio loop). After self_probe_max_failures consecutive
        # timeouts, we SIGKILL self and let launchd respawn.
        self._api_port_getter = api_port_getter
        self._self_probe_interval_s = self_probe_interval_s
        self._self_probe_timeout_s = self_probe_timeout_s
        self._self_probe_max_failures = self_probe_max_failures
        self._self_probe_failures = 0
        self._self_probe_last_attempt = time.monotonic()

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
                self._abort(silence, reason="loop silent")
                return
            if silence > self._early_warning_s and not self._warned_for_period:
                self._warn(silence)
                self._warned_for_period = True
            # Self-probe: catches the "loop alive but accept stopped"
            # wedge that the heartbeat alone misses.
            now = time.monotonic()
            if (self._api_port_getter is not None
                    and now - self._self_probe_last_attempt
                        >= self._self_probe_interval_s):
                self._self_probe_last_attempt = now
                ok = self._self_probe()
                # Secondary signal: count CLOSE_WAIT + FIN_WAIT_2
                # sockets on our listen port. 2026-05-14 we hit a
                # wedge where the in-process probe reported ok=True
                # for hours while external clients timed out and
                # CLOSE_WAIT piled up past 30+30. The leak signature
                # is forensically obvious and catches the wedge even
                # when the probe gets fooled. Threshold = 40 is well
                # above normal (the Tauri GUI typically holds <10
                # connections) but well below kernel SOMAXCONN.
                leaked = self._count_leaked_sockets()
                print(
                    f"[watchdog] self-probe ok={ok} "
                    f"port={self._api_port_getter()} "
                    f"failures={self._self_probe_failures} "
                    f"leaked_sockets={leaked}",
                    file=sys.stderr, flush=True,
                )
                if leaked >= 15:
                    self._abort(
                        silence,
                        reason=(
                            f"{leaked} CLOSE_WAIT/FIN_WAIT_2/CLOSED "
                            "sockets on listen port (handler-cleanup "
                            "wedge - GUI requests will start timing "
                            "out before this clears on its own)"
                        ),
                    )
                    return
                if ok:
                    self._self_probe_failures = 0
                else:
                    self._self_probe_failures += 1
                    if self._self_probe_failures >= self._self_probe_max_failures:
                        self._abort(
                            silence,
                            reason=(
                                f"{self._self_probe_failures} consecutive "
                                "/api/health probe timeouts (listener wedged)"
                            ),
                        )
                        return

    def _count_leaked_sockets(self) -> int:
        """Count stuck-cleanup sockets on our listen port.

        Three states qualify:
          - CLOSE_WAIT: peer sent FIN, daemon hasn't called close()
          - FIN_WAIT_2: daemon sent FIN, waiting for peer's FIN+ACK
          - CLOSED:     full four-way handshake done but FD is still
                        in the process's fd table - this is the
                        signature 2026-05-20's wedge produced (lsof
                        showed 6 sockets in CLOSED state on the
                        daemon side while WebKit's new SYNs piled up
                        in SYN_SENT because the listen backlog was
                        full)

        Threshold lowered from 40 to 15: even 15 leaked sockets is
        well past normal (Tauri GUI typically holds <10) and is
        already enough to fill the OS-level accept queue and start
        dropping new connections. SIGKILLing earlier means recovery
        completes before the user can articulate "it's broken".
        """
        try:
            port = self._api_port_getter() if self._api_port_getter else None
        except Exception:
            return 0
        if not port or port <= 0:
            return 0
        import subprocess
        try:
            r = subprocess.run(
                ["/usr/sbin/netstat", "-an", "-p", "tcp"],
                capture_output=True, timeout=5,
            )
        except Exception:
            return 0
        if r.returncode != 0:
            return 0
        port_str = f".{port} "
        count = 0
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if port_str not in line:
                continue
            if ("CLOSE_WAIT" in line
                    or "FIN_WAIT_2" in line
                    or "CLOSED" in line):
                count += 1
        return count

    def _self_probe(self) -> bool:
        """Probe /api/health via a subprocess curl, not in-process urllib.

        Why out-of-process: we hit an "asyncio listener wedged"
        pattern repeatedly during 2026-05-14's incidents where the
        daemon's TCP accept loop stopped draining, but an
        in-process urllib.request.urlopen call from this thread to
        127.0.0.1 STILL succeeded (probably via a kernel loopback
        fast-path tied to the same process). The watchdog kept
        reporting ok=True while every external client (the Tauri
        GUI, our curl probes) timed out. So we never SIGKILL'd, and
        the user saw an unrecoverable wedge.

        Forking curl puts the probe in a separate process. The
        connect goes through the same path external clients use, so
        if external clients can't reach the listener neither can
        this probe — exactly the signal we want.
        """
        try:
            port = self._api_port_getter() if self._api_port_getter else None
        except Exception:
            return False
        if not port or port <= 0:
            return False

        import subprocess
        # Hard-coded curl path: avoids $PATH lookup overhead and
        # surprise if /usr/local/bin/curl shadows the system one.
        # Macs always have /usr/bin/curl; Linux has it too.
        cmd = [
            "/usr/bin/curl", "-sS",
            "--max-time", str(int(self._self_probe_timeout_s)),
            "-o", "/dev/null",
            "-w", "%{http_code}",
            f"http://127.0.0.1:{port}/api/health",
        ]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                # +2s envelope on top of curl's own --max-time so the
                # subprocess.run timeout fires only if curl itself is
                # hung (extremely rare). curl's timeout is what we
                # want to detect a wedge.
                timeout=int(self._self_probe_timeout_s) + 2,
            )
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False
        if r.returncode != 0:
            return False
        return r.stdout.strip() == b"200"

    def _warn(self, silence: float) -> None:
        """Slow-but-not-dead: dump tracebacks WITHOUT killing.

        Most wedges develop over seconds, not instantly. By dumping at
        30s silence we capture the live frame of whatever sync code is
        blocking the loop while the daemon is still alive and the user
        can recover. If the silence keeps growing past max_silence_s,
        _abort takes over.

        Also schedules a dump of every pending asyncio task on the
        loop. faulthandler only sees thread frames, but asyncio
        handlers all share the main thread and are invisible to it.
        If the wedge is "loop alive in selectors.select but listener
        stopped accepting" (CLOSE_WAIT leak pattern observed
        2026-05-06), the task dump is what tells us which await is
        stuck.
        """
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
        # Schedule a task dump on the loop. If the loop is fully
        # wedged this never fires; that's fine - the thread dump
        # already captured the wedge frame. If the loop is just slow
        # the call eventually runs and dumps every awaiting handler.
        try:
            self._loop.call_soon_threadsafe(_dump_asyncio_tasks)
        except Exception:
            pass

    def _abort(self, silence: float, reason: str = "loop silent") -> None:
        msg = (
            f"[watchdog] aborting: {reason} "
            f"(silence={silence:.0f}s, max_silence={self._max_silence_s:.0f}s). "
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
