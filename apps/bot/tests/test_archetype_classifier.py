"""
Unit tests for engine.archetype_classifier.classify_archetype.

The classifier is a pure function. Each test exercises one branch and
its expected label. Real production questions (from pm_positions) are
used as fixtures so the tests describe exactly the inputs the bot sees.
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
    def test_qualifier_matches_qualification_anywhere(self):
        self.assertEqual(
            classify_archetype(
                "Madrid Open, Qualification: Alycia Parks vs Ksenia Efremova",
                category="sports",
                event_slug="wta-parks-efremov-2026-04-21",
            ),
            "tennis_qualifier",
        )

    def test_qualifier_matches_qualifying_round(self):
        self.assertEqual(
            classify_archetype(
                "Roland Garros Qualifying: Smith vs Jones",
                category="sports",
            ),
            "tennis_qualifier",
        )

    def test_main_draw_grand_slam(self):
        self.assertEqual(
            classify_archetype(
                "Madrid Open: Kaitlin Quevedo vs Venus Williams",
                category="sports",
                event_slug="wta-quevedo-william-2026-04-21",
            ),
            "tennis_main_draw",
        )

    def test_lower_tier_challenger_by_city_prefix(self):
        # "Savannah: X vs Y" — ATP Challenger-tier event.
        self.assertEqual(
            classify_archetype(
                "Savannah: Alex Rybakov vs Kilian Feldbausch",
                category="sports",
                event_slug="atp-rybakov-feldbau-2026-04-21",
            ),
            "tennis_lower_tier",
        )

    def test_lower_tier_abidjan(self):
        self.assertEqual(
            classify_archetype(
                "Abidjan: Maxime Chazal vs Millen Hurrion",
                category="sports",
                event_slug="atp-chazal-hurrion-2026-04-21",
            ),
            "tennis_lower_tier",
        )

    def test_atp_branded_without_tournament_name_is_lower_tier(self):
        # Fallthrough for ATP-flagged matches we don't have a tournament
        # match for — treated as lower tier by default.
        self.assertEqual(
            classify_archetype(
                "ATP event: Doe vs Roe",
                category="sports",
                event_slug="atp-doe-roe-2026",
            ),
            "tennis_lower_tier",
        )


class TeamSportsTests(unittest.TestCase):
    def test_mlb_game(self):
        self.assertEqual(
            classify_archetype(
                "St. Louis Cardinals vs. Miami Marlins",
                category="sports",
                event_slug="mlb-stl-mia-2026-04-21",
            ),
            "baseball_game",
        )

    def test_nba_prop_over_under(self):
        self.assertEqual(
            classify_archetype(
                "Trail Blazers vs. Spurs: O/U 219.5",
                category="sports",
                event_slug="nba-por-sas-2026-04-21",
            ),
            "basketball_prop",
        )

    def test_nba_spread(self):
        # No NBA team tokens in the question, so it lands in sports_other
        # via the coarse category. The accompanying O/U or spread market
        # on the same event does get tagged basketball_prop.
        self.assertIn(
            classify_archetype(
                "Spread: Rockets (-5.5)",
                category="sports",
                event_slug="nba-hou-lal-2026-04-21",
            ),
            ("basketball_prop", "basketball_game"),
        )

    def test_nba_straight_game(self):
        self.assertEqual(
            classify_archetype(
                "Lakers vs. Celtics",
                category="sports",
            ),
            "basketball_game",
        )

    def test_cricket_psl(self):
        self.assertEqual(
            classify_archetype(
                "Pakistan Super League: Rawalpindi Pindiz vs Multan Sultans",
                category="sports",
            ),
            "cricket_match",
        )

    def test_esports_lol_qualifier_is_tennis_qualifier_by_rule(self):
        # The word "Qualifier" in the question triggers the qualifier
        # branch regardless of sport. The user's explicit ask is to skip
        # *qualifier-tier* trades across the board — treating esports
        # qualifiers the same as tennis qualifiers is consistent with that.
        self.assertEqual(
            classify_archetype(
                "LoL: LYON vs FlyQuest (BO3) - Esports World Cup "
                "North America Qualifier Playoffs",
                category="sports",
            ),
            "tennis_qualifier",
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

    def test_canonical_labels_cover_fixtures(self):
        expected = {
            "tennis_qualifier", "tennis_main_draw", "tennis_lower_tier",
            "basketball_game", "basketball_prop", "baseball_game",
            "cricket_match",
            "price_threshold", "activity_count", "geopolitical_event",
            "sports_other", "binary_event",
        }
        self.assertTrue(expected.issubset(set(ARCHETYPES)))


class SkipListIntegrationTests(unittest.TestCase):
    """
    End-to-end contract: the sizer's skip-list gate fires on the
    classifier's output. This is the whole point of B1.
    """

    def _uc(self, skip_list):
        from dataclasses import dataclass, field

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

    def test_skip_list_rejects_tennis_qualifier(self):
        from execution.pm_sizer import size_position

        archetype = classify_archetype(
            "Madrid Open, Qualification: Alycia Parks vs Ksenia Efremova",
            category="sports",
        )
        decision = size_position(
            claude_p=0.80, confidence=0.75,
            ask_yes=0.40, ask_no=0.60,
            bankroll=1000.0,
            user_config=self._uc(("tennis_qualifier",)),
            archetype=archetype,
        )
        self.assertFalse(decision.should_trade)
        self.assertIn("skip list", decision.skip_reason or "")

    def test_skip_list_permits_basketball_game(self):
        from execution.pm_sizer import size_position

        archetype = classify_archetype("Lakers vs. Celtics", category="sports")
        decision = size_position(
            claude_p=0.80, confidence=0.75,
            ask_yes=0.40, ask_no=0.60,
            bankroll=1000.0,
            user_config=self._uc(("tennis_qualifier",)),
            archetype=archetype,
        )
        self.assertTrue(decision.should_trade)


if __name__ == "__main__":
    unittest.main()
