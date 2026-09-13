"""Self-rotation for the daemon's log files.

Under launchd the sidecar's stdout and stderr are redirected to
~/Library/Logs/Delfi/sidecar.log and sidecar.err by the LaunchAgent
plist, and main.py also tees every print into <app-data>/logs/
sidecar.log. launchd never rotates its files and the daemon runs for
weeks. Measured on 2026-09-13: 156 MB of sidecar.err plus 15 MB of
sidecar.log since May, and 53 MB in the tee file after two days.

The descriptors were opened with O_APPEND, so the daemon can shrink
its own logs safely: copy the last `keep_tail_bytes` to a sibling
`.prev` file, then `ftruncate` the live file to zero. Subsequent
writes land at the new end because O_APPEND re-seeks on every write.
stdout and stderr may share one file; callers key on (st_dev, st_ino).

No-op when the descriptors are pipes or terminals (Windows sidecar
spawned by the Tauri shell, dev runs from a terminal).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Optional

DEFAULT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_KEEP_TAIL_BYTES = 2 * 1024 * 1024


def _regular_file_identity(fd: int) -> Optional[tuple[int, int, int]]:
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (st.st_dev, st.st_ino, st.st_size)


def fd_path(fd: int) -> Optional[Path]:
    """Filesystem path behind `fd`, when the OS can tell us (macOS
    F_GETPATH, Linux /proc). None otherwise."""
    try:
        import fcntl
        getpath = getattr(fcntl, "F_GETPATH", None)
        if getpath is not None:
            raw = fcntl.fcntl(fd, getpath, b"\0" * 1024)
            path = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            return Path(path) if path else None
    except Exception:
        pass
    try:
        link = f"/proc/self/fd/{fd}"
        if os.path.exists(link):
            return Path(os.readlink(link))
    except Exception:
        pass
    return None


def rotate_fd_if_large(fd: int, *, max_bytes: int = DEFAULT_MAX_BYTES,
                       keep_tail_bytes: int = DEFAULT_KEEP_TAIL_BYTES,
                       prev_path: Optional[Path] = None,
                       src_path: Optional[Path] = None) -> bool:
    """Truncate the regular file behind `fd` when it exceeds `max_bytes`,
    preserving its tail in `prev_path`.

    The tail is read by PATH (`src_path`, or the path the OS reports for
    `fd`), not through the descriptor: launchd opens its log files
    write-only, so `os.pread` on fd 1/2 fails with EBADF. When no path
    is known the tail is not preserved. Returns True when a rotation
    happened."""
    ident = _regular_file_identity(fd)
    if ident is None:
        return False
    size = ident[2]
    if size <= max_bytes:
        return False
    try:
        path = src_path if src_path is not None else fd_path(fd)
        if prev_path is not None and keep_tail_bytes > 0 and path is not None:
            start = max(0, size - keep_tail_bytes)
            with open(path, "rb") as src:
                src.seek(start)
                tail = src.read(size - start)
            tmp = Path(str(prev_path) + ".tmp")
            with open(tmp, "wb") as fh:
                fh.write(tail)
            os.replace(tmp, prev_path)
        os.ftruncate(fd, 0)
    except OSError as exc:
        print(f"[log_rotation] rotation failed on fd {fd}: {exc}",
              file=sys.stderr, flush=True)
        return False
    return True


def rotate_stdio_logs(*, log_path_hint: Optional[Path] = None,
                      max_bytes: int = DEFAULT_MAX_BYTES,
                      keep_tail_bytes: int = DEFAULT_KEEP_TAIL_BYTES) -> int:
    """Rotate stdout and stderr when they are oversized regular files.

    The `.prev` tail goes next to the real file when the OS reports the
    descriptor's path; `log_path_hint` overrides that. Returns the
    number of descriptors rotated."""
    rotated = 0
    seen: set[tuple[int, int]] = set()
    for fd in (1, 2):
        ident = _regular_file_identity(fd)
        if ident is None:
            continue
        key = (ident[0], ident[1])
        if key in seen:
            continue
        seen.add(key)
        base = log_path_hint if log_path_hint is not None else fd_path(fd)
        prev = Path(str(base) + ".prev") if base is not None else None
        if rotate_fd_if_large(fd, max_bytes=max_bytes,
                              keep_tail_bytes=keep_tail_bytes, prev_path=prev,
                              src_path=base):
            rotated += 1
            print(f"[log_rotation] truncated log on fd {fd} "
                  f"(was {ident[2] // (1024 * 1024)} MB, kept last "
                  f"{keep_tail_bytes // (1024 * 1024)} MB in "
                  f"{prev.name if prev else 'nowhere'})",
                  file=sys.stderr, flush=True)
    return rotated
