import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from engine.scan_budget import TimeBudget


def test_unbounded_budget_never_stops() -> None:
    b = TimeBudget(None)
    assert b.unbounded
    assert b.remaining() == math.inf
    assert b.can_start_market()
    assert not b.expired()
    assert b.per_market_timeout(150) == 150


def test_budget_stops_before_a_market_that_cannot_fit() -> None:
    now = [0.0]
    b = TimeBudget(220, initial_expected_s=45, floor_s=20, clock=lambda: now[0])
    assert b.can_start_market()
    now[0] = 170                       # 50 s left, expected 45 -> ok
    assert b.can_start_market()
    now[0] = 180                       # 40 s left, expected 45 -> stop
    assert not b.can_start_market()
    assert not b.expired()
    now[0] = 221
    assert b.expired()
    assert b.remaining() == 0.0


def test_observed_durations_update_expectation() -> None:
    now = [0.0]
    b = TimeBudget(300, initial_expected_s=45, clock=lambda: now[0])
    b.note_market_duration(90)         # first observation replaces prior
    assert b.expected_market_s == 90
    b.note_market_duration(30)         # then 50/50 blend
    assert b.expected_market_s == 60
    now[0] = 250                       # 50 s left < 60 expected -> stop
    assert not b.can_start_market()


def test_floor_applies_when_markets_are_fast() -> None:
    now = [0.0]
    b = TimeBudget(100, floor_s=20, clock=lambda: now[0])
    b.note_market_duration(2)
    now[0] = 85                        # 15 s left < 20 floor -> stop
    assert not b.can_start_market()


def test_per_market_timeout_caps_to_remaining() -> None:
    now = [0.0]
    b = TimeBudget(100, clock=lambda: now[0])
    assert b.per_market_timeout(150) == 100
    now[0] = 70
    assert b.per_market_timeout(150) == 30
    now[0] = 99.5
    assert b.per_market_timeout(150) == 1.0
