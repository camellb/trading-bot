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
    USER_CONFIG_LIST_FIELDS,
    USER_CONFIG_NULLABLE_FIELDS,
    cast_value,
    validate_user_config_value,
    validated_update_payload,
)


class DefaultsMatchDoctrineTests(unittest.TestCase):
    """Defaults must match the Delfi doctrine table verbatim."""

    def test_defaults(self):
        u = UserConfig()
        # Two-gate sizer thresholds + confidence softener.
        self.assertAlmostEqual(u.min_p_win,                     0.50)
        self.assertAlmostEqual(u.confidence_full_stake,         0.70)
        self.assertAlmostEqual(u.confidence_override_threshold, 0.75)
        # Stake sizing.
        self.assertAlmostEqual(u.base_stake_pct,         0.02)
        self.assertAlmostEqual(u.max_stake_pct,          0.05)
        # Circuit breakers.
        self.assertAlmostEqual(u.daily_loss_limit_pct,   0.10)
        self.assertAlmostEqual(u.weekly_loss_limit_pct,  0.20)
        self.assertAlmostEqual(u.drawdown_halt_pct,      0.40)
        self.assertEqual      (u.streak_cooldown_losses, 3)
        self.assertAlmostEqual(u.dry_powder_reserve_pct, 0.20)

    def test_every_field_has_bounds(self):
        # List fields don't use numeric bounds; nullable fields only enforce
        # bounds when the value is set. Every field still has a description.
        for fld in UserConfig.__dataclass_fields__:
            self.assertIn(fld, USER_CONFIG_DESCRIPTIONS)
            if fld in USER_CONFIG_LIST_FIELDS:
                continue
            self.assertIn(fld, USER_CONFIG_BOUNDS)


class BoundsValidationTests(unittest.TestCase):
    def test_in_range_accepted(self):
        # At the lower bound, at the upper bound, and in the middle.
        lo, hi = USER_CONFIG_BOUNDS["min_p_win"]
        validate_user_config_value("min_p_win", lo)
        validate_user_config_value("min_p_win", hi)
        validate_user_config_value("min_p_win", (lo + hi) / 2)

    def test_below_min_rejected(self):
        lo, _ = USER_CONFIG_BOUNDS["min_p_win"]
        with self.assertRaises(ValueError) as ctx:
            validate_user_config_value("min_p_win", lo - 0.001)
        self.assertIn("min_p_win", str(ctx.exception))

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
            # Nullable diagnostic overrides default to None — not subject to
            # bounds until a concrete value is supplied.
            if k in USER_CONFIG_NULLABLE_FIELDS and v is None:
                continue
            self.assertGreaterEqual(v, lo, f"{k} default {v} below lower bound {lo}")
            self.assertLessEqual   (v, hi, f"{k} default {v} above upper bound {hi}")


class CastValueTests(unittest.TestCase):
    def test_float_field_accepts_str_and_int(self):
        self.assertEqual(cast_value("min_p_win",       "0.50"), 0.50)
        self.assertEqual(cast_value("base_stake_pct",  "0.02"), 0.02)

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
        payload = {"min_p_win": "0.55", "base_stake_pct": 0.015}
        clean = validated_update_payload(payload)
        self.assertEqual(clean["min_p_win"],      0.55)
        self.assertEqual(clean["base_stake_pct"], 0.015)

    def test_mixed_valid_and_invalid_raises(self):
        payload = {"min_p_win": 0.55, "max_stake_pct": 0.50}
        with self.assertRaises(ValueError):
            validated_update_payload(payload)

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            validated_update_payload({"bogus_field": 1})

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            validated_update_payload("not a dict")


class DiagnosticOverrideFieldsTests(unittest.TestCase):
    """Diagnostic-driven override fields on UserConfig.

    Under two-gate sizing only two override surfaces remain:
    `cost_assumption_override` (nullable float, retained for future P&L
    modelling) and `archetype_skip_list` (tuple of strings).
    """

    def test_defaults_are_unset(self):
        u = UserConfig()
        self.assertIsNone(u.cost_assumption_override)
        self.assertEqual(u.archetype_skip_list, tuple())

    def test_nullable_cast_accepts_none_and_empty(self):
        self.assertIsNone(cast_value("cost_assumption_override", None))
        self.assertIsNone(cast_value("cost_assumption_override", ""))
        self.assertIsNone(cast_value("cost_assumption_override", "null"))
        self.assertAlmostEqual(cast_value("cost_assumption_override", "0.02"), 0.02)

    def test_list_cast_accepts_csv_and_tuple(self):
        self.assertEqual(cast_value("archetype_skip_list", "politics,sports"),
                         ("politics", "sports"))
        self.assertEqual(cast_value("archetype_skip_list", ["a", "b"]),
                         ("a", "b"))
        self.assertEqual(cast_value("archetype_skip_list", None), tuple())
        self.assertEqual(cast_value("archetype_skip_list", ""), tuple())

    def test_list_cast_strips_and_drops_empties(self):
        self.assertEqual(cast_value("archetype_skip_list", "politics, ,sports,"),
                         ("politics", "sports"))

    def test_validate_accepts_none_for_nullable(self):
        # Must not raise.
        validate_user_config_value("cost_assumption_override", None)

    def test_validate_enforces_bounds_when_value_set(self):
        with self.assertRaises(ValueError):
            validate_user_config_value("cost_assumption_override", 0.5)   # above 0.10

    def test_validate_list_accepts_any_tuple(self):
        validate_user_config_value("archetype_skip_list", ("politics",))
        validate_user_config_value("archetype_skip_list", tuple())

    def test_validate_list_rejects_non_tuple(self):
        with self.assertRaises(ValueError):
            validate_user_config_value("archetype_skip_list", ["not", "a", "tuple"])

    def test_validated_payload_accepts_new_fields(self):
        payload = {
            "cost_assumption_override": "0.025",
            "archetype_skip_list":      "politics,sports",
        }
        clean = validated_update_payload(payload)
        self.assertAlmostEqual(clean["cost_assumption_override"], 0.025)
        self.assertEqual(clean["archetype_skip_list"], ("politics", "sports"))


class SystemSafetyBoundsTests(unittest.TestCase):
    """The bounds must prevent obviously catastrophic settings."""

    def test_max_stake_can_never_exceed_10pct(self):
        _, hi = USER_CONFIG_BOUNDS["max_stake_pct"]
        self.assertLessEqual(hi, 0.10)

    def test_base_stake_never_exceeds_5pct(self):
        _, hi = USER_CONFIG_BOUNDS["base_stake_pct"]
        self.assertLessEqual(hi, 0.05)

    def test_min_p_win_cannot_go_below_50pct(self):
        # A sub-50% p_win floor would let the sizer fire on picks the
        # forecaster thinks are worse than a coin flip.
        lo, _ = USER_CONFIG_BOUNDS["min_p_win"]
        self.assertGreaterEqual(lo, 0.50)

    def test_dry_powder_reserve_always_at_least_10pct(self):
        lo, _ = USER_CONFIG_BOUNDS["dry_powder_reserve_pct"]
        self.assertGreaterEqual(lo, 0.10)


if __name__ == "__main__":
    unittest.main()
