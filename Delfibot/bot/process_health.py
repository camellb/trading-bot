"""
Process-level health tracking - singleton shared by main.py and bot_api.

Records timestamps for scheduled job completions and error counts so the
/api/health endpoint can report operational state.
"""

from datetime import datetime, timezone
from typing import Optional
import threading


class ProcessHealth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job_last_ok: dict[str, datetime] = {}
        self._job_last_error: dict[str, datetime] = {}
        self._job_last_blocked: dict[str, tuple[datetime, str]] = {}
        self._error_count: int = 0
        self._bot_start_time: Optional[datetime] = None
        # Monotonic sequence per recorded event. Windows' wall clock
        # ticks every ~15 ms, so record_job_ok() followed by
        # record_job_blocked() in the same tick produced equal
        # timestamps and the max() tie went to the wrong state.
        self._seq: int = 0
        self._job_seq: dict[tuple[str, str], int] = {}

    def _stamp(self, job_name: str, state: str) -> datetime:
        self._seq += 1
        self._job_seq[(job_name, state)] = self._seq
        return datetime.now(timezone.utc)

    def set_start_time(self, t: datetime) -> None:
        self._bot_start_time = t

    @property
    def start_time(self) -> Optional[datetime]:
        return self._bot_start_time

    @property
    def uptime_seconds(self) -> float:
        if self._bot_start_time is None:
            return 0.0
        return (datetime.now(timezone.utc) - self._bot_start_time).total_seconds()

    def record_job_ok(self, job_name: str) -> None:
        with self._lock:
            self._job_last_ok[job_name] = self._stamp(job_name, "ok")

    def record_job_error(self, job_name: str) -> None:
        with self._lock:
            self._job_last_error[job_name] = self._stamp(job_name, "error")
            self._error_count += 1

    def record_job_blocked(self, job_name: str, reason: str) -> None:
        with self._lock:
            self._job_last_blocked[job_name] = (self._stamp(job_name, "blocked"), reason)

    @property
    def error_count(self) -> int:
        return self._error_count

    def last_ok(self, job_name: str) -> Optional[datetime]:
        with self._lock:
            return self._job_last_ok.get(job_name)

    def snapshot(self) -> dict:
        with self._lock:
            all_jobs = sorted(
                set(self._job_last_ok)
                | set(self._job_last_error)
                | set(self._job_last_blocked)
            )
            jobs = {}
            for name in all_jobs:
                ok_ts = self._job_last_ok.get(name)
                err_ts = self._job_last_error.get(name)
                blocked = self._job_last_blocked.get(name)
                blocked_ts = blocked[0] if blocked else None
                candidates = [
                    (ok_ts, self._job_seq.get((name, "ok"), 0), "ok"),
                    (err_ts, self._job_seq.get((name, "error"), 0), "error"),
                    (blocked_ts, self._job_seq.get((name, "blocked"), 0), "blocked"),
                ]
                state = max(
                    ((ts, seq, value) for ts, seq, value in candidates if ts),
                    default=(None, 0, "unknown"),
                    key=lambda item: (item[0], item[1]),
                )[2]
                jobs[name] = {
                    "state": state,
                    "last_ok": ok_ts.isoformat() if ok_ts else None,
                    "last_error": err_ts.isoformat() if err_ts else None,
                    "last_blocked": blocked_ts.isoformat() if blocked_ts else None,
                    "blocked_reason": blocked[1] if blocked else None,
                }
            return {
                "uptime_s": self.uptime_seconds,
                "started_at": (self._bot_start_time.isoformat()
                               if self._bot_start_time else None),
                "error_count": self._error_count,
                "jobs": jobs,
            }


health = ProcessHealth()
