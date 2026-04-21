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
    _PM_ECHO_REDACTED,
    _SCRAPE_BLOCKLIST,
    _format_ddg_results,
    _pick_urls_for_category,
    _scrub_polymarket_text,
    _scrub_prediction_market_echoes,
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


class ScrapeBlocklistExcludesCoinGecko(unittest.TestCase):
    """CoinGecko embeds Polymarket price widgets on asset pages — the full-
    page scrape must not pull coingecko.com even if DDG surfaces it."""

    def test_coingecko_in_blocklist(self):
        self.assertIn("coingecko.com", _SCRAPE_BLOCKLIST)

    def test_coingecko_url_is_not_picked(self):
        fake_results = [
            {"href": "https://www.coingecko.com/en/coins/bitcoin",
             "title": "Bitcoin Price", "body": "BTC price"},
            {"href": "https://www.coindesk.com/bitcoin-analysis",
             "title": "BTC analysis", "body": "btc trend"},
            {"href": "https://www.theblock.co/btc-report",
             "title": "The Block BTC", "body": "report"},
        ]
        picked = _pick_urls_for_category(fake_results, category="crypto",
                                         max_urls=3)
        self.assertFalse(
            any("coingecko.com" in u for u in picked),
            f"coingecko.com should not appear in scraped URLs, got {picked}",
        )
        # The legitimate crypto sources are still picked.
        self.assertTrue(any("coindesk.com" in u for u in picked))
        self.assertTrue(any("theblock.co"  in u for u in picked))

    def test_non_crypto_domains_unaffected(self):
        fake_results = [
            {"href": "https://www.bbc.com/news/politics-story",
             "title": "Politics", "body": "news"},
            {"href": "https://www.reuters.com/world/analysis",
             "title": "Reuters", "body": "news"},
        ]
        picked = _pick_urls_for_category(fake_results, category="politics",
                                         max_urls=3)
        self.assertEqual(len(picked), 2)


class ScrapeBlocklistCoversPredictionMarketEchoes(unittest.TestCase):
    """Sites that primarily echo prediction-market prices are blocklisted."""

    def test_predictit_blocklisted(self):
        self.assertIn("predictit.org", _SCRAPE_BLOCKLIST)

    def test_polyfire_blocklisted(self):
        self.assertIn("polyfire.co", _SCRAPE_BLOCKLIST)

    def test_metaculus_blocklisted(self):
        self.assertIn("metaculus.com", _SCRAPE_BLOCKLIST)


class PredictionMarketEchoScrub(unittest.TestCase):
    """Third-party pages echoing Polymarket / PredictIt prices are rewritten
    with a visible marker; independent expert forecasts are preserved."""

    def test_rcp_polymarket_widget_scrubbed(self):
        text = ("The latest polls are tight, but Polymarket gives this 65% "
                "as of this morning, reflecting late movement toward Trump.")
        out = _scrub_prediction_market_echoes(text)
        self.assertIn(_PM_ECHO_REDACTED, out)
        self.assertNotIn("65%", out)
        self.assertIn("late movement toward Trump", out)

    def test_predictit_widget_scrubbed(self):
        text = "PredictIt shows this at 42% while Polymarket has similar pricing."
        out = _scrub_prediction_market_echoes(text)
        self.assertIn(_PM_ECHO_REDACTED, out)
        self.assertNotIn("42%", out)

    def test_prediction_markets_phrase_scrubbed(self):
        text = ("Prediction markets price the incumbent at 58%, while the "
                "historical base rate is closer to 70%.")
        out = _scrub_prediction_market_echoes(text)
        self.assertIn(_PM_ECHO_REDACTED, out)
        self.assertNotIn("58%", out)
        # The independent base-rate sentence survives.
        self.assertIn("historical base rate is closer to 70%", out)

    def test_percent_on_polymarket_scrubbed(self):
        text = "As of Tuesday, Biden was trading at 48% on Polymarket."
        out = _scrub_prediction_market_echoes(text)
        self.assertIn(_PM_ECHO_REDACTED, out)
        self.assertNotIn("48%", out)

    def test_538_forecast_preserved(self):
        text = ("538's model gives Biden a 62% chance of winning, up from "
                "55% last week as polling tightened in the Rust Belt.")
        out = _scrub_prediction_market_echoes(text)
        self.assertNotIn(_PM_ECHO_REDACTED, out)
        self.assertIn("62%", out)
        self.assertIn("55%", out)
        self.assertIn("Rust Belt", out)

    def test_nate_silver_preserved(self):
        text = ("Nate Silver's personal model puts Harris at 51%, a sharp "
                "divergence from his prior estimate of 47%.")
        out = _scrub_prediction_market_echoes(text)
        self.assertNotIn(_PM_ECHO_REDACTED, out)
        self.assertIn("51%", out)
        self.assertIn("47%", out)

    def test_poll_result_preserved(self):
        text = ("Trump leads 47-43 in the latest NYT/Siena poll of likely "
                "voters, with 10% undecided.")
        out = _scrub_prediction_market_echoes(text)
        self.assertNotIn(_PM_ECHO_REDACTED, out)
        self.assertIn("47-43", out)
        self.assertIn("10%", out)

    def test_sportsbook_odds_preserved(self):
        text = ("Chiefs are -175 favorites at DraftKings, implying roughly "
                "64% win probability on raw odds.")
        out = _scrub_prediction_market_echoes(text)
        self.assertNotIn(_PM_ECHO_REDACTED, out)
        self.assertIn("64%", out)


class SnippetLevelEchoScrub(unittest.TestCase):
    """DDG snippets go through the echo scrub before entering the bundle."""

    def test_polyfire_snippet_scrubbed(self):
        ddg_results = [
            {"title": "BTC April — PolyFire",
             "body": "Polymarket has Bitcoin at 12% to reach $80k.",
             "href": "https://polyfire.co/some-market"},
        ]
        formatted = _format_ddg_results(ddg_results, max_snippet=300)
        joined = "\n".join(formatted)
        self.assertIn(_PM_ECHO_REDACTED, joined)
        self.assertNotIn("12%", joined)

    def test_on_polymarket_snippet_scrubbed(self):
        ddg_results = [
            {"title": "Senate race analysis",
             "body": ("On Polymarket, the Democratic nominee is trading "
                      "around 55% heading into the final week."),
             "href": "https://example.com/senate"},
        ]
        formatted = _format_ddg_results(ddg_results, max_snippet=300)
        joined = "\n".join(formatted)
        self.assertIn(_PM_ECHO_REDACTED, joined)
        self.assertNotIn("55%", joined)

    def test_independent_snippet_passes(self):
        ddg_results = [
            {"title": "NYT/Siena poll release",
             "body": ("The latest NYT/Siena poll shows Harris +3, 48-45, "
                      "with 7% undecided likely voters."),
             "href": "https://nytimes.com/poll"},
        ]
        formatted = _format_ddg_results(ddg_results, max_snippet=300)
        joined = "\n".join(formatted)
        self.assertNotIn(_PM_ECHO_REDACTED, joined)
        self.assertIn("48-45", joined)
        self.assertIn("7%", joined)


if __name__ == "__main__":
    unittest.main()
