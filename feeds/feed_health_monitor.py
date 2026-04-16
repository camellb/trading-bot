"""
Feed Health Monitor — tracks health state of all data feeds.

All feed modules import the module-level singleton `monitor` and call:
  monitor.report_healthy(feed_name)
  monitor.report_degraded(feed_name, detail)
  monitor.report_reconnecting(feed_name)

The regime classifier and Layer D call:
  monitor.are_core_feeds_healthy()  → True only if kline+ticker+markprice+orderbook healthy
  monitor.is_healthy(feed_name)     → True if state=="healthy" and not stale
  monitor.get_degraded_feeds()      → list of feed names not in "healthy" state

News and macro feeds do NOT block core health — they are overlay feeds.
Degradation of news/macro triggers the NEWS_DEGRADED_SIZE_MULTIPLIER exception
(50% size), not NO_TRADE.
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import config
from db import logger as db_logger

# Staleness thresholds per feed name
_STALE_THRESHOLDS: dict[str, Optional[timedelta]] = {
    "kline": timedelta(minutes=config.KLINE_STALE_THRESHOLD_MIN),
    "ticker": timedelta(seconds=config.TICKER_STALE_THRESHOLD_S),
    "markprice": timedelta(seconds=config.MARKPRICE_STALE_THRESHOLD_S),
    "orderbook": timedelta(seconds=config.TICKER_STALE_THRESHOLD_S),
    "news":        None,  # no staleness check — just state
    "macro":       None,  # no staleness check — just state
    "cryptopanic": None,  # overlay feed — no staleness check, not a core feed
    "deribit":     None,  # overlay feed — no staleness check, not a core feed
}

# Core feeds that must all be healthy for are_core_feeds_healthy()
_CORE_FEEDS = {"kline", "ticker", "markprice", "orderbook"}

# Notification thresholds
_STARTUP_GRACE_S = 120       # suppress all feed notifications for 2 min after start
_DEGRADATION_ALERT_S = 60    # only alert when feed has been degraded this long


class FeedHealthMonitor:
    """Thread-safe health tracker for all data feeds."""

    def __init__(self) -> None:
        self._state: dict[str, dict] = {}
        # Telegram notification state — all logic gated here, not in notifier
        self._bot_start_time: datetime = datetime.now(timezone.utc)
        self._degradation_start: dict[str, datetime] = {}  # feed → when degradation began
        self._notification_sent: dict[str, bool] = {}       # feed → alert was sent
        # Mass-degradation detection (network interruption vs individual feed failures)
        self._core_degraded_at: dict[str, datetime] = {}   # core feed → when it first went degraded
        self._last_mass_degradation_ts: Optional[datetime] = None  # set when all core feeds drop within 5s

    def get_last_mass_degradation_ts(self) -> Optional[datetime]:
        """Return the timestamp of the most recent mass-degradation event, or None."""
        return self._last_mass_degradation_ts

    def _in_grace_period(self) -> bool:
        """True during the first _STARTUP_GRACE_S seconds after bot start."""
        return (
            datetime.now(timezone.utc) - self._bot_start_time
        ).total_seconds() < _STARTUP_GRACE_S

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, feed_name: str) -> None:
        """Register a feed. Initial state is 'degraded' until first healthy report."""
        self._state[feed_name] = {
            "last_update": None,
            "state": "degraded",
            "detail": "registered — awaiting first healthy report",
        }

    # ── Reporting ─────────────────────────────────────────────────────────────

    def report_healthy(self, feed_name: str) -> None:
        """Mark feed healthy and update last_update to now."""
        if feed_name not in self._state:
            self.register(feed_name)
        prev_state = self._state[feed_name]["state"]
        self._state[feed_name]["state"] = "healthy"
        self._state[feed_name]["last_update"] = datetime.now(timezone.utc)
        self._state[feed_name]["detail"] = None

        if prev_state != "healthy":
            # Write healthy state to DB so the dashboard reflects the current state.
            # This covers transitions from both "degraded" and "reconnecting".
            # report_degraded() only writes on degraded transitions; we mirror
            # that here so the latest DB row per feed always shows current state.
            db_logger.log_feed_health(feed_name, "healthy", None)

            # Clear degradation timer and mass-degradation tracker for this feed
            self._degradation_start.pop(feed_name, None)
            self._core_degraded_at.pop(feed_name, None)

            # Only send recovery notification if a degradation alert was sent
            # (prevents orphaned recovery messages during grace period or fast flaps)
            if (
                not self._in_grace_period()
                and self._notification_sent.get(feed_name, False)
            ):
                self._notification_sent[feed_name] = False
                self._fire_telegram(self._notify_recovered, feed_name)

    @staticmethod
    def _fire_telegram(coro_fn, *args) -> None:
        """Schedule a Telegram coroutine as a background asyncio task.
        Safe to call from sync code running inside an async event loop."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro_fn(*args))
        except RuntimeError:
            pass  # No running event loop (e.g. unit tests)

    @staticmethod
    async def _notify_recovered(feed_name: str) -> None:
        from feeds.telegram_notifier import notifier
        await notifier.notify_feed_recovered(feed_name)

    _MASS_DEGRADATION_WINDOW_S = 5.0   # all core feeds must drop within this window

    def report_degraded(self, feed_name: str, detail: str) -> None:
        """
        Mark feed degraded. Log to DB. Send Telegram alert only when:
          1. Outside the 120s startup grace period.
          2. Feed has been continuously degraded for > 60 seconds.
          3. A notification has not already been sent for this degradation window.

        Also detects mass-degradation: if all core feeds go degraded within
        _MASS_DEGRADATION_WINDOW_S seconds of each other, this is treated as a
        network interruption. _last_mass_degradation_ts is set so that the OKX
        WebSocket reconnect loops can skip their per-stream delay and reconnect
        immediately.
        """
        if feed_name not in self._state:
            self.register(feed_name)
        prev_state = self._state[feed_name]["state"]
        self._state[feed_name]["state"] = "degraded"
        self._state[feed_name]["detail"] = detail
        print(f"[feed_health] DEGRADED: {feed_name} — {detail}", file=sys.stderr)
        # Only write to DB on state transitions to avoid flooding
        if prev_state != "degraded":
            db_logger.log_feed_health(feed_name, "degraded", detail)

        # ── Mass-degradation detection ─────────────────────────────────────
        if feed_name in _CORE_FEEDS and prev_state != "degraded":
            now = datetime.now(timezone.utc)
            self._core_degraded_at[feed_name] = now
            if len(self._core_degraded_at) == len(_CORE_FEEDS):
                times = list(self._core_degraded_at.values())
                window_s = (max(times) - min(times)).total_seconds()
                if window_s <= self._MASS_DEGRADATION_WINDOW_S:
                    self._last_mass_degradation_ts = now
                    msg = (
                        f"Network interruption: all core feeds degraded "
                        f"within {window_s:.1f}s"
                    )
                    print(f"[feed_health] NETWORK INTERRUPTION — {msg}", file=sys.stderr)
                    db_logger.log_feed_health("network", "degraded", msg)

        # ── Notification gating ────────────────────────────────────────────
        # Gate 1: startup grace period
        if self._in_grace_period():
            return

        now = datetime.now(timezone.utc)

        # Gate 2: start degradation timer on first call; don't alert yet
        if feed_name not in self._degradation_start:
            self._degradation_start[feed_name] = now
            return

        # Gate 3: threshold check
        elapsed = (now - self._degradation_start[feed_name]).total_seconds()
        if elapsed < _DEGRADATION_ALERT_S:
            return  # Not degraded long enough yet

        # Gates passed — send alert, reset timer to prevent repeat spam
        self._degradation_start[feed_name] = now
        self._notification_sent[feed_name] = True
        self._fire_telegram(self._notify_degraded, feed_name, detail)

    @staticmethod
    async def _notify_degraded(feed_name: str, detail: str) -> None:
        from feeds.telegram_notifier import notifier
        await notifier.notify_feed_degraded(feed_name, detail)

    def report_reconnecting(self, feed_name: str) -> None:
        """Mark feed as reconnecting."""
        if feed_name not in self._state:
            self.register(feed_name)
        self._state[feed_name]["state"] = "reconnecting"
        self._state[feed_name]["detail"] = "reconnecting"
        print(f"[feed_health] RECONNECTING: {feed_name}", file=sys.stderr)

    # ── Queries ───────────────────────────────────────────────────────────────

    def is_healthy(self, feed_name: str) -> bool:
        """
        Return True only if:
          - state == "healthy"
          - last_update is within the feed-specific staleness threshold
            (feeds with no staleness threshold only require state == "healthy")
        """
        entry = self._state.get(feed_name)
        if not entry or entry["state"] != "healthy":
            return False

        threshold = _STALE_THRESHOLDS.get(feed_name)
        if threshold is None:
            # No staleness check for this feed
            return True

        last_update = entry.get("last_update")
        if last_update is None:
            return False

        age = datetime.now(timezone.utc) - last_update
        return age <= threshold

    def are_core_feeds_healthy(self) -> bool:
        """
        Returns True only if kline, ticker, markprice, and orderbook are all healthy.
        News and macro feeds do NOT affect this result.
        """
        return all(self.is_healthy(feed) for feed in _CORE_FEEDS)

    def get_degraded_feeds(self) -> list[str]:
        """Return list of feed names currently not in 'healthy' state."""
        return [
            name
            for name, entry in self._state.items()
            if entry["state"] != "healthy"
        ]

    def get_state(self, feed_name: str) -> dict:
        """Return raw state dict for a feed (for debugging/logging)."""
        return dict(self._state.get(feed_name, {}))


# Module-level singleton — all other modules import this directly
monitor = FeedHealthMonitor()
