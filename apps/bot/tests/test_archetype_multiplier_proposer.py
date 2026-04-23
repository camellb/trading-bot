"""
Tests for the archetype-stake-multiplier proposer and the dict_set apply path.

The proposer consumes `archetype_pnl_attribution()` (list of
{archetype, n, roi, ...}) and emits a Proposal per archetype whose ROI sits
in one of the discrete tiers, subject to:
  - n >= ARCHETYPE_MULTIPLIER_MIN_N (25)
  - archetype not on archetype_skip_list
  - |proposed - currently_applied| >= ARCHETYPE_MULTIPLIER_HYSTERESIS
The metadata carries an "operation": "dict_set" payload that
`_apply_dict_set` merges into `archetype_stake_multipliers` via
`update_user_config`.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import learning_cadence as lc
from engine.learning_cadence import (
    ARCHETYPE_MULTIPLIER_HYSTERESIS,
    ARCHETYPE_MULTIPLIER_MIN_N,
    Proposal,
    _build_modified_config,
    _decode_metadata,
    _pick_multiplier_tier,
    _propose_archetype_stake_multiplier,
)
from engine.user_config import UserConfig


def _diag(rows):
    return {"archetype_pnl": rows}


# ── Tier selection ───────────────────────────────────────────────────────────
class PickTierTests(unittest.TestCase):
    def test_deep_loss_picks_half(self):
        self.assertEqual(_pick_multiplier_tier(-0.25), 0.5)

    def test_mild_loss_picks_three_quarters(self):
        self.assertEqual(_pick_multiplier_tier(-0.05), 0.75)

    def test_neutral_band_is_none(self):
        self.assertIsNone(_pick_multiplier_tier(0.0))
        self.assertIsNone(_pick_multiplier_tier(0.03))
        self.assertIsNone(_pick_multiplier_tier(0.04))

    def test_mild_profit_picks_one_and_a_quarter(self):
        self.assertEqual(_pick_multiplier_tier(0.10), 1.25)

    def test_strong_profit_picks_one_and_a_half(self):
        self.assertEqual(_pick_multiplier_tier(0.25), 1.5)

    def test_neutral_upper_boundary_inclusive_of_profit_tier(self):
        self.assertEqual(_pick_multiplier_tier(0.05), 1.25)


# ── Proposer gates ───────────────────────────────────────────────────────────
class ProposerGateTests(unittest.TestCase):
    def test_small_sample_emits_nothing(self):
        # n below the 25-trade gate → no proposal even with strong ROI.
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "sports", "n": ARCHETYPE_MULTIPLIER_MIN_N - 1,
                    "roi": 0.30}]),
            UserConfig(),
        )
        self.assertEqual(out, [])

    def test_neutral_band_emits_nothing(self):
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "markets", "n": 80, "roi": 0.02}]),
            UserConfig(),
        )
        self.assertEqual(out, [])

    def test_missing_archetype_key_skipped(self):
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "", "n": 100, "roi": -0.20}]),
            UserConfig(),
        )
        self.assertEqual(out, [])

    def test_missing_roi_skipped(self):
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "sports", "n": 100, "roi": None}]),
            UserConfig(),
        )
        self.assertEqual(out, [])

    def test_archetype_on_skip_list_skipped(self):
        cfg = UserConfig(archetype_skip_list=("tennis",))
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "tennis", "n": 60, "roi": 0.20}]),
            cfg,
        )
        self.assertEqual(out, [])


# ── Proposer tier emission ───────────────────────────────────────────────────
class ProposerTierEmissionTests(unittest.TestCase):
    def _roundtrip_meta(self, prop: Proposal) -> dict:
        return _decode_metadata(json.dumps(prop.proposal_metadata))

    def test_deep_loss_emits_half(self):
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "weather", "n": 40, "roi": -0.25}]),
            UserConfig(),
        )
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p.param_name, "archetype_stake_multipliers")
        self.assertAlmostEqual(p.proposed_value, 0.5, places=6)
        self.assertAlmostEqual(p.current_value, 1.0, places=6)
        meta = self._roundtrip_meta(p)
        self.assertEqual(meta["operation"], "dict_set")
        self.assertEqual(meta["target_field"], "archetype_stake_multipliers")
        self.assertEqual(meta["key"], "weather")
        self.assertAlmostEqual(meta["value"], 0.5, places=6)

    def test_strong_profit_emits_one_and_a_half(self):
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "politics", "n": 50, "roi": 0.30}]),
            UserConfig(),
        )
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].proposed_value, 1.5, places=6)

    def test_current_value_reflects_existing_multiplier(self):
        cfg = UserConfig(archetype_stake_multipliers={"crypto": 1.25})
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "crypto", "n": 60, "roi": 0.30}]),
            cfg,
        )
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].current_value, 1.25, places=6)
        self.assertAlmostEqual(out[0].proposed_value, 1.5, places=6)

    def test_multiple_archetypes_produce_multiple_proposals(self):
        out = _propose_archetype_stake_multiplier(
            _diag([
                {"archetype": "a", "n": 30, "roi": -0.20},
                {"archetype": "b", "n": 30, "roi":  0.20},
                {"archetype": "c", "n": 10, "roi":  0.50},  # n-gated
                {"archetype": "d", "n": 30, "roi":  0.02},  # neutral band
            ]),
            UserConfig(),
        )
        got = {p.proposal_metadata["key"]: p.proposed_value for p in out}
        self.assertEqual(set(got.keys()), {"a", "b"})
        self.assertAlmostEqual(got["a"], 0.5,  places=6)
        self.assertAlmostEqual(got["b"], 1.5,  places=6)


# ── Hysteresis ───────────────────────────────────────────────────────────────
class HysteresisTests(unittest.TestCase):
    def test_small_drift_within_band_is_noop(self):
        # currently 0.5, tier says 0.5 → diff 0.0 < 0.1 hysteresis → skip.
        cfg = UserConfig(archetype_stake_multipliers={"sports": 0.5})
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "sports", "n": 100, "roi": -0.20}]),
            cfg,
        )
        self.assertEqual(out, [])

    def test_tier_crossing_emits_new_proposal(self):
        # currently 0.5, new tier 1.5 → diff 1.0 >> 0.1 → emit.
        cfg = UserConfig(archetype_stake_multipliers={"sports": 0.5})
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "sports", "n": 100, "roi": 0.30}]),
            cfg,
        )
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].proposed_value, 1.5, places=6)

    def test_hysteresis_threshold_is_strict(self):
        # A gap exactly equal to the hysteresis value MUST emit. Using a
        # custom currently-applied value so the tier sits exactly +0.1 away.
        cfg = UserConfig(
            archetype_stake_multipliers={
                "sports": 1.5 - ARCHETYPE_MULTIPLIER_HYSTERESIS,
            }
        )
        out = _propose_archetype_stake_multiplier(
            _diag([{"archetype": "sports", "n": 100, "roi": 0.30}]),
            cfg,
        )
        # diff = 0.1 is NOT < 0.1, so the proposer emits.
        self.assertEqual(len(out), 1)


# ── _build_modified_config for dict_set ──────────────────────────────────────
class BuildModifiedConfigDictSetTests(unittest.TestCase):
    def test_merges_into_empty_map(self):
        current = UserConfig()
        prop = Proposal(
            param_name="archetype_stake_multipliers",
            current_value=1.0, proposed_value=1.5,
            evidence="irrelevant",
            proposal_metadata={
                "operation":    "dict_set",
                "target_field": "archetype_stake_multipliers",
                "key":          "politics",
                "value":        1.5,
            },
        )
        modified = _build_modified_config(prop, current)
        self.assertIsNotNone(modified)
        self.assertEqual(modified.archetype_stake_multipliers, {"politics": 1.5})

    def test_preserves_existing_keys(self):
        current = UserConfig(
            archetype_stake_multipliers={"sports": 0.75, "markets": 1.25},
        )
        prop = Proposal(
            param_name="archetype_stake_multipliers",
            current_value=1.0, proposed_value=0.5,
            evidence="irrelevant",
            proposal_metadata={
                "operation":    "dict_set",
                "target_field": "archetype_stake_multipliers",
                "key":          "weather",
                "value":        0.5,
            },
        )
        modified = _build_modified_config(prop, current)
        self.assertEqual(
            modified.archetype_stake_multipliers,
            {"sports": 0.75, "markets": 1.25, "weather": 0.5},
        )

    def test_overwrites_existing_key(self):
        current = UserConfig(
            archetype_stake_multipliers={"sports": 0.75},
        )
        prop = Proposal(
            param_name="archetype_stake_multipliers",
            current_value=0.75, proposed_value=1.5,
            evidence="irrelevant",
            proposal_metadata={
                "operation":    "dict_set",
                "target_field": "archetype_stake_multipliers",
                "key":          "sports",
                "value":        1.5,
            },
        )
        modified = _build_modified_config(prop, current)
        self.assertEqual(modified.archetype_stake_multipliers, {"sports": 1.5})

    def test_missing_key_returns_none(self):
        current = UserConfig()
        prop = Proposal(
            param_name="archetype_stake_multipliers",
            current_value=None, proposed_value=1.5,
            evidence="irrelevant",
            proposal_metadata={
                "operation":    "dict_set",
                "target_field": "archetype_stake_multipliers",
                "value":        1.5,
            },
        )
        self.assertIsNone(_build_modified_config(prop, current))

    def test_missing_value_falls_back_to_proposed_value(self):
        current = UserConfig()
        prop = Proposal(
            param_name="archetype_stake_multipliers",
            current_value=None, proposed_value=1.25,
            evidence="irrelevant",
            proposal_metadata={
                "operation":    "dict_set",
                "target_field": "archetype_stake_multipliers",
                "key":          "markets",
            },
        )
        modified = _build_modified_config(prop, current)
        self.assertIsNotNone(modified)
        self.assertAlmostEqual(
            modified.archetype_stake_multipliers["markets"], 1.25, places=6,
        )


# ── _apply_dict_set dispatch ─────────────────────────────────────────────────
class ApplyDictSetDispatchTests(unittest.TestCase):
    """`_apply_dict_set` reads via `get_user_config` and writes via
    `update_user_config`. Both are monkeypatched here so the test is DB-free."""

    def setUp(self):
        self.writes: list[dict] = []
        self._saved_get = lc.get_user_config
        self._saved_upd = lc.update_user_config

    def tearDown(self):
        lc.get_user_config = self._saved_get
        lc.update_user_config = self._saved_upd

    def _install(self, current_config):
        def fake_get(user_id):
            return current_config
        def fake_update(user_id, **changes):
            self.writes.append(dict(changes))
            return current_config
        lc.get_user_config = fake_get
        lc.update_user_config = fake_update

    def test_merges_into_empty_map(self):
        self._install(UserConfig())
        result = lc._apply_dict_set("default", {
            "operation":    "dict_set",
            "target_field": "archetype_stake_multipliers",
            "key":          "politics",
            "value":        1.5,
        })
        self.assertEqual(
            self.writes,
            [{"archetype_stake_multipliers": {"politics": 1.5}}],
        )
        self.assertEqual(result["operation"], "dict_set")
        self.assertEqual(result["key"], "politics")
        self.assertAlmostEqual(result["value"], 1.5, places=6)

    def test_preserves_existing_keys_on_update(self):
        self._install(UserConfig(
            archetype_stake_multipliers={"sports": 0.75, "markets": 1.25},
        ))
        lc._apply_dict_set("default", {
            "operation":    "dict_set",
            "target_field": "archetype_stake_multipliers",
            "key":          "weather",
            "value":        0.5,
        })
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(
            self.writes[0]["archetype_stake_multipliers"],
            {"sports": 0.75, "markets": 1.25, "weather": 0.5},
        )

    def test_overwrites_existing_key(self):
        self._install(UserConfig(
            archetype_stake_multipliers={"sports": 0.75},
        ))
        lc._apply_dict_set("default", {
            "operation":    "dict_set",
            "target_field": "archetype_stake_multipliers",
            "key":          "sports",
            "value":        1.5,
        })
        self.assertEqual(
            self.writes[0]["archetype_stake_multipliers"],
            {"sports": 1.5},
        )

    def test_missing_target_raises(self):
        self._install(UserConfig())
        with self.assertRaises(ValueError):
            lc._apply_dict_set("default", {
                "operation": "dict_set",
                "key":       "sports",
                "value":     1.0,
            })

    def test_missing_key_raises(self):
        self._install(UserConfig())
        with self.assertRaises(ValueError):
            lc._apply_dict_set("default", {
                "operation":    "dict_set",
                "target_field": "archetype_stake_multipliers",
                "value":        1.0,
            })

    def test_missing_value_raises(self):
        self._install(UserConfig())
        with self.assertRaises(ValueError):
            lc._apply_dict_set("default", {
                "operation":    "dict_set",
                "target_field": "archetype_stake_multipliers",
                "key":          "sports",
            })


if __name__ == "__main__":
    unittest.main()
