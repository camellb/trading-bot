"""
Phase 3 tests — trade-volume learning cadence (pure-function proposer).

The cadence is trade-volume-gated and reads DB state to decide whether
to run. Tests here focus on the proposer itself: given a stats bundle
and a UserConfig, does it emit the right suggestions and honour the
n ≥ 20 gate?

Under the three-gate sizer doctrine the only proposers that remain are:
  * drawdown pressure → lower `max_stake_pct`
  * archetype Brier > 0.25 → append to `archetype_skip_list`
  * implied cost drifted above assumed → override `cost_assumption_override`

The historical min_ev / calibration-cap / EV-bucket / selection-loosening
proposers are gone with the old paradigm and are no longer tested.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.learning_cadence import (
    ADVISORY_PARAMS,
    ARCHETYPE_BRIER_THRESHOLD,
    COST_CORRECTION_MIN_N,
    COST_DELTA_THRESHOLD,
    LEARNING_CYCLE_TRADE_INTERVAL,
    MIN_BUCKET_N,
    STRICT_BUCKET_N,
    propose_suggestions,
)
from engine.user_config import UserConfig


EMPTY_DIAG: dict = {
    "brier_by_archetype": [],
    "cost_validation":    {"n": 0, "assumed_cost": 0.015, "implied_cost": None,
                           "usable": False},
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


def _pick(out, name):
    return [p for p in out if p.param_name == name]


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


# ── Diagnostic-driven proposers ─────────────────────────────────────────────
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


class AdvisoryProposalsHaveNoBacktestTests(unittest.TestCase):
    """Advisory proposals skip the backtest delta. After the doctrine change
    the only advisory target is `archetype_skip_list` — cost overrides are
    now scalar-valued and do get a backtest delta."""

    def test_advisory_params_is_list_targets_only(self):
        self.assertEqual(ADVISORY_PARAMS, {"archetype_skip_list"})

    def test_attach_backtest_delta_skips_advisory_without_db(self):
        from engine.learning_cadence import Proposal, _attach_backtest_delta
        prop = Proposal(
            param_name="archetype_skip_list",
            current_value=None, proposed_value=None,
            evidence="test",
            proposal_metadata={
                "operation":    "list_append",
                "target_field": "archetype_skip_list",
                "items":        ["politics"],
            },
        )
        # Should be a no-op even without DB / backtester available —
        # exception swallowed by the try/except in _attach_backtest_delta.
        _attach_backtest_delta(prop, UserConfig())
        self.assertIsNone(prop.backtest_delta)
        self.assertIsNone(prop.backtest_trades)


class ProposalMetadataShapeTests(unittest.TestCase):
    """Every proposer must tag proposals with an `operation` so apply_suggestion
    can dispatch. List-append proposers use target_field + items; scalar
    proposers use field + value."""

    def _only(self, props, name):
        hits = [p for p in props if p.param_name == name]
        self.assertEqual(len(hits), 1, f"expected exactly one {name} proposal")
        return hits[0]

    def test_archetype_skip_list_metadata(self):
        diag = dict(EMPTY_DIAG, brier_by_archetype=[
            {"archetype": "politics", "n": STRICT_BUCKET_N + 5,
             "brier": ARCHETYPE_BRIER_THRESHOLD + 0.06},
        ])
        p = self._only(
            propose_suggestions(neutral_stats(), UserConfig(), diag=diag),
            "archetype_skip_list",
        )
        self.assertEqual(p.proposal_metadata, {
            "operation":    "list_append",
            "target_field": "archetype_skip_list",
            "items":        ["politics"],
        })

    def test_scalar_max_stake_metadata_on_drawdown(self):
        cfg = UserConfig(max_stake_pct=0.05)
        out = propose_suggestions(
            stats(n=50, roi=-0.10, win_rate=0.40, peak_dd=0.35),
            cfg, diag=EMPTY_DIAG,
        )
        p = self._only(out, "max_stake_pct")
        self.assertEqual(p.proposal_metadata["operation"], "scalar_set")
        self.assertEqual(p.proposal_metadata["field"], "max_stake_pct")
        self.assertAlmostEqual(p.proposal_metadata["value"], p.proposed_value)

    def test_scalar_cost_override_metadata(self):
        diag = dict(EMPTY_DIAG, cost_validation={
            "n": COST_CORRECTION_MIN_N + 10,
            "assumed_cost": 0.015,
            "implied_cost": 0.024,
        })
        p = self._only(
            propose_suggestions(neutral_stats(), UserConfig(), diag=diag),
            "cost_assumption_override",
        )
        self.assertEqual(p.proposal_metadata["operation"], "scalar_set")
        self.assertEqual(p.proposal_metadata["field"], "cost_assumption_override")
        self.assertAlmostEqual(p.proposal_metadata["value"], 0.024, places=4)


if __name__ == "__main__":
    unittest.main()
