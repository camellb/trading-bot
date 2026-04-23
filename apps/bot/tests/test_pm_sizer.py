"""
Regression tests for the sizer.

Gate 1 - side selection (never skips):
    - confidence >= confidence_override_threshold (default 0.75):
      ignore the market, side = YES if claude_p >= 0.50 else NO.
    - confidence <  confidence_override_threshold:
      mean = (claude_p + ask_yes) / 2, side = YES if mean >= 0.50 else NO.

Gate 2 - minimum p_win (default 0.50).

Confidence softener (size only - never skip):
    multiplier = min(1.0, 0.01 + (confidence / confidence_full_stake) * 0.99)
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.pm_sizer import size_position, COST_ASSUMPTION
from engine.user_config import UserConfig


def default_cfg() -> UserConfig:
    return UserConfig()


def cfg(**overrides) -> UserConfig:
    base = default_cfg()
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def call(**kwargs):
    defaults = dict(
        claude_p    = 0.70,
        confidence  = 0.60,
        ask_yes     = 0.55,
        ask_no      = 0.45,
        bankroll    = 1000.0,
        user_config = default_cfg(),
        archetype   = None,
    )
    defaults.update(kwargs)
    return size_position(**defaults)


# ── Gate 1 (side selection): high-confidence override ────────────────────────
class HighConfidenceOverrideTests(unittest.TestCase):
    def test_override_follows_claude_yes_against_market(self):
        # Claude 0.70 YES, market 0.10 YES, confidence 0.80 → YES override.
        d = call(claude_p=0.70, confidence=0.80, ask_yes=0.10, ask_no=0.90)
        self.assertEqual(d.side, "YES")
        self.assertTrue(d.should_trade)

    def test_override_follows_claude_no_against_market(self):
        # Claude 0.15 YES (→ NO lean 0.85), market 0.80 YES, confidence 0.82.
        d = call(claude_p=0.15, confidence=0.82, ask_yes=0.80, ask_no=0.20)
        self.assertEqual(d.side, "NO")
        self.assertAlmostEqual(d.p_win, 0.85, places=6)
        self.assertTrue(d.should_trade)

    def test_override_tie_breaks_to_yes(self):
        # claude_p exactly 0.50, confidence above override → YES.
        d = call(claude_p=0.50, confidence=0.80, ask_yes=0.40, ask_no=0.60)
        self.assertEqual(d.side, "YES")

    def test_override_threshold_is_inclusive(self):
        # confidence exactly at the threshold fires the override.
        d = call(claude_p=0.80, confidence=0.75, ask_yes=0.10, ask_no=0.90,
                 user_config=cfg(confidence_override_threshold=0.75))
        self.assertEqual(d.side, "YES")  # follows Claude, ignores market


# ── Gate 1 (side selection): low-confidence mean rule ────────────────────────
class MeanRuleTests(unittest.TestCase):
    def test_agreement_fires_yes(self):
        # Claude 0.70, market 0.60, confidence 0.50 → mean 0.65 → YES.
        d = call(claude_p=0.70, confidence=0.50, ask_yes=0.60, ask_no=0.40)
        self.assertEqual(d.side, "YES")

    def test_agreement_fires_no(self):
        # Claude 0.20, market 0.30, confidence 0.50 → mean 0.25 → NO.
        d = call(claude_p=0.20, confidence=0.50, ask_yes=0.30, ask_no=0.70)
        self.assertEqual(d.side, "NO")
        self.assertAlmostEqual(d.p_win, 0.80, places=6)

    def test_mean_can_fire_claudes_side_on_disagreement(self):
        # Claude 0.80, market 0.30 → mean 0.55 → YES (Claude's side).
        d = call(claude_p=0.80, confidence=0.60, ask_yes=0.30, ask_no=0.70)
        self.assertEqual(d.side, "YES")

    def test_mean_can_fire_markets_side_on_disagreement(self):
        # Claude 0.70, market 0.10 → mean 0.40 → NO (market's side).
        # Gate 2 will block because p_win on NO = 0.30, below default 0.50.
        d = call(claude_p=0.70, confidence=0.60, ask_yes=0.10, ask_no=0.90)
        self.assertEqual(d.side, "NO")
        self.assertFalse(d.should_trade)
        self.assertIsNotNone(d.skip_reason)
        self.assertIn("p_win", d.skip_reason)

    def test_claude_exactly_half_with_market_support_fires_yes(self):
        # Claude 0.50, market 0.55 → mean 0.525 → YES, p_win = 0.50 passes
        # Gate 2 (min_p_win defaults to 0.50, test uses >=).
        d = call(claude_p=0.50, confidence=0.40, ask_yes=0.55, ask_no=0.45)
        self.assertEqual(d.side, "YES")
        self.assertTrue(d.should_trade)

    def test_mean_boundary_goes_yes(self):
        # Mean exactly 0.50 → YES.
        d = call(claude_p=0.55, confidence=0.40, ask_yes=0.45, ask_no=0.55)
        self.assertEqual(d.side, "YES")


# ── Gate 2: minimum p_win ────────────────────────────────────────────────────
class Gate2MinPwinTests(unittest.TestCase):
    def test_row_20_pattern_blocked_by_gate2(self):
        # Claude 0.25 YES (→ prefers NO with p_win 0.75), market 0.855 YES,
        # confidence 0.60 (below 0.75 override). Mean = (0.25 + 0.855)/2
        # = 0.5525 → side YES, p_win = 0.25. Default min_p_win = 0.50 →
        # Gate 2 skips.
        d = call(claude_p=0.25, confidence=0.60, ask_yes=0.855, ask_no=0.145)
        self.assertEqual(d.side, "YES")
        self.assertAlmostEqual(d.p_win, 0.25, places=6)
        self.assertFalse(d.should_trade)
        self.assertIsNotNone(d.skip_reason)
        self.assertIn("p_win", d.skip_reason)

    def test_default_floor_is_point_five(self):
        u = UserConfig()
        self.assertEqual(u.min_p_win, 0.50)

    def test_exactly_at_threshold_passes(self):
        # p_win exactly 0.50 passes.
        d = call(claude_p=0.50, confidence=0.40, ask_yes=0.55, ask_no=0.45)
        self.assertTrue(d.should_trade)

    def test_below_threshold_skips(self):
        # claude_p=0.45, mean rule fires NO (p_win = 0.55) with market < 0.5.
        # To get p_win below 0.50 we need a mean-rule trade where Claude
        # leans weakly and market picks the side. Use claude_p=0.60,
        # market=0.10, confidence=0.60 → mean 0.35 → NO, p_win=0.40.
        d = call(claude_p=0.60, confidence=0.60, ask_yes=0.10, ask_no=0.90)
        self.assertEqual(d.side, "NO")
        self.assertAlmostEqual(d.p_win, 0.40, places=6)
        self.assertFalse(d.should_trade)
        self.assertIn("p_win", d.skip_reason)

    def test_user_can_raise_threshold(self):
        # With min_p_win=0.75 a p_win of 0.70 is blocked.
        d = call(claude_p=0.70, confidence=0.80, ask_yes=0.10, ask_no=0.90,
                 user_config=cfg(min_p_win=0.75))
        self.assertFalse(d.should_trade)
        self.assertIn("p_win", d.skip_reason)


# ── Gate 3 removed ───────────────────────────────────────────────────────────
# A prior version of this sizer rejected trades whose expected return
# (1/ask - 1 - cost) fell below `min_expected_return`. It was scrapped
# because it muted heavy favourites where the math still favoured the
# bet - e.g. Claude 0.95 on a market pricing YES at 0.94. Side selection
# + the p_win floor is the full skip logic.


# ── Confidence softener ──────────────────────────────────────────────────────
class ConfidenceSoftenerTests(unittest.TestCase):
    def _stake_at(self, confidence: float) -> float:
        # Use claude_p/ask that easily passes Gate 2.
        # Force override so confidence is the only softener input.
        d = call(
            claude_p=0.85, confidence=confidence,
            ask_yes=0.50, ask_no=0.50,
            user_config=cfg(
                confidence_override_threshold=0.01,  # always override
                base_stake_pct=0.02,
                max_stake_pct=0.05,
            ),
            bankroll=10_000.0,
        )
        return d.stake_usd

    def test_zero_confidence_is_one_percent_of_base(self):
        # base = 0.02, multiplier at conf=0 is 0.01 → stake_pct 0.0002 →
        # bankroll 10_000 → $2.00, which equals the $2 absolute floor.
        self.assertGreater(self._stake_at(0.0), 0.0)
        self.assertAlmostEqual(self._stake_at(0.0), 2.0, places=4)

    def test_mid_confidence_is_about_half(self):
        # confidence=0.35 on default full_stake=0.70 → multiplier ≈ 0.505
        # → stake_pct ≈ 0.0101 → bankroll 10_000 → ~$101.
        s = self._stake_at(0.35)
        self.assertAlmostEqual(s, 101.0, delta=2.0)

    def test_full_confidence_is_full_stake(self):
        # confidence=0.70 → multiplier 1.0 → stake_pct = 0.02 → $200.
        self.assertAlmostEqual(self._stake_at(0.70), 200.0, delta=0.5)

    def test_above_full_stays_full(self):
        # confidence=1.0 is capped at 1.0 multiplier → still $200.
        self.assertAlmostEqual(self._stake_at(1.00), 200.0, delta=0.5)

    def test_monotonic_in_confidence(self):
        stakes = [self._stake_at(c) for c in (0.0, 0.1, 0.3, 0.5, 0.7, 0.9)]
        # Non-decreasing - the softener never drops with more confidence.
        for a, b in zip(stakes, stakes[1:]):
            self.assertLessEqual(a, b + 1e-9)

    def test_softener_never_skips(self):
        # Confidence 0.0 still trades (at $2 absolute floor), as long as
        # Gate 2 is satisfied. Historical "confidence_skip_floor" is gone.
        d = call(claude_p=0.85, confidence=0.0,
                 ask_yes=0.50, ask_no=0.50,
                 user_config=cfg(confidence_override_threshold=0.01),
                 bankroll=10_000.0)
        self.assertTrue(d.should_trade)
        self.assertGreater(d.stake_usd, 0.0)


# ── Invariants ──────────────────────────────────────────────────────────────
class InvariantTests(unittest.TestCase):
    def test_ev_field_always_zero(self):
        d = call(claude_p=0.80, confidence=0.80, ask_yes=0.60, ask_no=0.40)
        self.assertEqual(d.ev, 0.0)

    def test_max_stake_cap(self):
        # base=0.05 with max=0.03 and full confidence → stake capped at 3%.
        d = call(claude_p=0.85, confidence=0.80, ask_yes=0.50, ask_no=0.50,
                 user_config=cfg(base_stake_pct=0.05, max_stake_pct=0.03),
                 bankroll=10_000.0)
        self.assertAlmostEqual(d.stake_usd, 300.0, delta=0.5)

    def test_min_absolute_stake_two_dollars(self):
        # Tiny bankroll - the $2 floor still applies.
        d = call(claude_p=0.85, confidence=0.80, ask_yes=0.50, ask_no=0.50,
                 bankroll=1.0)
        # Absolute floor overrides bankroll percentage.
        self.assertGreaterEqual(d.stake_usd, 2.0)

    def test_zero_bankroll_skips(self):
        d = call(claude_p=0.85, confidence=0.80, ask_yes=0.50, ask_no=0.50,
                 bankroll=0.0)
        self.assertFalse(d.should_trade)

    def test_archetype_skip_outranks_gates(self):
        d = call(
            claude_p=0.90, confidence=0.90, ask_yes=0.20, ask_no=0.80,
            user_config=cfg(archetype_skip_list=("tennis",)),
            archetype="tennis",
        )
        self.assertFalse(d.should_trade)
        self.assertIn("skip list", d.skip_reason or "")

    def test_no_direction_skip_reason_emitted(self):
        # No combination of inputs should produce a "direction" skip; the
        # new Gate 1 never skips. Probe the old contrarian case that used
        # to trip direction-disagreement.
        d = call(claude_p=0.25, confidence=0.60, ask_yes=0.85, ask_no=0.15)
        reason = (d.skip_reason or "").lower()
        self.assertNotIn("direction disagreement", reason)
        self.assertNotIn("no direction", reason)

    def test_no_confidence_floor_skip_reason_emitted(self):
        d = call(claude_p=0.85, confidence=0.00, ask_yes=0.50, ask_no=0.50,
                 user_config=cfg(confidence_override_threshold=0.01),
                 bankroll=10_000.0)
        self.assertIsNone(d.skip_reason)


# ── Archetype stake multiplier ───────────────────────────────────────────────
class ArchetypeStakeMultiplierTests(unittest.TestCase):
    """
    `archetype_stake_multipliers` composes multiplicatively with the
    confidence softener. `max_stake_pct` still caps the final stake.
    """

    def _full_conf_cfg(self, **overrides) -> UserConfig:
        base = cfg(
            confidence_override_threshold=0.01,  # always override
            base_stake_pct=0.02,
            max_stake_pct=0.05,
        )
        for k, v in overrides.items():
            setattr(base, k, v)
        return base

    def _call(self, archetype, user_config):
        # conf=1.0 → softener multiplier = 1.0, so stake reflects arch_mult cleanly.
        return call(
            claude_p=0.85, confidence=1.0,
            ask_yes=0.50, ask_no=0.50,
            user_config=user_config, bankroll=10_000.0,
            archetype=archetype,
        )

    def test_archetype_none_preserves_behaviour(self):
        # conf=1.0, base_stake_pct=0.02, bankroll 10k → $200.
        d = self._call(None, self._full_conf_cfg())
        self.assertAlmostEqual(d.stake_usd, 200.0, delta=0.5)

    def test_half_multiplier_halves_stake(self):
        cfg_ = self._full_conf_cfg(
            archetype_stake_multipliers={"sports": 0.5},
        )
        d = self._call("sports", cfg_)
        self.assertAlmostEqual(d.stake_usd, 100.0, delta=0.5)

    def test_double_multiplier_doubles_stake_up_to_max_cap(self):
        # 0.02 * 2.0 = 0.04, under max_stake_pct=0.05 → $400.
        cfg_ = self._full_conf_cfg(
            archetype_stake_multipliers={"markets": 2.0},
        )
        d = self._call("markets", cfg_)
        self.assertAlmostEqual(d.stake_usd, 400.0, delta=0.5)

    def test_multiplier_capped_by_max_stake_pct(self):
        # 0.02 * 5.0 = 0.10, clipped by max_stake_pct=0.05 → $500.
        cfg_ = self._full_conf_cfg(
            archetype_stake_multipliers={"markets": 5.0},
        )
        d = self._call("markets", cfg_)
        self.assertAlmostEqual(d.stake_usd, 500.0, delta=0.5)

    def test_unknown_archetype_defaults_to_one(self):
        cfg_ = self._full_conf_cfg(
            archetype_stake_multipliers={"sports": 2.0},
        )
        # archetype="weather" not in map → multiplier 1.0 → $200.
        d = self._call("weather", cfg_)
        self.assertAlmostEqual(d.stake_usd, 200.0, delta=0.5)

    def test_value_below_floor_clamps_to_min(self):
        # 0.01 below the 0.1 floor → clamps to 0.1 → 0.02 * 0.1 = 0.002 →
        # $20, which is above the $2 absolute floor.
        cfg_ = self._full_conf_cfg(
            archetype_stake_multipliers={"sports": 0.01},
        )
        d = self._call("sports", cfg_)
        self.assertAlmostEqual(d.stake_usd, 20.0, delta=0.5)

    def test_value_above_ceiling_clamps_to_max(self):
        # 50.0 clamps to 10.0 → 0.02 * 10 = 0.20 → capped by max_stake_pct=0.05 → $500.
        cfg_ = self._full_conf_cfg(
            archetype_stake_multipliers={"markets": 50.0},
        )
        d = self._call("markets", cfg_)
        self.assertAlmostEqual(d.stake_usd, 500.0, delta=0.5)

    def test_non_numeric_multiplier_falls_back_to_one(self):
        cfg_ = self._full_conf_cfg(
            archetype_stake_multipliers={"sports": "not-a-number"},
        )
        d = self._call("sports", cfg_)
        self.assertAlmostEqual(d.stake_usd, 200.0, delta=0.5)

    def test_missing_attr_falls_back_to_one(self):
        # Strip the attribute entirely to simulate an older config path.
        cfg_ = self._full_conf_cfg()
        try:
            delattr(cfg_, "archetype_stake_multipliers")
        except AttributeError:
            pass
        d = self._call("sports", cfg_)
        self.assertAlmostEqual(d.stake_usd, 200.0, delta=0.5)


# ── Row-#20 regression (the "big NO win" the prior rule killed) ─────────────
class Row20ScenarioTests(unittest.TestCase):
    """
    The exact market the user recalled: ask_yes=0.855 (so NO is cheap at
    ~0.145), Claude forecast 0.25 for YES (→ prefers NO with p_win 0.75),
    confidence 0.60.

    Under the new rule (confidence below 0.75 override):
        mean = (0.25 + 0.855) / 2 = 0.5525 → side YES
        p_win on YES = 0.25 → Gate 2 blocks (0.25 < 0.50).

    This is the spec's documented expected behaviour - SKIP via Gate 2,
    not via Gate 1.
    """

    def test_skips_via_gate2(self):
        d = call(claude_p=0.25, confidence=0.60,
                 ask_yes=0.855, ask_no=0.145)
        self.assertEqual(d.side, "YES")  # mean rule picked YES
        self.assertAlmostEqual(d.p_win, 0.25, places=6)
        self.assertFalse(d.should_trade)
        self.assertIn("p_win", d.skip_reason)

    def test_would_fire_under_confidence_override(self):
        # Same market but with confidence 0.80 - override follows Claude,
        # side NO, p_win 0.75 passes Gate 2.
        d = call(claude_p=0.25, confidence=0.80,
                 ask_yes=0.855, ask_no=0.145)
        self.assertEqual(d.side, "NO")
        self.assertAlmostEqual(d.p_win, 0.75, places=6)
        self.assertTrue(d.should_trade)


if __name__ == "__main__":
    unittest.main()
