"""
Unit tests for `feeds.polymarket_feed` resolution-time extraction.

The tests pin the contract that powered Bug 1's fix: `endDate` from
Polymarket is a trading-window close, NOT a resolution time. We
prefer `gameStartTime + 3h` for sports and `events[0].endDate` for
events when it is later than the market-level `endDate`. Synthetic
fixtures only - no live API.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feeds.polymarket_feed import (
    SPORTS_RESOLUTION_BUFFER,
    PolyMarket,
    _as_market,
    _parse_iso,
    extract_resolution_estimate,
)


# ── _parse_iso ───────────────────────────────────────────────────────────────
class ParseIsoTests(unittest.TestCase):
    def test_standard_iso_with_z_suffix(self):
        dt = _parse_iso("2030-06-01T19:00:00Z")
        self.assertIsNotNone(dt)
        assert dt is not None  # for type narrowing
        self.assertEqual(dt.year, 2030)
        self.assertEqual(dt.month, 6)
        self.assertEqual(dt.hour, 19)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_game_start_time_format_with_space_separator(self):
        # Gamma's `gameStartTime` uses `'2026-04-27 01:30:00+00'` with
        # a space instead of 'T'. The standard library's fromisoformat
        # rejects that on older Python versions, so the parser
        # normalises space to 'T'.
        dt = _parse_iso("2030-06-01 19:00:00+00")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual(dt.hour, 19)
        self.assertIsNotNone(dt.tzinfo)

    def test_date_only_string_gets_utc(self):
        # Naive date strings ('2030-06-01') get tz forced to UTC so
        # arithmetic with tz-aware now() does not blow up.
        dt = _parse_iso("2030-06-01")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_iso(""))
        self.assertIsNone(_parse_iso(None))

    def test_malformed_returns_none(self):
        self.assertIsNone(_parse_iso("not a date"))
        self.assertIsNone(_parse_iso("2030-99-99"))


# ── extract_resolution_estimate ──────────────────────────────────────────────
class ExtractResolutionEstimateTests(unittest.TestCase):
    def test_sports_uses_game_start_time_plus_buffer(self):
        # Sports markets have gameStartTime. We add ~3h so the
        # countdown reflects the end of the game, not the tip.
        # `endDate` on sports rows usually equals tip; using it
        # would lie about how much time the user is waiting.
        raw = {
            "gameStartTime": "2030-06-01 19:00:00+00",
            "endDate": "2030-06-01T19:00:00Z",
            "endDateIso": "2030-06-01",
            "events": [{"endDate": "2030-06-01T19:00:00Z"}],
        }
        got = extract_resolution_estimate(raw)
        expected = datetime(2030, 6, 1, 19, 0, 0, tzinfo=timezone.utc) + SPORTS_RESOLUTION_BUFFER
        self.assertEqual(got, expected)

    def test_events_zero_end_date_when_later_than_market_end_date(self):
        # Common pattern: the market's `endDate` is a buffered
        # trading-window close set BEFORE the actual event
        # deadline. The event-level deadline (`events[0].endDate`)
        # is the better resolution proxy.
        raw = {
            "endDate": "2030-06-01T00:00:00Z",   # trading closes day-of
            "events": [{"endDate": "2030-06-30T00:00:00Z"}],  # actual deadline 4w later
        }
        got = extract_resolution_estimate(raw)
        self.assertEqual(got, datetime(2030, 6, 30, 0, 0, 0, tzinfo=timezone.utc))

    def test_events_zero_end_date_ignored_when_same_as_market_end_date(self):
        # When the event-level deadline equals the market endDate
        # there is no new information; fall back to endDate.
        raw = {
            "endDate": "2030-06-01T00:00:00Z",
            "events": [{"endDate": "2030-06-01T00:00:00Z"}],
        }
        got = extract_resolution_estimate(raw)
        self.assertEqual(got, datetime(2030, 6, 1, 0, 0, 0, tzinfo=timezone.utc))

    def test_events_zero_end_date_ignored_when_earlier(self):
        # If event deadline is earlier than the market endDate,
        # trust endDate. (Rare but defensive.)
        raw = {
            "endDate": "2030-06-30T00:00:00Z",
            "events": [{"endDate": "2030-06-01T00:00:00Z"}],
        }
        got = extract_resolution_estimate(raw)
        self.assertEqual(got, datetime(2030, 6, 30, 0, 0, 0, tzinfo=timezone.utc))

    def test_falls_back_to_end_date_when_no_events(self):
        raw = {"endDate": "2030-06-15T00:00:00Z", "events": []}
        got = extract_resolution_estimate(raw)
        self.assertEqual(got, datetime(2030, 6, 15, 0, 0, 0, tzinfo=timezone.utc))

    def test_falls_back_to_end_date_iso_when_end_date_missing(self):
        raw = {"endDateIso": "2030-06-15", "events": []}
        got = extract_resolution_estimate(raw)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.year, 2030)
        self.assertEqual(got.month, 6)
        self.assertEqual(got.day, 15)

    def test_returns_none_when_all_fields_missing(self):
        self.assertIsNone(extract_resolution_estimate({}))
        self.assertIsNone(extract_resolution_estimate({"endDate": None}))


# ── PolyMarket.resolution_at_estimate ────────────────────────────────────────
class PolyMarketResolutionEstimateTests(unittest.TestCase):
    def _make_market(self, **overrides) -> PolyMarket:
        defaults = dict(
            id="m1",
            condition_id="c1",
            question="Will X happen?",
            description="",
            outcome_yes="Yes",
            outcome_no="No",
            yes_price=0.5,
            no_price=0.5,
            volume_24h_clob=10_000.0,
            liquidity_num=5_000.0,
            end_date_iso=datetime(2030, 6, 1, 19, 0, 0, tzinfo=timezone.utc),
            slug="will-x-happen",
            category_hint=None,
            neg_risk=False,
            group_item_title=None,
            event_slug=None,
            game_start_time=None,
            event_end_date=None,
        )
        defaults.update(overrides)
        return PolyMarket(**defaults)

    def test_sports_uses_game_start_plus_buffer(self):
        gst = datetime(2030, 6, 1, 19, 0, 0, tzinfo=timezone.utc)
        mk = self._make_market(game_start_time=gst)
        self.assertEqual(mk.resolution_at_estimate, gst + SPORTS_RESOLUTION_BUFFER)

    def test_event_end_date_used_when_later(self):
        mk = self._make_market(
            end_date_iso=datetime(2030, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
            event_end_date=datetime(2030, 6, 30, 0, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            mk.resolution_at_estimate,
            datetime(2030, 6, 30, 0, 0, 0, tzinfo=timezone.utc),
        )

    def test_falls_back_to_end_date_iso_when_event_end_not_later(self):
        mk = self._make_market(
            end_date_iso=datetime(2030, 6, 30, 0, 0, 0, tzinfo=timezone.utc),
            event_end_date=datetime(2030, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            mk.resolution_at_estimate,
            datetime(2030, 6, 30, 0, 0, 0, tzinfo=timezone.utc),
        )

    def test_falls_back_to_end_date_iso_when_no_extras(self):
        mk = self._make_market()
        self.assertEqual(mk.resolution_at_estimate, mk.end_date_iso)


# ── _as_market populates new fields ──────────────────────────────────────────
class AsMarketParserTests(unittest.TestCase):
    def _base_row(self) -> dict:
        # Minimal binary market row that passes _as_market's
        # acceptance gates (two outcomes, two prices, valid endDate).
        return {
            "id": "m1",
            "conditionId": "c1",
            "question": "Will X happen?",
            "description": "",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": "[0.5, 0.5]",
            "volume24hrClob": 10_000,
            "liquidityNum": 5_000,
            "endDate": "2030-06-01T19:00:00Z",
            "endDateIso": "2030-06-01",
            "slug": "will-x-happen",
            "events": [],
        }

    def test_populates_game_start_time_for_sports_row(self):
        row = self._base_row()
        row["gameStartTime"] = "2030-06-01 19:00:00+00"
        mk = _as_market(row)
        self.assertIsNotNone(mk)
        assert mk is not None
        self.assertEqual(
            mk.game_start_time,
            datetime(2030, 6, 1, 19, 0, 0, tzinfo=timezone.utc),
        )
        # And resolution_at_estimate should be game_start_time + buffer.
        self.assertEqual(
            mk.resolution_at_estimate,
            datetime(2030, 6, 1, 19, 0, 0, tzinfo=timezone.utc) + SPORTS_RESOLUTION_BUFFER,
        )

    def test_populates_event_end_date_for_event_row(self):
        row = self._base_row()
        row["events"] = [{
            "ticker": "FED-APR",
            "slug": "fed-april-decision",
            "endDate": "2030-06-30T00:00:00Z",
        }]
        mk = _as_market(row)
        self.assertIsNotNone(mk)
        assert mk is not None
        self.assertEqual(
            mk.event_end_date,
            datetime(2030, 6, 30, 0, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            mk.resolution_at_estimate,
            datetime(2030, 6, 30, 0, 0, 0, tzinfo=timezone.utc),
        )

    def test_no_extras_still_parses_and_falls_back(self):
        mk = _as_market(self._base_row())
        self.assertIsNotNone(mk)
        assert mk is not None
        self.assertIsNone(mk.game_start_time)
        self.assertIsNone(mk.event_end_date)
        self.assertEqual(mk.resolution_at_estimate, mk.end_date_iso)


if __name__ == "__main__":
    unittest.main()
