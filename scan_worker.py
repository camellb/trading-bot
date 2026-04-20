"""
Scan worker — runs the Polymarket scan pipeline in an isolated subprocess.

Why a subprocess?
    The scan involves heavy I/O and CPU work:
      - DuckDuckGo searches (ThreadPoolExecutor, network I/O)
      - Claude / Gemini API calls (ThreadPoolExecutor, HTTP)
      - trafilatura / lxml HTML parsing (C extension, holds the GIL)
      - aiohttp connection handling (C socket creation)

    When these run on the main event loop's process, GIL contention from
    C extensions (especially lxml) starves the event loop thread.  Timer
    callbacks (heartbeat, watchdog, scheduler) stop firing even though
    the process is alive.  The watchdog kills the process, launchd
    restarts it, and the cycle repeats every scan interval.

    Running in a subprocess gives the scan its own GIL, event loop, and
    thread pools.  The main process stays responsive: heartbeat ticks,
    watchdog checks, API server, scheduler — all unaffected.

Usage:
    python scan_worker.py <limit> <min_volume_24h> [<timeout_seconds>]

    JSON result   -> stdout  (the ONLY thing on stdout)
    Log output    -> stderr  (inherited or captured by parent)
"""

import sys
import os

# ── Redirect print() to stderr BEFORE any imports ────────────────────────
# Scan modules use print() liberally for logging.  If those messages land
# on stdout they corrupt the JSON result the parent process reads.
# We save the real stdout file object, then point sys.stdout at stderr.
_result_pipe = sys.stdout
sys.stdout = sys.stderr

# ── Now safe to import everything ─────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(override=True)

import asyncio
import json
import signal
import traceback

import config
from engine.memory import MemoryManager
from engine.pm_analyst import PMAnalyst
from polymarket_runner import scan_and_analyze


async def _run(limit: int, min_volume: float, max_seconds: int = 0) -> dict:
    """Run the full scan pipeline: reconcile -> fetch candidates -> analyse."""
    memory = MemoryManager()
    analyst = PMAnalyst(memory=memory)

    # Increase the thread pool — same as main.py does.
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=20, thread_name_prefix="scan")
    )

    return await scan_and_analyze(
        limit=limit,
        min_volume_24h=min_volume,
        memory=memory,
        analyst=analyst,
        max_seconds=max_seconds,
    )


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    min_volume = float(sys.argv[2]) if len(sys.argv) > 2 else 10_000.0
    max_seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    # Clean shutdown on SIGTERM (parent kills us on timeout).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"[scan_worker] starting: limit={limit} min_volume={min_volume}"
          f" budget={max_seconds}s", flush=True)

    try:
        summary = asyncio.run(_run(limit, min_volume, max_seconds))
    except SystemExit:
        summary = {"error": "killed"}
    except Exception:
        traceback.print_exc()  # goes to stderr
        summary = {"error": "crashed", "detail": traceback.format_exc()[-500:]}

    # Write result JSON to the ORIGINAL stdout (the pipe to the parent).
    _result_pipe.write(json.dumps(summary, default=str))
    _result_pipe.flush()

    print(f"[scan_worker] done: opened={summary.get('opened', '?')} "
          f"errors={summary.get('errors', '?')}", flush=True)

    # Hard exit: asyncio.run() cleanup hangs if orphaned executor threads
    # are still running (e.g. a Gemini/Claude API call that timed out via
    # _timeout_guard but the thread is still blocked on the HTTP response).
    # os._exit() skips interpreter cleanup — safe here because we've
    # already flushed our result JSON and log output above.
    os._exit(0)


if __name__ == "__main__":
    main()
