"""Wall-clock budget for the market scan.

The scan evaluates markets one at a time. Each market costs research
(up to 30 s), one forecaster call (up to 90 s per attempt) and, on a
trade, an order. The scheduler gives the whole scan 240 s before its
inner fence cancels it and 260 s before the outer fence abandons it.
Before 2026-09-13 the scan had no idea a budget existed: it ran until
the fence killed it mid-market, every tick was recorded as an error,
and `runtime_alerts` pushed "Market scan failed" to the dashboard and
Telegram on every tick (262 fence hits in two days on one install).

`TimeBudget` lets the scan stop cleanly between markets instead. It
tracks how long recent markets took (exponential moving average) and
answers "is there enough time left to start another one?" so the
fence only fires on a genuine wedge.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Optional


class TimeBudget:
    def __init__(self, seconds: Optional[float], *,
                 initial_expected_s: float = 45.0,
                 floor_s: float = 20.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._deadline = (self._started + float(seconds)) if seconds else None
        self._expected_s = float(initial_expected_s)
        self._floor_s = float(floor_s)
        self.markets_timed = 0

    @property
    def unbounded(self) -> bool:
        return self._deadline is None

    def elapsed(self) -> float:
        return self._clock() - self._started

    def remaining(self) -> float:
        if self._deadline is None:
            return math.inf
        return max(0.0, self._deadline - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    @property
    def expected_market_s(self) -> float:
        return self._expected_s

    def note_market_duration(self, seconds: float) -> None:
        """Feed the observed cost of one market into the estimate. The
        first observation replaces the prior; later ones blend 50/50 so
        one slow outlier does not stop the scan for the rest of the tick."""
        seconds = max(0.0, float(seconds))
        if self.markets_timed == 0:
            self._expected_s = seconds
        else:
            self._expected_s = 0.5 * self._expected_s + 0.5 * seconds
        self.markets_timed += 1

    def can_start_market(self) -> bool:
        """True when the remaining time covers one more market at the
        current expected cost (never less than `floor_s`)."""
        if self._deadline is None:
            return True
        return self.remaining() >= max(self._expected_s, self._floor_s)

    def per_market_timeout(self, cap_s: float) -> Optional[float]:
        """Timeout to wrap the next market in: the smaller of `cap_s` and
        what is left, or None when the budget is unbounded and `cap_s`
        is not positive."""
        if self._deadline is None:
            return cap_s if cap_s > 0 else None
        return max(1.0, min(cap_s, self.remaining())) if cap_s > 0 else max(1.0, self.remaining())
