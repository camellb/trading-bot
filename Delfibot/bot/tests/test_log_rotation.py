import os
import sys
from pathlib import Path

import pytest

needs_fd_path = pytest.mark.skipif(
    sys.platform == "win32",
    reason="tail copy needs a readable path for the fd (F_GETPATH or /proc)",
)

sys.path.insert(0, str(Path(__file__).parents[1]))

from engine import log_rotation


@needs_fd_path
def test_rotate_fd_truncates_and_keeps_tail(tmp_path) -> None:
    log = tmp_path / "sidecar.log"
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, b"a" * 1000 + b"TAIL-MARKER\n")
        assert not log_rotation.rotate_fd_if_large(
            fd, max_bytes=10_000, keep_tail_bytes=100, prev_path=tmp_path / "sidecar.log.prev",
        )
        assert log.stat().st_size == 1012

        rotated = log_rotation.rotate_fd_if_large(
            fd, max_bytes=500, keep_tail_bytes=100, prev_path=tmp_path / "sidecar.log.prev",
        )
        assert rotated
        assert log.stat().st_size == 0
        prev = (tmp_path / "sidecar.log.prev").read_bytes()
        assert len(prev) == 100
        assert prev.endswith(b"TAIL-MARKER\n")

        # O_APPEND: later writes land at the new start of the file.
        os.write(fd, b"after\n")
        assert log.read_bytes() == b"after\n"
    finally:
        os.close(fd)


def test_rotate_ignores_pipes_and_terminals() -> None:
    r, w = os.pipe()
    try:
        assert not log_rotation.rotate_fd_if_large(w, max_bytes=0, keep_tail_bytes=0)
    finally:
        os.close(r)
        os.close(w)


def test_fd_path_resolves_regular_file(tmp_path) -> None:
    log = tmp_path / "sidecar.err"
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        resolved = log_rotation.fd_path(fd)
        # macOS (F_GETPATH) and Linux (/proc) both resolve; other
        # platforms may return None, which callers treat as "no .prev".
        assert resolved is None or resolved.resolve() == log.resolve()
    finally:
        os.close(fd)


@needs_fd_path
def test_rotate_stdio_dedupes_shared_file(tmp_path, monkeypatch) -> None:
    log = tmp_path / "sidecar.log"
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    saved = (os.dup(1), os.dup(2))
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.write(1, b"x" * 2048)
        n = log_rotation.rotate_stdio_logs(log_path_hint=log, max_bytes=1024, keep_tail_bytes=64)
        assert n == 1  # both fds point at one inode: rotated once
        assert (tmp_path / "sidecar.log.prev").stat().st_size == 64
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])
        os.close(fd)
