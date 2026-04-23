"""
Unit tests for engine.diagnostics - pure helpers, cache behaviour, and
in-memory stub-engine tests for the sides of the module that exercise SQL
shapes. The full Postgres-specific SQL is not exercised here (would require
a live DB); the tests focus on correctness of helpers, scope filter wiring,
and cache invalidation.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import diagnostics as D


class ScopeClauseTests(unittest.TestCase):
    def test_all_scope_is_truthy(self):
        self.assertIn("TRUE", D._scope_clause("all"))

    def test_traded_scope_filters_on_trade_id_not_null(self):
        self.assertIn("trade_id IS NOT NULL", D._scope_clause("traded"))

    def test_skipped_scope_filters_on_trade_id_null(self):
        self.assertEqual(D._scope_clause("skipped"), "p.trade_id IS NULL")

    def test_unknown_scope_falls_back_to_all(self):
        self.assertIn("TRUE", D._scope_clause("nonsense"))


class HelperMathTests(unittest.TestCase):
    def test_safe_div_handles_zero_denominator(self):
        self.assertIsNone(D._safe_div(1.0, 0.0))

    def test_safe_div_returns_ratio(self):
        self.assertAlmostEqual(D._safe_div(3.0, 4.0), 0.75)

    def test_brier_squared_error(self):
        self.assertAlmostEqual(D._brier(0.8, 1), 0.04)
        self.assertAlmostEqual(D._brier(0.8, 0), 0.64)

    def test_log_loss_is_bounded_below(self):
        # p=1, outcome=0 would blow to +inf without the epsilon clamp.
        out = D._log_loss(1.0, 0)
        self.assertTrue(out < 30.0, f"log_loss should clamp, got {out}")

    def test_log_loss_correct_direction(self):
        small = D._log_loss(0.95, 1)
        large = D._log_loss(0.95, 0)
        self.assertLess(small, large)


class HorizonRangeTests(unittest.TestCase):
    def test_known_buckets_return_bounds(self):
        lo, hi = D._horizon_range("< 1d")
        self.assertEqual((lo, hi), (0.0, 24.0))
        lo, hi = D._horizon_range("30d+")
        self.assertEqual((lo, hi), (720.0, None))

    def test_unknown_bucket_returns_none_tuple(self):
        self.assertEqual(D._horizon_range("bogus"), (None, None))


class CalibrationBinsTests(unittest.TestCase):
    def test_has_ten_equal_width_bins(self):
        self.assertEqual(len(D.CALIBRATION_BINS), 10)
        for lo, hi in D.CALIBRATION_BINS:
            self.assertAlmostEqual(hi - lo, 0.1, places=6)

    def test_bins_are_contiguous_and_cover_zero_to_one(self):
        self.assertEqual(D.CALIBRATION_BINS[0][0], 0.0)
        self.assertEqual(D.CALIBRATION_BINS[-1][1], 1.0)


class CacheBucketTests(unittest.TestCase):
    def test_bucket_is_five_minute_quantum(self):
        b1 = D._cache_bucket()
        self.assertEqual(b1, int(time.time() // D._CACHE_TTL_SECONDS))

    def test_clear_cache_is_idempotent_and_safe(self):
        D.clear_cache()
        D.clear_cache()


class EmptyShapeFallbackTests(unittest.TestCase):
    """If the DB is unavailable, every metric must return a safe zero shape."""

    def setUp(self):
        D.clear_cache()

    def _raise_engine(self, *args, **kwargs):
        raise RuntimeError("no DB in test")

    def test_calibration_curve_returns_zero_bins_on_error(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            out = D.calibration_curve("all")
        self.assertEqual(out["total"], 0)
        self.assertEqual(len(out["bins"]), len(D.CALIBRATION_BINS))
        self.assertTrue(all(b["n"] == 0 and not b["usable"] for b in out["bins"]))

    def test_brier_score_returns_empty_score(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            out = D.brier_score("traded", archetype="politics")
        self.assertEqual(out, {
            "scope": "traded", "archetype": "politics", "horizon": None,
            "n": 0, "brier": None, "mean_pred": None, "mean_actual": None,
            "usable": False,
        })

    def test_log_score_returns_zero_shape(self):
        with patch.object(D, "_fetch_resolved_rows",
                          side_effect=self._raise_engine):
            out = D.log_score("all")
        self.assertEqual(out["n"], 0)
        self.assertIsNone(out["log_loss"])
        self.assertFalse(out["usable"])

    def test_brier_by_archetype_returns_empty_list(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            self.assertEqual(D.brier_by_archetype("all"), [])

    def test_brier_by_horizon_returns_one_row_per_bucket(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            out = D.brier_by_horizon("all")
        self.assertEqual(len(out), len(D.HORIZON_BUCKETS))
        self.assertTrue(all(not r["usable"] for r in out))

    def test_selection_quality_returns_zero_shape(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            out = D.selection_quality()
        self.assertEqual(out["traded"]["n"], 0)
        self.assertEqual(out["skipped_counterfactual"]["n"], 0)
        self.assertFalse(out["traded"]["usable"])

    def test_roi_by_ev_bucket_empty_on_error(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            self.assertEqual(D.roi_by_ev_bucket(), [])

    def test_cost_validation_zero_shape(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            out = D.cost_validation()
        self.assertEqual(out["n"], 0)
        self.assertIsNone(out["implied_cost"])
        self.assertFalse(out["usable"])

    def test_bankroll_series_empty_on_error(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            self.assertEqual(D.bankroll_series(), [])

    def test_theoretical_optimal_zero_shape(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            out = D.theoretical_optimal_roi()
        self.assertEqual(out["n"], 0)
        self.assertIsNone(out["roi"])

    def test_archetype_attribution_empty_on_error(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            self.assertEqual(D.archetype_pnl_attribution(), [])

    def test_full_report_packages_every_section(self):
        with patch.object(D, "_get_engine", side_effect=self._raise_engine):
            out = D.full_report("all")
        for section in ("forecaster", "sizer", "system"):
            self.assertIn(section, out)
        self.assertIn("calibration_curve", out["forecaster"])
        self.assertIn("selection_quality", out["sizer"])
        self.assertIn("bankroll_series", out["system"])


class CacheInvalidationTests(unittest.TestCase):
    def setUp(self):
        D.clear_cache()

    def test_clear_cache_flushes_calibration_impl(self):
        def boom(*a, **kw):
            raise RuntimeError("boom")
        with patch.object(D, "_get_engine", side_effect=boom):
            D.calibration_curve("all")
        info_before = D._calibration_curve_impl.cache_info()
        self.assertGreaterEqual(info_before.currsize, 1)
        D.clear_cache()
        info_after = D._calibration_curve_impl.cache_info()
        self.assertEqual(info_after.currsize, 0)


class SelectionQualitySyntheticTests(unittest.TestCase):
    """
    Exercise the counterfactual PnL arithmetic with a stubbed engine.
    Proves the entry/exit/won math without touching Postgres.
    """

    def setUp(self):
        D.clear_cache()

    def test_counterfactual_math_matches_flat_stake(self):
        # Three skipped predictions; the 0.5-prob one is excluded by rule.
        #   (0.40, 0.70, 1) → YES @0.40, win → shares 25, pnl +15
        #   (0.80, 0.30, 0) → NO  @0.20, win → shares 50, pnl +40
        #   (0.50, 0.50, 1) → skipped (ambiguous direction)
        class StubConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False

            def execute(self, sql, params=None):
                text_sql = str(sql).lower()
                if "pm_positions" in text_sql and "realized_pnl_usd" in text_sql:
                    class R:
                        def fetchone(self_inner): return (0, 0.0, 0.0)
                    return R()
                class R:
                    def fetchall(self_inner):
                        return [
                            (0.40, 0.70, 1),
                            (0.80, 0.30, 0),
                            (0.50, 0.50, 1),
                        ]
                return R()

        class StubEngine:
            def begin(self): return StubConn()

        with patch.object(D, "_get_engine", return_value=StubEngine()):
            out = D.selection_quality()

        cf = out["skipped_counterfactual"]
        self.assertEqual(cf["n"], 2)
        self.assertAlmostEqual(cf["hypothetical_cost"], 20.0)
        self.assertAlmostEqual(cf["hypothetical_pnl"], 55.0)
        self.assertAlmostEqual(cf["roi"], 55.0 / 20.0)


if __name__ == "__main__":
    unittest.main()
