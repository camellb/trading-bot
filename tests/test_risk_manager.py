"""
Phase 2 tests — risk manager circuit breakers.

The manager reads settled P&L / peak equity / streak counts from the DB.
We mock those helpers and exercise the pure verdict logic: breakers halt,
streak cooldown halves stake, dry powder reserve carves off effective
bankroll, DB failures fall through to a permissive verdict.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.user_config import UserConfig
from engine import risk_manager as rm


def stub_stats(**overrides):
    """Returns a patcher context manager that mocks the DB-reading helpers."""
    defaults = {
        "_pnl_since":          lambda *a, **k: 0.0,
        "_realized_total":     lambda *a, **k: 0.0,
        "_peak_equity":        lambda *a, **k: 1000.0,
        "_consecutive_losses": lambda *a, **k: 0,
    }
    # Allow overrides to pass raw numbers in addition to callables.
    patches = {}
    for key, default in defaults.items():
        val = overrides.get(key.lstrip("_"), default)
        if not callable(val):
            constant = val
            val = lambda *a, _c=constant, **k: _c
        patches[key] = val

    class _Ctx:
        def __enter__(self_):
            self_.patchers = [
                mock.patch.object(rm, name, side_effect=fn)
                for name, fn in patches.items()
            ]
            for p in self_.patchers:
                p.start()
            return self_

        def __exit__(self_, *args):
            for p in self_.patchers:
                p.stop()
    return _Ctx()


class NominalPathTests(unittest.TestCase):
    def test_no_halts_applies_dry_powder(self):
        cfg = UserConfig()
        with stub_stats():
            v = rm.evaluate(user_config=cfg, bankroll=1000.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertFalse(v.halted)
        self.assertEqual(v.stake_multiplier, 1.0)
        # 20% reserve → sizer sees $800.
        self.assertAlmostEqual(v.effective_bankroll, 800.0, places=2)


class DailyLossBreakerTests(unittest.TestCase):
    def test_trips_when_daily_pnl_below_limit(self):
        cfg = UserConfig(daily_loss_limit_pct=0.10)
        # -$101 on $1000 starting = -10.1%, past the -10% limit.
        with stub_stats(pnl_since=-101.0):
            v = rm.evaluate(user_config=cfg, bankroll=1000.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertTrue(v.halted)
        self.assertIn("daily loss", v.halt_reason.lower())
        self.assertEqual(v.effective_bankroll, 0.0)

    def test_does_not_trip_under_limit(self):
        cfg = UserConfig(daily_loss_limit_pct=0.10)
        with stub_stats(pnl_since=-50.0):
            v = rm.evaluate(user_config=cfg, bankroll=950.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertFalse(v.halted)


class WeeklyLossBreakerTests(unittest.TestCase):
    def test_trips_on_weekly_limit(self):
        cfg = UserConfig()
        # Daily pnl just under the -10% daily limit, weekly pnl past -20%.
        call_count = {"n": 0}

        def pnl_side_effect(mode, hours):
            # hours=24 → today, hours=168 → weekly.
            return -50.0 if hours == 24 else -250.0

        with stub_stats(pnl_since=pnl_side_effect):
            v = rm.evaluate(user_config=cfg, bankroll=750.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertTrue(v.halted)
        self.assertIn("weekly", v.halt_reason.lower())


class DrawdownHaltTests(unittest.TestCase):
    def test_halts_on_drawdown(self):
        cfg = UserConfig(drawdown_halt_pct=0.40)
        with stub_stats(peak_equity=2000.0, realized_total=-900.0):
            # current_equity = starting + realized_total = 1000 - 900 = 100
            # drawdown = 1 - 100/2000 = 95%, past 40% threshold.
            v = rm.evaluate(user_config=cfg, bankroll=100.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertTrue(v.halted)
        self.assertIn("drawdown", v.halt_reason.lower())

    def test_does_not_halt_below_threshold(self):
        cfg = UserConfig(drawdown_halt_pct=0.40)
        # Peak 1100, current 1000: 9% drawdown — well under 40%.
        with stub_stats(peak_equity=1100.0, realized_total=0.0):
            v = rm.evaluate(user_config=cfg, bankroll=1000.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertFalse(v.halted)


class StreakCooldownTests(unittest.TestCase):
    def test_halves_stake_at_threshold(self):
        cfg = UserConfig(streak_cooldown_losses=3)
        with stub_stats(consecutive_losses=3):
            v = rm.evaluate(user_config=cfg, bankroll=1000.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertFalse(v.halted)
        self.assertEqual(v.stake_multiplier, 0.5)
        self.assertIn("streak", v.notes.lower())

    def test_does_not_trigger_below_threshold(self):
        cfg = UserConfig(streak_cooldown_losses=3)
        with stub_stats(consecutive_losses=2):
            v = rm.evaluate(user_config=cfg, bankroll=1000.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertEqual(v.stake_multiplier, 1.0)

    def test_triggers_above_threshold(self):
        cfg = UserConfig(streak_cooldown_losses=3)
        with stub_stats(consecutive_losses=7):
            v = rm.evaluate(user_config=cfg, bankroll=1000.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertEqual(v.stake_multiplier, 0.5)


class DryPowderReserveTests(unittest.TestCase):
    def test_reserve_reduces_effective_bankroll(self):
        cfg = UserConfig(dry_powder_reserve_pct=0.25)
        with stub_stats():
            v = rm.evaluate(user_config=cfg, bankroll=1000.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertAlmostEqual(v.effective_bankroll, 750.0, places=2)


class FailSafeTests(unittest.TestCase):
    def test_db_failure_returns_permissive_verdict(self):
        cfg = UserConfig()
        with mock.patch.object(rm, "_pnl_since", side_effect=RuntimeError("db down")):
            v = rm.evaluate(user_config=cfg, bankroll=1000.0,
                            starting_cash=1000.0, mode="shadow")
        self.assertFalse(v.halted)
        self.assertEqual(v.stake_multiplier, 1.0)
        # Dry powder still applied.
        self.assertAlmostEqual(v.effective_bankroll, 800.0, places=2)
        self.assertIn("unavailable", v.notes.lower())


if __name__ == "__main__":
    unittest.main()
