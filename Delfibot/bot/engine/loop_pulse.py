"""Heartbeat for an asyncio loop that runs in a background thread.

The daemon runs two event loops: the aiohttp API loop (watched by
`engine.loop_watchdog.LoopHeartbeat`) and the persistent scheduler job
loop in `main._ensure_job_loop`. Until 2026-09-13 only the API loop had
a heartbeat, so a job loop wedged by a synchronous call was invisible:
/api/health kept reporting `loop_silence_s` of ~1 s while every
scheduled job died at its outer fence and the dashboard showed
"Market scan failed" on every tick.

`LoopPulse` re-arms itself with `loop.call_later` every `interval_s`
seconds. `silence_seconds()` grows while the loop cannot run
callbacks, so a watcher on another thread can tell a blocked loop
from an idle one. Thread-safe: `start()` may be called from any
thread; the pump runs on the loop thread.
"""

from __future__ import annotations

import asyncio
import threading
import time


class LoopPulse:
    def __init__(self, loop: asyncio.AbstractEventLoop, *,
                 interval_s: float = 5.0) -> None:
        self._loop = loop
        self._interval_s = float(interval_s)
        self._lock = threading.Lock()
        self._last_pump = time.monotonic()
        self._stopped = False
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._last_pump = time.monotonic()
        self._loop.call_soon_threadsafe(self._pump)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True

    def reset(self) -> None:
        """Forgive the current silence (sleep/wake clock jump)."""
        with self._lock:
            self._last_pump = time.monotonic()

    def _pump(self) -> None:
        with self._lock:
            self._last_pump = time.monotonic()
            stopped = self._stopped
        if not stopped and not self._loop.is_closed():
            self._loop.call_later(self._interval_s, self._pump)

    def silence_seconds(self) -> float:
        """Seconds since the loop last ran the pump. Healthy loops report
        under `interval_s`; a value several multiples higher means the
        loop thread is blocked in synchronous code."""
        with self._lock:
            if not self._started or self._stopped:
                return 0.0
            return time.monotonic() - self._last_pump
