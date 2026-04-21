"""Tests for the polymarket.com price-leak scrubber in research/fetcher.py.

The evaluator prompt tells Claude not to anchor on market prices. Polymarket's
own event pages leak prices into the research bundle via sibling-market
ladders, volume displays, and narrative "trader consensus at X%" phrases.
The scrubber strips those tokens before the scrape reaches Claude.
"""

from __future__ import annotations

import re
import unittest

from research.fetcher import (
    _POLYMARKET_SCRUB_PATTERNS,
    _scrub_polymarket_text,
)


# Real samples captured from polymarket.com scrapes (post-trafilatura).
SAMPLE_CRYPTO_LADDER = """\
What price will Bitcoin hit April 20-26?
$200,535 Vol.
Apr 27, 2026
↑ 88,000
1%
↑ 84,000
4%
↑ 80,000
31%
↑ 78,000
64%
↓ 70,000
13%
This market will resolve to "Yes" if any Binance 1-minute candle for BTC/USDT
has a final "High" price equal to or greater than the price specified.
The resolution source is Binance BTC/USDT.
"""

SAMPLE_POLITICS_NARRATIVE = """\
Trader consensus favors no US-Iran nuclear deal by April 30 at 57%, driven by
stalled indirect negotiations amid a fragile ceasefire nearing expiry around
April 22. Recent US seizures of Iranian cargo ships and mutual threats from
President Trump have heightened tensions.
$1,797,200 Vol.
"""

SAMPLE_MIXED_LONG_TAIL = """\
Russian forces continue probing Ukrainian defenses around Verkhnia Tersa in
Zaporizhzhia Oblast through infiltration missions and ground assaults northwest
of Hulyaipole, but have achieved no confirmed advances per the Institute for
the Study of War's April 20 assessment.
$160,676 Vol.
April 30
91%
Territory will be considered captured if any part of the specified territory
is shaded under the ISW map.
"""


class ScrubberRemovesPriceTokens(unittest.TestCase):
    """After scrubbing, the three core regex families should match nothing."""

    PRICE_PCT = re.compile(r"\b\d{1,3}%")
    VOLUME    = re.compile(r"\$[\d,]+\s*Vol\.?", re.IGNORECASE)
    CONSENSUS = re.compile(
        r"\b(trader|market) consensus (favors|for|against|at|on|is)\b",
        re.IGNORECASE,
    )

    def _assert_clean(self, text: str) -> None:
        self.assertIsNone(
            self.PRICE_PCT.search(text),
            f"percentage leaked: {self.PRICE_PCT.search(text)!r}",
        )
        self.assertIsNone(
            self.VOLUME.search(text),
            f"volume leaked: {self.VOLUME.search(text)!r}",
        )
        self.assertIsNone(
            self.CONSENSUS.search(text),
            f"consensus phrase leaked: {self.CONSENSUS.search(text)!r}",
        )

    def test_crypto_ladder_scrubbed(self):
        out = _scrub_polymarket_text(SAMPLE_CRYPTO_LADDER)
        self._assert_clean(out)

    def test_politics_narrative_scrubbed(self):
        out = _scrub_polymarket_text(SAMPLE_POLITICS_NARRATIVE)
        self._assert_clean(out)

    def test_long_tail_scrubbed(self):
        out = _scrub_polymarket_text(SAMPLE_MIXED_LONG_TAIL)
        self._assert_clean(out)


class ScrubberPreservesNonPriceEvidence(unittest.TestCase):
    """Narrative that isn't a price should survive."""

    def test_resolution_criteria_preserved(self):
        out = _scrub_polymarket_text(SAMPLE_CRYPTO_LADDER)
        self.assertIn("Binance", out)
        self.assertIn("BTC/USDT", out)
        self.assertIn("1-minute candle", out)

    def test_geopolitical_context_preserved(self):
        out = _scrub_polymarket_text(SAMPLE_POLITICS_NARRATIVE)
        self.assertIn("Iran", out)
        self.assertIn("ceasefire", out)
        self.assertIn("Trump", out)

    def test_long_tail_narrative_preserved(self):
        out = _scrub_polymarket_text(SAMPLE_MIXED_LONG_TAIL)
        self.assertIn("Verkhnia Tersa", out)
        self.assertIn("Zaporizhzhia", out)
        self.assertIn("ISW map", out)


class ScrubberDropsGarbageParagraphs(unittest.TestCase):
    """Paragraphs that become >50% redacted tokens should be dropped entirely."""

    def test_high_density_price_line_dropped(self):
        garbage = "Trader consensus at 57% on Polymarket price 42%"
        out = _scrub_polymarket_text(garbage)
        self.assertNotIn("Trader consensus at", out)

    def test_mixed_paragraph_kept(self):
        ok = ("Iran rejected the US demand for zero enrichment, a rare public "
              "statement from Tehran's foreign ministry this week.")
        out = _scrub_polymarket_text(ok)
        self.assertIn("Iran rejected", out)
        self.assertIn("Tehran", out)


class ScrubberPatternsExposed(unittest.TestCase):
    """The pattern list is public so callers can introspect it."""

    def test_patterns_are_compiled(self):
        self.assertTrue(len(_POLYMARKET_SCRUB_PATTERNS) >= 3)
        for pat in _POLYMARKET_SCRUB_PATTERNS:
            self.assertIsInstance(pat, re.Pattern)


if __name__ == "__main__":
    unittest.main()
