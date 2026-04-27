"""
Smoke-test the bundled Delfi sidecar binary.

Spawns ./src-tauri/binaries/delfi-sidecar-<triple>, waits for the
DELFI_LOCAL_API_READY <port> handshake on stdout, hits /api/health and
/api/state on the reported port, then terminates the child gracefully.

Run from Delfibot/bot/:

    ../../.venv/bin/python scripts/smoke_sidecar.py

Exits non-zero on any failure so CI can wire it up later.

Uses a fresh tempdir for the SQLite DB so the user's real DB is never
touched. Tempdir is cleaned up at the end via tempfile.TemporaryDirectory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def find_sidecar(bot_dir: Path) -> Path:
    binaries = bot_dir / "src-tauri" / "binaries"
    candidates = sorted(binaries.glob("delfi-sidecar-*"))
    if not candidates:
        sys.exit(f"[smoke] no sidecar binary in {binaries}")
    return candidates[0]


def main() -> int:
    bot_dir = Path(__file__).resolve().parent.parent
    sidecar = find_sidecar(bot_dir)
    print(f"[smoke] sidecar: {sidecar} ({sidecar.stat().st_size // (1024 * 1024)} MB)")

    with tempfile.TemporaryDirectory(prefix="delfi-smoke-") as tmpdir:
        db_path = os.path.join(tmpdir, "delfi-test.db")
        env = os.environ.copy()
        env["DELFI_DB_PATH"] = db_path

        print(f"[smoke] DELFI_DB_PATH={db_path}")
        proc = subprocess.Popen(
            [str(sidecar)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        port: int | None = None
        log_lines: list[str] = []
        deadline = time.time() + 120
        try:
            while time.time() < deadline:
                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                log_lines.append(line.rstrip())
                if line.startswith("DELFI_LOCAL_API_READY"):
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        port = int(parts[1])
                        break

            if port is None:
                print("[smoke] FAIL: no DELFI_LOCAL_API_READY within 30s")
                print("[smoke] last 30 lines of sidecar output:")
                for entry in log_lines[-30:]:
                    print(f"   {entry}")
                return 1

            print(f"[smoke] handshake OK on port {port}")

            # Hit a couple of read-only endpoints. Anything 5xx fails the test.
            for endpoint in ("/api/health", "/api/state", "/api/config"):
                url = f"http://127.0.0.1:{port}{endpoint}"
                try:
                    with urllib.request.urlopen(url, timeout=3) as resp:
                        status = resp.status
                        body = resp.read(2000)
                except Exception as exc:
                    print(f"[smoke] FAIL: {endpoint}: {exc}")
                    return 1
                try:
                    parsed = json.loads(body)
                except Exception:
                    print(f"[smoke] FAIL: {endpoint} returned non-JSON: {body[:120]!r}")
                    return 1
                if status != 200:
                    print(f"[smoke] FAIL: {endpoint} -> HTTP {status}: {parsed}")
                    return 1
                print(f"[smoke] {endpoint}: 200 OK ({len(body)} bytes)")

            return 0
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                    print("[smoke] sidecar exited cleanly")
                except subprocess.TimeoutExpired:
                    print("[smoke] sidecar did not exit on SIGTERM, killing")
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
