"""
Regression tests for the EV-based sizer.

Covers the cases called out in the project doctrine:

    - Agrees-with-market bets produce +EV and take YES.
    - Longshot-NO when Claude is confident YES is very unlikely.
    - Uncertain estimates (0.45..0.55) are skipped.
    - Extreme-EV bets still stake flat — no Kelly amplification.
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass

# Allow running via `python -m unittest tests.test_pm_sizer` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.pm_sizer import size_position
from engine.user_config import UserConfig


def default_cfg() -> UserConfig:
    """Fresh dataclass defaults — no DB hit."""
    return UserConfig()


def cfg(**overrides) -> UserConfig:
    base = default_cfg()
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class AgreesWithMarketTests(unittest.TestCase):
    """Claude 0.85, market ask YES 0.82 — old bot skipped, new bot bets YES."""

    def test_picks_yes_side_and_positive_ev(self):
        d = size_position(
            claude_p=0.85, confidence=0.70,
            ask_yes=0.82, ask_no=0.20,
            bankroll=1000.0, user_config=default_cfg(),
        )
        # The sizer's first job is to identify the +EV side. This case gives
        # ev_yes ≈ +2.16% and ev_no ≈ −26.5%; YES is the correct side.
        self.assertEqual(d.side, "YES")
        self.assertGreater(d.ev, 0.0)

    def test_bets_when_threshold_allows(self):
        # With default min_ev_threshold=3% this marginal case would skip
        # (+2.16% EV doesn't clear). Lower the threshold and it bets.
        d = size_position(
            claude_p=0.85, confidence=0.70,
            ask_yes=0.82, ask_no=0.20,
            bankroll=1000.0, user_config=cfg(min_ev_threshold=0.02),
        )
        self.assertTrue(d.should_trade)
        self.assertEqual(d.side, "YES")
        self.assertAlmostEqual(d.entry_price, 0.82, places=3)


class LongshotNoTests(unittest.TestCase):
    """Claude 0.15, market ask NO 0.18 — clear NO bet."""

    def test_bets_no(self):
        d = size_position(
            claude_p=0.15, confidence=0.70,
            ask_yes=0.84, ask_no=0.18,
            bankroll=1000.0, user_config=default_cfg(),
        )
        self.assertTrue(d.should_trade)
        self.assertEqual(d.side, "NO")
        self.assertAlmostEqual(d.entry_price, 0.18, places=3)
        self.assertGreater(d.ev, 0.03)


class UncertainBandTests(unittest.TestCase):
    """Claude 0.48 — right in the coin-flip band, sizer skips regardless."""

    def test_skips_uncertain(self):
        d = size_position(
            claude_p=0.48, confidence=0.90,
            ask_yes=0.50, ask_no=0.52,
            bankroll=1000.0, user_config=default_cfg(),
        )
        self.assertFalse(d.should_trade)
        self.assertIsNotNone(d.skip_reason)
        self.assertIn("uncertain", d.skip_reason.lower())

    def test_skips_uncertain_upper_edge(self):
        d = size_position(
            claude_p=0.54, confidence=0.90,
            ask_yes=0.50, ask_no=0.52,
            bankroll=1000.0, user_config=default_cfg(),
        )
        self.assertFalse(d.should_trade)

    def test_edge_of_uncertain_band_not_skipped(self):
        # 0.45 and 0.55 are boundaries — strictly outside (sizer uses <).
        d = size_position(
            claude_p=0.45, confidence=0.70,
            ask_yes=0.42, ask_no=0.60,
            bankroll=1000.0, user_config=default_cfg(),
        )
        self.assertIsNone(d.skip_reason if d.skip_reason and "uncertain" in d.skip_reason.lower() else None)


class FlatSizingTests(unittest.TestCase):
    """Claude 0.85 with market ask YES 0.30 — huge EV, but stake stays flat."""

    def test_extreme_ev_does_not_amplify_stake(self):
        user_config = default_cfg()
        bankroll = 1000.0
        d = size_position(
            claude_p=0.85, confidence=0.85,
            ask_yes=0.30, ask_no=0.72,
            bankroll=bankroll, user_config=user_config,
        )
        self.assertTrue(d.should_trade)
        self.assertEqual(d.side, "YES")
        # EV is enormous here; Kelly would push 60%+ of bankroll. The new
        # sizer must cap at max_stake_pct (5% default) regardless.
        max_allowed = bankroll * user_config.max_stake_pct + 1e-6
        self.assertLessEqual(d.stake_usd, max_allowed)

    def test_confidence_scaling_not_ev_scaling(self):
        # Same huge EV, but confidence 0.4 → half the baseline stake.
        low = size_position(
            claude_p=0.85, confidence=0.40,
            ask_yes=0.30, ask_no=0.72,
            bankroll=1000.0, user_config=default_cfg(),
        )
        high = size_position(
            claude_p=0.85, confidence=0.85,
            ask_yes=0.30, ask_no=0.72,
            bankroll=1000.0, user_config=default_cfg(),
        )
        self.assertTrue(low.should_trade)
        self.assertTrue(high.should_trade)
        self.assertLess(low.stake_usd, high.stake_usd)


class EvComputationTests(unittest.TestCase):
    """The doctrine formula: EV = p_win × (1 / ask) − 1 − cost_assumption."""

    def test_ev_matches_formula(self):
        d = size_position(
            claude_p=0.70, confidence=0.70,
            ask_yes=0.50, ask_no=0.52,
            bankroll=1000.0, user_config=default_cfg(),
        )
        # YES side: 0.70 / 0.50 - 1 - 0.015 = 1.4 - 1.015 = 0.385
        self.assertEqual(d.side, "YES")
        self.assertAlmostEqual(d.ev, 0.385, places=4)
        self.assertAlmostEqual(d.p_win, 0.70, places=6)


class ConfidenceStakeTiersTests(unittest.TestCase):
    """Stake scales with confidence tiers, not continuously."""

    def _stake(self, confidence: float) -> float:
        d = size_position(
            claude_p=0.80, confidence=confidence,
            ask_yes=0.40, ask_no=0.62,
            bankroll=10000.0, user_config=default_cfg(),
        )
        return d.stake_usd

    def test_tier_low(self):
        # confidence < 0.5 → 0.5 × base_stake_pct = 1% of bankroll = $100
        self.assertAlmostEqual(self._stake(0.40), 100.0, places=2)

    def test_tier_base(self):
        # 0.5 ≤ confidence < 0.8 → base_stake_pct = 2% = $200
        self.assertAlmostEqual(self._stake(0.60), 200.0, places=2)

    def test_tier_high(self):
        # confidence ≥ 0.8 → 1.5 × base = 3% = $300 (capped at max 5%)
        self.assertAlmostEqual(self._stake(0.85), 300.0, places=2)


class BelowThresholdTests(unittest.TestCase):
    def test_skip_reason_when_ev_below_min(self):
        d = size_position(
            claude_p=0.55, confidence=0.60,
            ask_yes=0.54, ask_no=0.47,
            bankroll=1000.0, user_config=default_cfg(),
        )
        # ev_yes = 0.55/0.54 - 1 - 0.015 ≈ 0.0035 — well under 3%
        self.assertFalse(d.should_trade)
        self.assertIsNotNone(d.skip_reason)
        self.assertIn("ev", d.skip_reason.lower())


class MinAbsoluteStakeTests(unittest.TestCase):
    def test_small_bankroll_still_meets_min_stake(self):
        # 0.5% of $100 = $0.50, below the $2 absolute floor.
        d = size_position(
            claude_p=0.85, confidence=0.40,
            ask_yes=0.30, ask_no=0.72,
            bankroll=100.0, user_config=default_cfg(),
        )
        if d.should_trade:
            self.assertGreaterEqual(d.stake_usd, 2.0)


class NoKellyFieldsTests(unittest.TestCase):
    """SizingDecision must not expose Kelly-era fields."""

    def test_sizing_decision_has_no_kelly_fields(self):
        d = size_position(
            claude_p=0.70, confidence=0.70,
            ask_yes=0.50, ask_no=0.52,
            bankroll=1000.0, user_config=default_cfg(),
        )
        self.assertFalse(hasattr(d, "kelly_full"))
        self.assertFalse(hasattr(d, "kelly_frac"))
        self.assertFalse(hasattr(d, "edge"))


if __name__ == "__main__":
    unittest.main()
