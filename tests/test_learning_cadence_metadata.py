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
        payload = {"operation": "scalar_set", "field": "min_ev_threshold",
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
            param_name="min_ev_threshold",
            current_value=0.03, proposed_value=0.04,
            evidence="irrelevant",
            proposal_metadata={
                "operation": "scalar_set",
                "field":     "min_ev_threshold",
                "value":     0.04,
            },
        )
        roundtrip = _decode_metadata(json.dumps(prop.proposal_metadata))
        self.assertEqual(roundtrip, prop.proposal_metadata)


if __name__ == "__main__":
    unittest.main()
