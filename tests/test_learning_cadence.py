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
    MIN_BUCKET_N,
    LEARNING_CYCLE_TRADE_INTERVAL,
    propose_suggestions,
)
from engine.user_config import UserConfig


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
            )
            self.assertEqual(out, [], f"expected no proposals at n={n}")


class LosingStreakProposesHigherThresholdTests(unittest.TestCase):
    def test_raise_min_ev(self):
        cfg = UserConfig(min_ev_threshold=0.03)
        out = propose_suggestions(
            stats(n=50, roi=-0.10, win_rate=0.35),
            cfg,
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
        )
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
