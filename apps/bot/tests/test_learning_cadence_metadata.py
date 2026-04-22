"""
Round-trip tests for `proposal_metadata` on the pending_suggestions row.

The metadata column carries list-append payloads (target_field + items) so
`apply_suggestion` can dispatch correctly across process restarts. These
tests exercise the serialization boundary only — the actual INSERT /
SELECT path needs a live PostgreSQL and is covered by the end-to-end
verification step.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.learning_cadence import Proposal, _decode_metadata


class DecodeMetadataTests(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_decode_metadata(None))

    def test_dict_passthrough(self):
        d = {"operation": "list_append", "target_field": "x", "items": ["a"]}
        self.assertIs(_decode_metadata(d), d)

    def test_json_string_is_parsed(self):
        payload = {"operation": "scalar_set", "field": "max_stake_pct",
                   "value": 0.04}
        raw = json.dumps(payload)
        self.assertEqual(_decode_metadata(raw), payload)

    def test_json_bytes_is_parsed(self):
        payload = {"operation": "list_append", "target_field": "archetype_skip_list",
                   "items": ["politics"]}
        raw = json.dumps(payload).encode("utf-8")
        self.assertEqual(_decode_metadata(raw), payload)

    def test_garbage_string_returns_none(self):
        self.assertIsNone(_decode_metadata("not json"))

    def test_non_object_json_returns_none(self):
        # Arrays/strings/numbers at the top level aren't valid proposal shapes.
        self.assertIsNone(_decode_metadata("[1, 2, 3]"))
        self.assertIsNone(_decode_metadata("42"))


class ProposalMetadataShapeTests(unittest.TestCase):
    """The `proposal_metadata` field must survive a JSON round-trip intact."""

    def test_list_append_shape_roundtrips(self):
        prop = Proposal(
            param_name="archetype_skip_list",
            current_value=None, proposed_value=None,
            evidence="irrelevant",
            proposal_metadata={
                "operation":    "list_append",
                "target_field": "archetype_skip_list",
                "items":        ["politics", "weather"],
            },
        )
        roundtrip = _decode_metadata(json.dumps(prop.proposal_metadata))
        self.assertEqual(roundtrip, prop.proposal_metadata)

    def test_scalar_set_shape_roundtrips(self):
        prop = Proposal(
            param_name="max_stake_pct",
            current_value=0.05, proposed_value=0.04,
            evidence="irrelevant",
            proposal_metadata={
                "operation": "scalar_set",
                "field":     "max_stake_pct",
                "value":     0.04,
            },
        )
        roundtrip = _decode_metadata(json.dumps(prop.proposal_metadata))
        self.assertEqual(roundtrip, prop.proposal_metadata)


class ApplyListAppendDispatchTests(unittest.TestCase):
    """`_apply_list_append` must merge items into the existing tuple without
    duplicates, preserving order. It reads via `get_user_config` and writes
    via `update_user_config` — both monkeypatched here to isolate from the DB."""

    def setUp(self):
        from engine import learning_cadence as lc
        from engine.user_config import UserConfig
        self.lc = lc
        self.UserConfig = UserConfig
        # Capture every write so tests can inspect the payload update_user_config saw.
        self.writes: list[dict] = []
        self._saved_get = lc.get_user_config
        self._saved_upd = lc.update_user_config

    def tearDown(self):
        self.lc.get_user_config = self._saved_get
        self.lc.update_user_config = self._saved_upd

    def _install_stubs(self, current_config):
        def fake_get(user_id):
            return current_config
        def fake_update(user_id, **changes):
            self.writes.append(dict(changes))
            return current_config
        self.lc.get_user_config = fake_get
        self.lc.update_user_config = fake_update

    def test_empty_list_gets_item(self):
        self._install_stubs(self.UserConfig(archetype_skip_list=()))
        result = self.lc._apply_list_append("default", {
            "operation":    "list_append",
            "target_field": "archetype_skip_list",
            "items":        ["politics"],
        })
        self.assertEqual(self.writes, [{"archetype_skip_list": ("politics",)}])
        self.assertEqual(result["added"], ["politics"])
        self.assertEqual(result["skipped_dups"], [])

    def test_non_empty_list_appends_preserving_order(self):
        self._install_stubs(self.UserConfig(
            archetype_skip_list=("weather", "other"),
        ))
        result = self.lc._apply_list_append("default", {
            "operation":    "list_append",
            "target_field": "archetype_skip_list",
            "items":        ["crypto"],
        })
        self.assertEqual(self.writes,
                         [{"archetype_skip_list": ("weather", "other", "crypto")}])
        self.assertEqual(result["added"], ["crypto"])

    def test_duplicate_is_noop_append(self):
        self._install_stubs(self.UserConfig(
            archetype_skip_list=("politics", "weather"),
        ))
        result = self.lc._apply_list_append("default", {
            "operation":    "list_append",
            "target_field": "archetype_skip_list",
            "items":        ["politics"],
        })
        # update_user_config is still called so CSV serialisation stays
        # consistent, but the resulting tuple is unchanged.
        self.assertEqual(self.writes,
                         [{"archetype_skip_list": ("politics", "weather")}])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["skipped_dups"], ["politics"])

    def test_missing_target_field_raises(self):
        self._install_stubs(self.UserConfig())
        with self.assertRaises(ValueError):
            self.lc._apply_list_append("default", {
                "operation": "list_append",
                "items":     ["anything"],
            })

    def test_missing_items_raises(self):
        self._install_stubs(self.UserConfig())
        with self.assertRaises(ValueError):
            self.lc._apply_list_append("default", {
                "operation":    "list_append",
                "target_field": "archetype_skip_list",
            })

    def test_empty_items_list_raises(self):
        self._install_stubs(self.UserConfig())
        with self.assertRaises(ValueError):
            self.lc._apply_list_append("default", {
                "operation":    "list_append",
                "target_field": "archetype_skip_list",
                "items":        [],
            })


class ApplyScalarDispatchTests(unittest.TestCase):
    """`_apply_scalar` must continue to route scalar_set proposals through
    `update_user_config(field=value)` unchanged."""

    def setUp(self):
        from engine import learning_cadence as lc
        from engine.user_config import UserConfig
        self.lc = lc
        self.UserConfig = UserConfig
        self.writes: list[dict] = []
        self._saved_upd = lc.update_user_config
        lc.update_user_config = lambda uid, **kw: self.writes.append(dict(kw))

    def tearDown(self):
        self.lc.update_user_config = self._saved_upd

    def test_scalar_write(self):
        result = self.lc._apply_scalar("default", "max_stake_pct", 0.04)
        self.assertEqual(self.writes, [{"max_stake_pct": 0.04}])
        self.assertEqual(result["operation"], "scalar_set")
        self.assertAlmostEqual(result["value"], 0.04, places=6)

    def test_scalar_with_none_value_raises(self):
        with self.assertRaises(ValueError):
            self.lc._apply_scalar("default", "max_stake_pct", None)


if __name__ == "__main__":
    unittest.main()
