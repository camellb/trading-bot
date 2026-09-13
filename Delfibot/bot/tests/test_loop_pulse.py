import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from engine.loop_pulse import LoopPulse


def _start_loop_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="pulse-test-loop", daemon=True)
    t.start()
    return loop, t


def test_pulse_tracks_blocked_loop() -> None:
    loop, thread = _start_loop_thread()
    try:
        pulse = LoopPulse(loop, interval_s=0.05)
        assert pulse.silence_seconds() == 0.0  # not started yet
        pulse.start()
        time.sleep(0.3)
        assert pulse.silence_seconds() < 0.2

        # Block the loop thread synchronously, the way a sync SQLite
        # write or CPU-bound parser does, and watch the silence grow.
        loop.call_soon_threadsafe(time.sleep, 0.6)
        time.sleep(0.45)
        assert pulse.silence_seconds() > 0.3

        # Once the blocking call returns the pump re-arms and recovers.
        time.sleep(0.4)
        assert pulse.silence_seconds() < 0.2

        pulse.stop()
        assert pulse.silence_seconds() == 0.0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_start_is_idempotent() -> None:
    loop, thread = _start_loop_thread()
    try:
        pulse = LoopPulse(loop, interval_s=0.05)
        pulse.start()
        pulse.start()
        time.sleep(0.2)
        assert pulse.silence_seconds() < 0.2
        pulse.stop()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
