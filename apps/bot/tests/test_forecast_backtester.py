"""
Forecast-backtester analytics tests.

The real 90-day pass needs a live database. These tests drive the
pure-function simulator with synthetic evaluations so the distribution
roll-ups (EV buckets - retained for schema continuity, not as a gate -
and archetype breakdown) and core totals are exercised deterministically.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtester.forecast_backtester import (
    EV_BUCKETS,
    Evaluation,
    archetype_distribution,
    ev_bucket_distribution,
    format_phase5_report,
    simulate_with_config,
)
from engine.user_config import UserConfig


def e(market_id: str, yes: float, claude_p: float, conf: float,
      category: str, outcome=None) -> Evaluation:
    return Evaluation(
        market_id=market_id, market_price_yes=yes,
        claude_probability=claude_p, confidence=conf,
        category=category, resolved_outcome=outcome,
    )


class ReplayStructureTests(unittest.TestCase):
    def test_deduplicates_by_market_id(self):
        # Direction-agreeing fixtures so Gate 1 passes: ask_yes > 0.50.
        evals = [
            e("m1", 0.60, 0.70, 0.70, "politics", outcome=1),
            e("m1", 0.62, 0.72, 0.75, "politics", outcome=1),
        ]
        r = simulate_with_config(evals, UserConfig(), starting_cash=1000.0)
        self.assertEqual(r["trades_taken"], 1)

    def test_no_coin_flip_dead_zone(self):
        # Under the mean-rule + override doctrine Gate 1 never skips. A
        # claude_p right next to 0.50 with market support still fires as
        # long as Gate 2 (p_win >= min_p_win) and Gate 3 (expected_return)
        # pass. claude_p=0.55, ask_yes=0.54, conf=0.70 (below override):
        # mean = 0.545 → YES, p_win = 0.55 passes default 0.50, exp_ret
        # ≈ 0.837 passes default 0.05. Trade fires.
        r = simulate_with_config(
            [e("m1", 0.54, 0.55, 0.70, "other")],
            UserConfig(),
            starting_cash=1000.0,
        )
        self.assertEqual(r["trades_taken"], 1)


class BucketDistributionTests(unittest.TestCase):
    def test_each_bucket_label_present(self):
        # Build a synthetic trade list covering every bucket.
        # Use evaluations that produce ev values in each bucket range.
        evals = [
            # ev_yes = 0.70/0.65 - 1 - 0.015 ≈ +0.062 (in 5-10% bucket)
            e("m1", 0.65, 0.70, 0.80, "sports", outcome=1),
            # ev_yes = 0.80/0.50 - 1 - 0.015 = +0.585 (20%+)
            e("m2", 0.50, 0.80, 0.80, "crypto", outcome=0),
            # ev_yes = 0.65/0.55 - 1 - 0.015 ≈ +0.167 (10-20%)
            e("m3", 0.55, 0.65, 0.70, "politics", outcome=1),
            # ev_yes = 0.60/0.56 - 1 - 0.015 ≈ +0.056 (5-10%)
            e("m4", 0.56, 0.60, 0.70, "macro", outcome=1),
        ]
        r = simulate_with_config(evals, UserConfig(), starting_cash=1000.0)
        labels = {b["bucket"] for b in r["ev_buckets"]}
        self.assertSetEqual(labels, {"3-5%", "5-10%", "10-20%", "20%+"})

    def test_bucket_totals_are_empty_under_doctrine(self):
        # Under the back-the-forecast doctrine, SizingDecision.ev is always
        # 0.0, so every simulated trade falls below the first bucket's 3%
        # floor. Bucket n should be zero while trades_taken is positive -
        # the buckets are retained for schema continuity, not reporting.
        evals = [
            e("m1", 0.55, 0.80, 0.80, "sports", outcome=1),
            e("m2", 0.55, 0.65, 0.70, "politics", outcome=0),
            e("m3", 0.56, 0.60, 0.70, "macro", outcome=1),
        ]
        r = simulate_with_config(evals, UserConfig(), starting_cash=1000.0)
        total_in_buckets = sum(b["n"] for b in r["ev_buckets"])
        self.assertEqual(total_in_buckets, 0)
        self.assertGreater(r["trades_taken"], 0)


class ArchetypeDistributionTests(unittest.TestCase):
    def test_groups_by_category(self):
        # All direction-agreeing (ask_yes > 0.50) and p_win >= 0.65.
        evals = [
            e("m1", 0.55, 0.80, 0.80, "sports", outcome=1),
            e("m2", 0.60, 0.80, 0.80, "sports", outcome=1),
            e("m3", 0.60, 0.70, 0.70, "politics", outcome=0),
        ]
        r = simulate_with_config(evals, UserConfig(), starting_cash=1000.0)
        cats = {b["category"]: b for b in r["by_archetype"]}
        self.assertIn("sports", cats)
        self.assertIn("politics", cats)
        self.assertEqual(cats["sports"]["n"], 2)
        self.assertEqual(cats["politics"]["n"], 1)

    def test_missing_category_bucketed_as_other(self):
        trades = [
            type("T", (), {"category": None, "stake_usd": 10.0, "resolved": True,
                           "pnl_usd": 1.0, "ev": 0.05})(),
        ]
        out = archetype_distribution(trades)
        self.assertEqual(out[0]["category"], "other")


class DecisionRuleReportTests(unittest.TestCase):
    def test_report_mentions_required_sections(self):
        r = simulate_with_config(
            [e("m1", 0.55, 0.80, 0.80, "sports", outcome=1)],
            UserConfig(),
            starting_cash=1000.0,
        )
        rep = format_phase5_report(r, old_trade_count=35)
        for needle in ["EV bucket distribution",
                       "Archetype distribution",
                       "Trades taken (new sizer)",
                       "Trades taken (old sizer):   35",
                       "Decision rule"]:
            self.assertIn(needle, rep)


class PnlArithmeticTests(unittest.TestCase):
    def test_win_pays_one_per_share_minus_cost(self):
        # Single YES bet at $0.50 with winning outcome: proceeds = shares
        # (since settlement_price = 1.0 for winners).
        evals = [e("m1", 0.55, 0.80, 0.80, "sports", outcome=1)]
        r = simulate_with_config(evals, UserConfig(), starting_cash=1000.0)
        self.assertEqual(r["wins"], 1)
        self.assertGreater(r["total_pnl"], 0.0)

    def test_loss_costs_the_full_stake(self):
        evals = [e("m1", 0.55, 0.80, 0.80, "sports", outcome=0)]
        r = simulate_with_config(evals, UserConfig(), starting_cash=1000.0)
        self.assertEqual(r["wins"], 0)
        self.assertLess(r["total_pnl"], 0.0)
        # PnL equals −stake.
        trade = r["trades"][0]
        self.assertAlmostEqual(trade.pnl_usd, -trade.stake_usd, places=6)


class BucketConstantsTests(unittest.TestCase):
    def test_buckets_match_doctrine(self):
        labels = [b[0] for b in EV_BUCKETS]
        self.assertEqual(labels, ["3-5%", "5-10%", "10-20%", "20%+"])


if __name__ == "__main__":
    unittest.main()
