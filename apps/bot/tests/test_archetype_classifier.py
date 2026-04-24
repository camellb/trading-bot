"""
Unit tests for engine.archetype_classifier.classify_archetype.

The classifier is a pure function. Each test exercises one branch and
its expected label. Real production questions (from pm_positions) are
used as fixtures so the tests describe exactly the inputs the bot sees.

The taxonomy is sport-level: "tennis", "basketball", "baseball",
"football", "hockey", "cricket", "esports", "soccer", plus the
sports catch-all "sports_other" and non-sports labels.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.archetype_classifier import (
    ARCHETYPES,
    classify_archetype,
)


class TennisClassificationTests(unittest.TestCase):
    def test_qualification_returns_tennis(self):
        self.assertEqual(
            classify_archetype(
                "Madrid Open, Qualification: Alycia Parks vs Ksenia Efremova",
                category="sports",
                event_slug="wta-parks-efremov-2026-04-21",
            ),
            "tennis",
        )

    def test_qualifying_round_returns_tennis(self):
        self.assertEqual(
            classify_archetype(
                "Roland Garros Qualifying: Smith vs Jones",
                category="sports",
            ),
            "tennis",
        )

    def test_grand_slam_main_draw_returns_tennis(self):
        self.assertEqual(
            classify_archetype(
                "Madrid Open: Kaitlin Quevedo vs Venus Williams",
                category="sports",
                event_slug="wta-quevedo-william-2026-04-21",
            ),
            "tennis",
        )

    def test_challenger_by_city_prefix_returns_tennis(self):
        # "Savannah: X vs Y" - ATP Challenger-tier event.
        self.assertEqual(
            classify_archetype(
                "Savannah: Alex Rybakov vs Kilian Feldbausch",
                category="sports",
                event_slug="atp-rybakov-feldbau-2026-04-21",
            ),
            "tennis",
        )

    def test_abidjan_challenger_returns_tennis(self):
        self.assertEqual(
            classify_archetype(
                "Abidjan: Maxime Chazal vs Millen Hurrion",
                category="sports",
                event_slug="atp-chazal-hurrion-2026-04-21",
            ),
            "tennis",
        )

    def test_atp_branded_without_tournament_name_returns_tennis(self):
        self.assertEqual(
            classify_archetype(
                "ATP event: Doe vs Roe",
                category="sports",
                event_slug="atp-doe-roe-2026",
            ),
            "tennis",
        )


class TeamSportsTests(unittest.TestCase):
    def test_mlb_game_returns_baseball(self):
        self.assertEqual(
            classify_archetype(
                "St. Louis Cardinals vs. Miami Marlins",
                category="sports",
                event_slug="mlb-stl-mia-2026-04-21",
            ),
            "baseball",
        )

    def test_nba_prop_over_under_returns_basketball(self):
        self.assertEqual(
            classify_archetype(
                "Trail Blazers vs. Spurs: O/U 219.5",
                category="sports",
                event_slug="nba-por-sas-2026-04-21",
            ),
            "basketball",
        )

    def test_nba_spread_without_team_falls_to_sports_other(self):
        # No NBA team tokens in the question, so it lands in sports_other
        # via the coarse category.
        self.assertEqual(
            classify_archetype(
                "Spread: something (-5.5)",
                category="sports",
                event_slug="nba-hou-lal-2026-04-21",
            ),
            "sports_other",
        )

    def test_nba_straight_game_returns_basketball(self):
        self.assertEqual(
            classify_archetype(
                "Lakers vs. Celtics",
                category="sports",
            ),
            "basketball",
        )

    def test_cricket_psl_returns_cricket(self):
        self.assertEqual(
            classify_archetype(
                "Pakistan Super League: Rawalpindi Pindiz vs Multan Sultans",
                category="sports",
            ),
            "cricket",
        )

    def test_soccer_premier_league_returns_soccer(self):
        self.assertEqual(
            classify_archetype(
                "Premier League: Arsenal vs Chelsea",
                category="sports",
            ),
            "soccer",
        )

    def test_nhl_game_returns_hockey(self):
        self.assertEqual(
            classify_archetype(
                "Maple Leafs vs. Bruins",
                category="sports",
            ),
            "hockey",
        )

    def test_nfl_game_returns_football(self):
        self.assertEqual(
            classify_archetype(
                "Patriots vs. Chiefs",
                category="sports",
            ),
            "football",
        )

    def test_esports_lol_qualifier_returns_tennis_by_qualifier_rule(self):
        # The word "Qualifier" in the question triggers the qualifier
        # branch regardless of sport - short-circuit by design.
        self.assertEqual(
            classify_archetype(
                "LoL: LYON vs FlyQuest (BO3) - Esports World Cup "
                "North America Qualifier Playoffs",
                category="sports",
            ),
            "tennis",
        )

    def test_esports_non_qualifier_returns_esports(self):
        self.assertEqual(
            classify_archetype(
                "LCS Finals: Cloud9 vs TSM",
                category="sports",
            ),
            "esports",
        )


class NonSportsTests(unittest.TestCase):
    def test_crypto_price_threshold(self):
        self.assertEqual(
            classify_archetype(
                "Will Bitcoin reach $80,000 April 20-26?",
                category="crypto",
            ),
            "price_threshold",
        )

    def test_activity_count_tweets(self):
        self.assertEqual(
            classify_archetype(
                "Will Elon Musk post 65-89 tweets from April 20 to April 22, 2026?",
                category="entertainment",
            ),
            "activity_count",
        )

    def test_geopolitical_event_diplomatic(self):
        self.assertEqual(
            classify_archetype(
                "US x Iran diplomatic meeting by April 24, 2026?",
                category="geopolitics",
            ),
            "geopolitical_event",
        )

    def test_geopolitical_event_blockade(self):
        self.assertEqual(
            classify_archetype(
                "Will Donald Trump announce that the United States blockade "
                "of the Strait of Hormuz has been lifted by April 23, 2026?",
                category="geopolitics",
            ),
            "geopolitical_event",
        )


class EdgeCaseTests(unittest.TestCase):
    def test_empty_question_falls_through_to_binary_event(self):
        self.assertEqual(classify_archetype(""), "binary_event")
        self.assertEqual(classify_archetype("   ", category=None), "binary_event")

    def test_unknown_sports_question_falls_through_to_sports_other(self):
        self.assertEqual(
            classify_archetype("Some obscure thing happens", category="sports"),
            "sports_other",
        )

    def test_unknown_category_falls_through_to_binary_event(self):
        self.assertEqual(
            classify_archetype("Will something unusual happen by 2027?"),
            "binary_event",
        )

    def test_canonical_labels_cover_sport_level_taxonomy(self):
        expected = {
            "tennis", "basketball", "baseball", "football", "hockey",
            "cricket", "esports", "soccer", "sports_other",
            "price_threshold", "activity_count", "geopolitical_event",
            "binary_event",
        }
        self.assertEqual(expected, set(ARCHETYPES))


class SkipListIntegrationTests(unittest.TestCase):
    """
    End-to-end contract: the sizer's skip-list gate fires on the
    classifier's output. Flat taxonomy - "tennis" instead of
    "tennis_qualifier" - is the label the skip list must match.
    """

    def _uc(self, skip_list):
        from dataclasses import dataclass

        @dataclass
        class _UC:
            min_p_win: float = 0.50
            confidence_full_stake: float = 0.70
            confidence_override_threshold: float = 0.75
            base_stake_pct: float = 0.02
            max_stake_pct: float = 0.05
            archetype_skip_list: tuple = ()

        uc = _UC()
        uc.archetype_skip_list = tuple(skip_list)
        return uc

    def test_skip_list_rejects_tennis(self):
        from execution.pm_sizer import size_position

        archetype = classify_archetype(
            "Madrid Open, Qualification: Alycia Parks vs Ksenia Efremova",
            category="sports",
        )
        decision = size_position(
            claude_p=0.80, confidence=0.75,
            ask_yes=0.40, ask_no=0.60,
            bankroll=1000.0,
            user_config=self._uc(("tennis",)),
            archetype=archetype,
        )
        self.assertFalse(decision.should_trade)
        self.assertIn("skip list", decision.skip_reason or "")

    def test_skip_list_permits_basketball(self):
        from execution.pm_sizer import size_position

        archetype = classify_archetype("Lakers vs. Celtics", category="sports")
        decision = size_position(
            claude_p=0.80, confidence=0.75,
            ask_yes=0.40, ask_no=0.60,
            bankroll=1000.0,
            user_config=self._uc(("tennis",)),
            archetype=archetype,
        )
        self.assertTrue(decision.should_trade)


if __name__ == "__main__":
    unittest.main()
