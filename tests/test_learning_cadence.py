"""
Phase 3 tests — trade-volume learning cadence (pure-function proposer).

The cadence is trade-volume-gated and reads DB state to decide whether
to run. Tests here focus on the proposer itself: given a stats bundle
and a UserConfig, does it emit the right suggestions and honour the
n ≥ 20 gate?
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.learning_cadence import (
    ADVISORY_PARAMS,
    ARCHETYPE_BRIER_THRESHOLD,
    CALIBRATION_GAP_THRESHOLD,
    COST_CORRECTION_MIN_N,
    COST_DELTA_THRESHOLD,
    LEARNING_CYCLE_TRADE_INTERVAL,
    MIN_BUCKET_N,
    SELECTION_LOOSEN_MIN_N,
    STRICT_BUCKET_N,
    propose_suggestions,
)
from engine.user_config import UserConfig


EMPTY_DIAG: dict = {
    "brier_by_archetype": [],
    "calibration_curve":  {"scope": "all", "total": 0, "bins": []},
    "cost_validation":    {"n": 0, "assumed_cost": 0.015, "implied_cost": None,
                           "usable": False},
    "selection_quality":  {"traded": {"n": 0, "roi": None},
                           "skipped_counterfactual": {"n": 0, "roi": None}},
    "roi_by_ev_bucket":   [],
}


def stats(n=50, roi=0.0, win_rate=0.5, peak_dd=0.0, by_category=None):
    return {
        "recent_window": {
            "n":                 n,
            "roi":               roi,
            "win_rate":          win_rate,
            "peak_drawdown_pct": peak_dd,
            "by_category":       by_category or {},
        }
    }


def neutral_stats():
    return stats(n=50, roi=0.01, win_rate=0.52, peak_dd=0.05)


class GatesTests(unittest.TestCase):
    def test_min_bucket_n_is_20(self):
        self.assertEqual(MIN_BUCKET_N, 20)

    def test_cycle_interval_is_50(self):
        self.assertEqual(LEARNING_CYCLE_TRADE_INTERVAL, 50)

    def test_no_proposals_below_min_bucket(self):
        cfg = UserConfig()
        for n in (0, 5, 10, MIN_BUCKET_N - 1):
            out = propose_suggestions(
                stats(n=n, roi=-0.20, win_rate=0.30, peak_dd=0.50),
                cfg,
                diag=EMPTY_DIAG,
            )
            self.assertEqual(out, [], f"expected no proposals at n={n}")


class LosingStreakProposesHigherThresholdTests(unittest.TestCase):
    def test_raise_min_ev(self):
        cfg = UserConfig(min_ev_threshold=0.03)
        out = propose_suggestions(
            stats(n=50, roi=-0.10, win_rate=0.35),
            cfg,
            diag=EMPTY_DIAG,
        )
        self.assertTrue(
            any(p.param_name == "min_ev_threshold"
                and p.proposed_value > cfg.min_ev_threshold
                for p in out),
            f"expected a min_ev_threshold raise, got {out}",
        )

    def test_proposed_value_within_bounds(self):
        cfg = UserConfig(min_ev_threshold=0.09)
        out = propose_suggestions(
            stats(n=50, roi=-0.15, win_rate=0.30),
            cfg,
            diag=EMPTY_DIAG,
        )
        # 0.09 × 1.3 = 0.117, capped at 0.10.
        for p in out:
            if p.param_name == "min_ev_threshold":
                self.assertLessEqual(p.proposed_value, 0.10)


class WinningStreakProposesLowerThresholdTests(unittest.TestCase):
    def test_lower_min_ev(self):
        cfg = UserConfig(min_ev_threshold=0.05)
        out = propose_suggestions(
            stats(n=50, roi=0.15, win_rate=0.60),
            cfg,
            diag=EMPTY_DIAG,
        )
        self.assertTrue(
            any(p.param_name == "min_ev_threshold"
                and p.proposed_value < cfg.min_ev_threshold
                for p in out),
            f"expected a min_ev_threshold reduction, got {out}",
        )

    def test_not_below_lower_bound(self):
        cfg = UserConfig(min_ev_threshold=0.015)
        out = propose_suggestions(
            stats(n=50, roi=0.20, win_rate=0.65),
            cfg,
            diag=EMPTY_DIAG,
        )
        # 0.015 × 0.8 = 0.012, clamped to lower bound 0.01. Only emit
        # if the delta is meaningful — 0.015 → 0.012 is 0.003 < 0.005, so
        # the proposer should not emit here.
        for p in out:
            if p.param_name == "min_ev_threshold":
                self.assertGreaterEqual(p.proposed_value, 0.01)


class DrawdownProposesLowerMaxStakeTests(unittest.TestCase):
    def test_cut_max_stake_after_drawdown(self):
        cfg = UserConfig(max_stake_pct=0.05)
        out = propose_suggestions(
            stats(n=50, roi=-0.10, win_rate=0.40, peak_dd=0.35),
            cfg,
            diag=EMPTY_DIAG,
        )
        self.assertTrue(
            any(p.param_name == "max_stake_pct"
                and p.proposed_value < cfg.max_stake_pct
                for p in out),
        )

    def test_no_cut_when_drawdown_small(self):
        cfg = UserConfig(max_stake_pct=0.05)
        out = propose_suggestions(
            stats(n=50, roi=0.02, win_rate=0.50, peak_dd=0.05),
            cfg,
            diag=EMPTY_DIAG,
        )
        self.assertFalse(
            any(p.param_name == "max_stake_pct" for p in out),
        )


class NoProposalsWhenStableTests(unittest.TestCase):
    def test_nothing_proposed_when_performance_neutral(self):
        cfg = UserConfig()
        out = propose_suggestions(
            stats(n=50, roi=0.01, win_rate=0.52, peak_dd=0.05),
            cfg,
            diag=EMPTY_DIAG,
        )
        self.assertEqual(out, [])


# ── Diagnostic-driven proposers (Commit 3) ──────────────────────────────────
def _pick(out, name):
    return [p for p in out if p.param_name == name]


class ArchetypeThresholdProposerTests(unittest.TestCase):
    def test_emits_for_high_brier_archetype_with_strict_sample(self):
        diag = dict(EMPTY_DIAG, brier_by_archetype=[
            {"archetype": "politics", "n": STRICT_BUCKET_N + 5,
             "brier": ARCHETYPE_BRIER_THRESHOLD + 0.06},
        ])
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        props = _pick(out, "archetype_skip_list")
        self.assertEqual(len(props), 1)
        self.assertIn("politics", props[0].evidence)
        self.assertIsNone(props[0].current_value)
        self.assertIsNone(props[0].proposed_value)

    def test_no_emit_below_strict_bucket_n(self):
        diag = dict(EMPTY_DIAG, brier_by_archetype=[
            {"archetype": "politics", "n": STRICT_BUCKET_N - 1, "brier": 0.5},
        ])
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        self.assertEqual(_pick(out, "archetype_skip_list"), [])

    def test_no_emit_when_brier_under_threshold(self):
        diag = dict(EMPTY_DIAG, brier_by_archetype=[
            {"archetype": "sports", "n": 100,
             "brier": ARCHETYPE_BRIER_THRESHOLD - 0.05},
        ])
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        self.assertEqual(_pick(out, "archetype_skip_list"), [])


class CalibrationShrinkageProposerTests(unittest.TestCase):
    def test_emits_cap_for_overconfident_high_p_bin(self):
        bins = [
            {"lo": 0.7, "hi": 0.8, "n": STRICT_BUCKET_N + 10,
             "mean_pred": 0.75, "mean_actual": 0.58},   # gap 0.17
            {"lo": 0.8, "hi": 0.9, "n": STRICT_BUCKET_N,
             "mean_pred": 0.85, "mean_actual": 0.80},   # gap 0.05 (sub-threshold)
        ]
        diag = dict(EMPTY_DIAG,
                    calibration_curve={"scope": "all", "total": 200, "bins": bins})
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        props = _pick(out, "probability_cap")
        self.assertEqual(len(props), 1)
        # Cap is actual + 0.02, clipped to [0.5, 0.95].
        self.assertAlmostEqual(props[0].proposed_value, 0.60, places=3)

    def test_no_emit_when_gap_below_threshold(self):
        bins = [
            {"lo": 0.8, "hi": 0.9, "n": 100,
             "mean_pred": 0.85, "mean_actual": 0.80},
        ]
        diag = dict(EMPTY_DIAG,
                    calibration_curve={"scope": "all", "total": 100, "bins": bins})
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        self.assertEqual(_pick(out, "probability_cap"), [])

    def test_no_emit_for_low_p_bins(self):
        bins = [
            {"lo": 0.1, "hi": 0.2, "n": 100,
             "mean_pred": 0.15, "mean_actual": 0.00},   # gap 0.15 but low-p
        ]
        diag = dict(EMPTY_DIAG,
                    calibration_curve={"scope": "all", "total": 100, "bins": bins})
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        self.assertEqual(_pick(out, "probability_cap"), [])

    def test_no_emit_below_strict_bucket_n(self):
        bins = [
            {"lo": 0.8, "hi": 0.9, "n": STRICT_BUCKET_N - 1,
             "mean_pred": 0.85, "mean_actual": 0.60},
        ]
        diag = dict(EMPTY_DIAG,
                    calibration_curve={"scope": "all", "total": 25, "bins": bins})
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        self.assertEqual(_pick(out, "probability_cap"), [])


class CostCorrectionProposerTests(unittest.TestCase):
    def test_emits_when_implied_exceeds_assumed(self):
        diag = dict(EMPTY_DIAG, cost_validation={
            "n": COST_CORRECTION_MIN_N + 10,
            "assumed_cost": 0.015,
            "implied_cost": 0.015 + COST_DELTA_THRESHOLD + 0.004,   # 0.024
        })
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        props = _pick(out, "cost_assumption_override")
        self.assertEqual(len(props), 1)
        self.assertAlmostEqual(props[0].current_value, 0.015, places=4)
        self.assertAlmostEqual(props[0].proposed_value, 0.024, places=4)

    def test_no_emit_when_within_tolerance(self):
        diag = dict(EMPTY_DIAG, cost_validation={
            "n": 100, "assumed_cost": 0.015, "implied_cost": 0.016,
        })
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        self.assertEqual(_pick(out, "cost_assumption_override"), [])

    def test_no_emit_below_sample_gate(self):
        diag = dict(EMPTY_DIAG, cost_validation={
            "n": COST_CORRECTION_MIN_N - 1,
            "assumed_cost": 0.015, "implied_cost": 0.050,
        })
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        self.assertEqual(_pick(out, "cost_assumption_override"), [])


class SelectionLooseningProposerTests(unittest.TestCase):
    def test_emits_lower_min_ev_when_skipped_beats_traded(self):
        diag = dict(EMPTY_DIAG, selection_quality={
            "traded":                 {"n": SELECTION_LOOSEN_MIN_N + 1, "roi": -0.02},
            "skipped_counterfactual": {"n": SELECTION_LOOSEN_MIN_N + 5, "roi": 0.12},
        })
        cfg = UserConfig(min_ev_threshold=0.05)
        out = propose_suggestions(neutral_stats(), cfg, diag=diag)
        props = [p for p in _pick(out, "min_ev_threshold")
                 if p.proposed_value < cfg.min_ev_threshold]
        self.assertTrue(props, f"expected a loosening proposal, got {out}")

    def test_no_emit_when_traded_beats_skipped(self):
        diag = dict(EMPTY_DIAG, selection_quality={
            "traded":                 {"n": 100, "roi": 0.08},
            "skipped_counterfactual": {"n": 100, "roi": 0.02},
        })
        cfg = UserConfig(min_ev_threshold=0.05)
        out = propose_suggestions(neutral_stats(), cfg, diag=diag)
        # None of the proposals should be a loosening from selection-gate logic.
        looseners = [p for p in _pick(out, "min_ev_threshold")
                     if p.proposed_value < cfg.min_ev_threshold]
        self.assertEqual(looseners, [])

    def test_no_emit_below_sample_gate(self):
        diag = dict(EMPTY_DIAG, selection_quality={
            "traded":                 {"n": 10, "roi": -0.10},
            "skipped_counterfactual": {"n": 100, "roi": 0.30},
        })
        cfg = UserConfig(min_ev_threshold=0.05)
        out = propose_suggestions(neutral_stats(), cfg, diag=diag)
        looseners = [p for p in _pick(out, "min_ev_threshold")
                     if p.proposed_value < cfg.min_ev_threshold]
        self.assertEqual(looseners, [])


class EvBucketExcludeProposerTests(unittest.TestCase):
    def test_emits_for_negative_roi_bucket(self):
        diag = dict(EMPTY_DIAG, roi_by_ev_bucket=[
            {"bucket": "0-2%",  "n": MIN_BUCKET_N + 5,  "roi": -0.10},
            {"bucket": "2-5%",  "n": MIN_BUCKET_N + 20, "roi":  0.05},
            {"bucket": "5-10%", "n": MIN_BUCKET_N - 1,  "roi": -0.50},  # under n gate
        ])
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        props = _pick(out, "ev_bucket_skip_list")
        self.assertEqual(len(props), 1)
        self.assertIn("0-2%", props[0].evidence)

    def test_no_emit_for_positive_roi_bucket(self):
        diag = dict(EMPTY_DIAG, roi_by_ev_bucket=[
            {"bucket": "2-5%", "n": 100, "roi": 0.06},
        ])
        out = propose_suggestions(neutral_stats(), UserConfig(), diag=diag)
        self.assertEqual(_pick(out, "ev_bucket_skip_list"), [])


class AdvisoryProposalsHaveNoBacktestTests(unittest.TestCase):
    """Advisory proposals must not be backtested: their target sizer fields
    don't exist on UserConfig yet. `_attach_backtest_delta` returns early on
    any param_name in ADVISORY_PARAMS."""

    def test_advisory_params_set_covers_every_advisory_proposer(self):
        expected = {
            "archetype_skip_list",
            "probability_cap",
            "cost_assumption_override",
            "ev_bucket_skip_list",
        }
        self.assertEqual(ADVISORY_PARAMS, expected)

    def test_attach_backtest_delta_skips_advisory(self):
        from engine.learning_cadence import Proposal, _attach_backtest_delta
        prop = Proposal(
            param_name="archetype_skip_list",
            current_value=None, proposed_value=None,
            evidence="test",
        )
        # Should be a no-op even without DB or backtester available.
        _attach_backtest_delta(prop, UserConfig())
        self.assertIsNone(prop.backtest_delta)
        self.assertIsNone(prop.backtest_trades)


if __name__ == "__main__":
    unittest.main()
