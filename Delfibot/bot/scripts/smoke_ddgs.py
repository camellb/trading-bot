"""
Smoke-test the DDGS pre-warm fix in research/fetcher.py.

Spawns 4 threads concurrently inside the same process, each calling
DDGS() and running a trivial search. With the pre-warm wired into
research/fetcher.py the first DDGS() call is single-threaded at module
import time, so all 4 workers should return well within the deadline.
Without the pre-warm, ddgs's lazy-load races and the workers deadlock
indefinitely (observed: never returns; SIGUSR1 thread dump shows all 4
stuck inside ddgs/__init__.py:_load_real and ddgs/ddgs.py:_get_engines).

Run from Delfibot/bot/:

    ../../.venv/bin/python scripts/smoke_ddgs.py

Exits non-zero on any failure so CI can wire it up later.
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FT
from pathlib import Path

# Ensure the bot package root is on sys.path when invoked directly.
_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

# Import side-effect: pre-warms DDGS in research.fetcher's module body.
from research import fetcher  # noqa: E402

if not fetcher._DDGS_AVAILABLE:
    print("[smoke-ddgs] SKIP: ddgs library not installed")
    sys.exit(0)

from ddgs import DDGS  # noqa: E402


def _worker(idx: int) -> tuple[int, float, int]:
    t0 = time.monotonic()
    with DDGS() as ddg:
        results = list(
            ddg.text(
                f"polymarket prediction market test {idx}",
                max_results=3,
                region="wt-wt",
            )
            or []
        )
    return idx, time.monotonic() - t0, len(results)


def main() -> int:
    deadline_secs = float(os.environ.get("DELFI_DDGS_SMOKE_TIMEOUT", "30"))
    print(f"[smoke-ddgs] launching 4 threads, per-worker deadline "
          f"{deadline_secs}s")

    t0 = time.monotonic()
    failures = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_worker, i) for i in range(4)]
        for f in futures:
            try:
                idx, dt, n = f.result(timeout=deadline_secs)
                print(f"[smoke-ddgs] worker {idx}: {dt:.2f}s, {n} results")
            except _FT:
                # The deadlock manifests as a hung worker. Don't wait for
                # the others, just report.
                print(f"[smoke-ddgs] FAIL: worker hung past {deadline_secs}s "
                      f"(deadlock symptom)")
                failures += 1
            except Exception as exc:
                print(f"[smoke-ddgs] FAIL: worker raised: {exc!r}")
                failures += 1

    total = time.monotonic() - t0
    if failures:
        print(f"[smoke-ddgs] FAIL: {failures}/4 worker(s) failed in {total:.2f}s")
        return 1
    print(f"[smoke-ddgs] PASS: all 4 workers completed in {total:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
