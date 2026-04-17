"""
Feed Health Monitor — tracks state of the PM data feeds.

Feeds that flow through this module:
    polymarket — Gamma API scan, refreshed hourly.
    news       — RSS + Nitter + CryptoPanic poll, refreshed every NEWS_POLL_INTERVAL_MIN.
    macro      — macro calendar scraper, refreshed weekly.

Overlay feeds (news, macro) do not gate anything; they only surface as
warnings in Telegram. `polymarket` is the only core feed — if it fails
for an extended period we still don't halt trading (shadow mode has no
stop loss to protect), but we do raise a critical alert so the operator
can investigate.
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from db import logger as db_logger


# ── Staleness thresholds ─────────────────────────────────────────────────────
_STALE_THRESHOLDS: dict[str, Optional[timedelta]] = {
    "polymarket":  timedelta(hours=2),      # one missed scan is fine; two is a warning
    "news":        None,                    # state-only, no staleness check
    "macro":       None,                    # state-only, no staleness check
    "cryptopanic": None,                    # overlay feed
}

# The only feed whose failure the scheduler logs prominently.
_CORE_FEEDS = {"polymarket"}

_STARTUP_GRACE_S     = 120   # suppress feed alerts for 2 min after start
_DEGRADATION_ALERT_S = 60    # minimum continuous-degraded window before alerting


class FeedHealthMonitor:
    """Single-process health tracker. All feed modules call into this."""

    def __init__(self) -> None:
        self._state: dict[str, dict] = {}
        self._bot_start_time: datetime = datetime.now(timezone.utc)
        self._degradation_start:    dict[str, datetime] = {}
        self._notification_sent:    dict[str, bool]     = {}

    # ── Startup helpers ──────────────────────────────────────────────────────
    def _in_grace_period(self) -> bool:
        return (datetime.now(timezone.utc) - self._bot_start_time).total_seconds() < _STARTUP_GRACE_S

    def set_bot_start_time(self, ts: datetime) -> None:
        self._bot_start_time = ts

    # ── Registration ─────────────────────────────────────────────────────────
    def register(self, feed_name: str) -> None:
        self._state[feed_name] = {
            "last_update": None,
            "state":       "degraded",
            "detail":      "registered — awaiting first healthy report",
        }

    # ── Reporting ────────────────────────────────────────────────────────────
    def report_healthy(self, feed_name: str) -> None:
        if feed_name not in self._state:
            self.register(feed_name)
        prev = self._state[feed_name]["state"]
        self._state[feed_name]["state"]       = "healthy"
        self._state[feed_name]["last_update"] = datetime.now(timezone.utc)
        self._state[feed_name]["detail"]      = None

        if prev != "healthy":
            db_logger.log_feed_health(feed_name, "healthy", None)
            self._degradation_start.pop(feed_name, None)
            if not self._in_grace_period() and self._notification_sent.get(feed_name):
                self._notification_sent[feed_name] = False
                self._fire_telegram(self._notify_recovered, feed_name)

    def report_degraded(self, feed_name: str, detail: str,
                         expected: bool = False) -> None:
        if feed_name not in self._state:
            self.register(feed_name)
        prev = self._state[feed_name]["state"]
        self._state[feed_name]["state"]  = "degraded"
        self._state[feed_name]["detail"] = detail
        print(f"[feed_health] DEGRADED: {feed_name} — {detail}", file=sys.stderr)
        if prev != "degraded":
            db_logger.log_feed_health(feed_name, "degraded", detail)

        if expected or self._in_grace_period():
            return
        now = datetime.now(timezone.utc)
        if feed_name not in self._degradation_start:
            self._degradation_start[feed_name] = now
            return
        elapsed = (now - self._degradation_start[feed_name]).total_seconds()
        if elapsed < _DEGRADATION_ALERT_S:
            return
        self._degradation_start[feed_name] = now
        self._notification_sent[feed_name] = True
        self._fire_telegram(self._notify_degraded, feed_name, detail)

    def report_reconnecting(self, feed_name: str) -> None:
        if feed_name not in self._state:
            self.register(feed_name)
        self._state[feed_name]["state"]  = "reconnecting"
        self._state[feed_name]["detail"] = "reconnecting"
        print(f"[feed_health] RECONNECTING: {feed_name}", file=sys.stderr)

    # ── Telegram glue ────────────────────────────────────────────────────────
    @staticmethod
    def _fire_telegram(coro_fn, *args) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro_fn(*args))
        except RuntimeError:
            pass

    @staticmethod
    async def _notify_recovered(feed_name: str) -> None:
        from feeds.telegram_notifier import notifier
        await notifier.notify_feed_recovered(feed_name)

    @staticmethod
    async def _notify_degraded(feed_name: str, detail: str) -> None:
        from feeds.telegram_notifier import notifier
        await notifier.notify_feed_degraded(feed_name, detail)

    # ── Queries ──────────────────────────────────────────────────────────────
    def is_healthy(self, feed_name: str) -> bool:
        entry = self._state.get(feed_name)
        if not entry or entry["state"] != "healthy":
            return False
        threshold = _STALE_THRESHOLDS.get(feed_name)
        if threshold is None:
            return True
        last = entry.get("last_update")
        if last is None:
            return False
        return (datetime.now(timezone.utc) - last) <= threshold

    def are_core_feeds_healthy(self) -> bool:
        return all(self.is_healthy(feed) for feed in _CORE_FEEDS)

    def get_degraded_feeds(self) -> list[str]:
        return [
            name for name, entry in self._state.items()
            if entry["state"] != "healthy"
        ]

    def get_state(self, feed_name: str) -> dict:
        return dict(self._state.get(feed_name, {}))

    def snapshot(self) -> dict[str, dict]:
        return {name: dict(entry) for name, entry in self._state.items()}


# Module-level singleton.
monitor = FeedHealthMonitor()
