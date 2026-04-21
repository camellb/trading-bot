"""
Phase 2 tests — per-user risk configuration.

DB round-trips need a live PostgreSQL; these tests stay in pure-function
territory (cast + bounds validation + the UserConfig dataclass itself).
Integration tests for the INSERT / UPDATE path live in the main test
suite against a real DB.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.user_config import (
    UserConfig,
    USER_CONFIG_BOUNDS,
    USER_CONFIG_DESCRIPTIONS,
    cast_value,
    validate_user_config_value,
    validated_update_payload,
)


class DefaultsMatchDoctrineTests(unittest.TestCase):
    """Defaults must match the Delfi doctrine table verbatim."""

    def test_defaults(self):
        u = UserConfig()
        self.assertAlmostEqual(u.min_ev_threshold,       0.03)
        self.assertAlmostEqual(u.base_stake_pct,         0.02)
        self.assertAlmostEqual(u.max_stake_pct,          0.05)
        self.assertAlmostEqual(u.daily_loss_limit_pct,   0.10)
        self.assertAlmostEqual(u.weekly_loss_limit_pct,  0.20)
        self.assertAlmostEqual(u.drawdown_halt_pct,      0.40)
        self.assertEqual      (u.streak_cooldown_losses, 3)
        self.assertAlmostEqual(u.dry_powder_reserve_pct, 0.20)

    def test_every_field_has_bounds(self):
        for fld in UserConfig.__dataclass_fields__:
            self.assertIn(fld, USER_CONFIG_BOUNDS)
            self.assertIn(fld, USER_CONFIG_DESCRIPTIONS)


class BoundsValidationTests(unittest.TestCase):
    def test_in_range_accepted(self):
        # At the lower bound, at the upper bound, and in the middle.
        validate_user_config_value("min_ev_threshold", 0.01)
        validate_user_config_value("min_ev_threshold", 0.10)
        validate_user_config_value("min_ev_threshold", 0.05)

    def test_below_min_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_user_config_value("min_ev_threshold", 0.009)
        self.assertIn("min_ev_threshold", str(ctx.exception))

    def test_above_max_rejected(self):
        with self.assertRaises(ValueError):
            validate_user_config_value("max_stake_pct", 0.11)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            validate_user_config_value("not_a_real_field", 0.01)

    def test_bounds_bracket_the_default_for_every_field(self):
        u = UserConfig()
        for k, (lo, hi) in USER_CONFIG_BOUNDS.items():
            v = getattr(u, k)
            self.assertGreaterEqual(v, lo, f"{k} default {v} below lower bound {lo}")
            self.assertLessEqual   (v, hi, f"{k} default {v} above upper bound {hi}")


class CastValueTests(unittest.TestCase):
    def test_float_field_accepts_str_and_int(self):
        self.assertEqual(cast_value("min_ev_threshold", "0.05"), 0.05)
        self.assertEqual(cast_value("min_ev_threshold", 1),      1.0)

    def test_int_field_requires_int_like(self):
        self.assertEqual(cast_value("streak_cooldown_losses", 4),   4)
        self.assertEqual(cast_value("streak_cooldown_losses", "5"), 5)
        with self.assertRaises(ValueError):
            cast_value("streak_cooldown_losses", "not_a_number")

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            cast_value("not_a_field", 1)


class ValidatedUpdatePayloadTests(unittest.TestCase):
    def test_valid_multi_field_update(self):
        payload = {"min_ev_threshold": "0.04", "base_stake_pct": 0.015}
        clean = validated_update_payload(payload)
        self.assertEqual(clean["min_ev_threshold"], 0.04)
        self.assertEqual(clean["base_stake_pct"],   0.015)

    def test_mixed_valid_and_invalid_raises(self):
        payload = {"min_ev_threshold": 0.04, "max_stake_pct": 0.50}
        with self.assertRaises(ValueError):
            validated_update_payload(payload)

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            validated_update_payload({"bogus_field": 1})

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            validated_update_payload("not a dict")


class SystemSafetyBoundsTests(unittest.TestCase):
    """The bounds must prevent obviously catastrophic settings."""

    def test_max_stake_can_never_exceed_10pct(self):
        _, hi = USER_CONFIG_BOUNDS["max_stake_pct"]
        self.assertLessEqual(hi, 0.10)

    def test_base_stake_never_exceeds_5pct(self):
        _, hi = USER_CONFIG_BOUNDS["base_stake_pct"]
        self.assertLessEqual(hi, 0.05)

    def test_min_ev_threshold_always_positive(self):
        lo, _ = USER_CONFIG_BOUNDS["min_ev_threshold"]
        self.assertGreater(lo, 0.0)

    def test_dry_powder_reserve_always_at_least_10pct(self):
        lo, _ = USER_CONFIG_BOUNDS["dry_powder_reserve_pct"]
        self.assertGreaterEqual(lo, 0.10)


if __name__ == "__main__":
    unittest.main()
