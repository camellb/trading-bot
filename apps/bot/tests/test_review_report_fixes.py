"""
Unit tests pinning the post-bug fixes to the 50-trade review pipeline.

The user reported four operational issues with the review:

  1. They received two reports back to back. The cause was the bookmark
     bug in `engine.learning_cadence._last_cycle_settled_count`: when a
     cycle ran but produced zero proposals, no row landed in
     `pending_suggestions`, so `MAX(settled_count_at_creation)` did not
     advance and the very next settle re-fired the cycle. Fix: read the
     bookmark from `learning_reports` (always written) and `MAX` it
     with the legacy pending_suggestions source.

  2. The Telegram message was cut off mid-word. Telegram has a 4096
     char hard limit and the report easily exceeds it once you include
     the lifetime block, archetype table, calibration bins, top wins,
     top losses, and proposals. Fix: chunk on line boundaries and send
     each chunk as its own `<pre>` message.

  3. `/apply` only applied one suggestion at a time. Fix: new
     `apply_all_pending_suggestions` walks every pending row, returning
     applied / failed lists for the Telegram handler to render.

  4. ROI in the review didn't match ROI on the dashboard. They are
     computed differently (capital-staked vs starting-cash) but the
     review now surfaces both, clearly labelled, so they reconcile.

These tests cover:
  - `_chunk_for_telegram` line-boundary chunking, single-line edge cases.
  - `apply_all_pending_suggestions` happy path, partial failure, empty
    queue. Uses a stub `apply_suggestion` to avoid DB dependency.
  - The `lifetime` block is initialised in the data scaffold and
    survives the empty-cycle path.
  - Bookmark MAX-of-both via `_last_cycle_settled_count` with
    monkeypatched DB calls.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import learning_cadence as lc
from engine import review_report as rr


# ── Telegram chunker ─────────────────────────────────────────────────────────
class ChunkerTests(unittest.TestCase):
    def test_short_body_returns_single_chunk(self):
        body = "line one\nline two\nline three"
        chunks = lc._chunk_for_telegram(body)
        self.assertEqual(chunks, [body])

    def test_long_body_splits_into_multiple_chunks_under_budget(self):
        # 500 lines of 60 chars each ≈ 30k chars, well over the budget.
        line = "x" * 60
        body = "\n".join(line for _ in range(500))
        chunks = lc._chunk_for_telegram(body)
        self.assertGreater(len(chunks), 1, "expected multiple chunks")
        for c in chunks:
            self.assertLessEqual(len(c), lc._TELEGRAM_CHUNK_BUDGET,
                                 f"chunk over budget: {len(c)}")

    def test_chunks_split_on_newline_not_mid_word(self):
        # Each line is a complete "wordN" token. After chunking, no
        # chunk should start or end mid-token.
        body = "\n".join(f"word{i}" for i in range(2000))
        chunks = lc._chunk_for_telegram(body)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertTrue(c.startswith("word"),
                            f"chunk started mid-word: {c[:30]!r}")
            last_line = c.split("\n")[-1]
            self.assertTrue(last_line.startswith("word"),
                            f"chunk ended mid-word: {last_line!r}")

    def test_oversized_single_line_hard_splits_on_chars(self):
        # Pathological 10000-char single line. Hard-split path keeps the
        # chunker alive without producing a crash.
        body = "y" * 10_000
        chunks = lc._chunk_for_telegram(body)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), lc._TELEGRAM_CHUNK_BUDGET)
        # Round-trip identity for the hard-split path.
        self.assertEqual("".join(chunks), body)


# ── apply_all_pending_suggestions ────────────────────────────────────────────
class ApplyAllTests(unittest.TestCase):
    def test_empty_queue_returns_status_none(self):
        with patch.object(lc, "list_pending_suggestions", return_value=[]):
            result = lc.apply_all_pending_suggestions(user_id="u1")
        self.assertEqual(result["status"], "none")
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["failed"],  [])
        self.assertEqual(result["total"],   0)

    def test_happy_path_applies_every_pending_in_order(self):
        rows = [
            {"id": 1, "status": "pending",
             "created_at": "2026-04-26T10:00:00Z",
             "param_name": "max_stake_pct", "current_value": 0.05},
            {"id": 2, "status": "pending",
             "created_at": "2026-04-26T10:01:00Z",
             "param_name": "min_p_win",     "current_value": 0.55},
        ]

        applied_ids: list[int] = []

        def fake_apply(suggestion_id, *, user_id, resolved_by):
            applied_ids.append(suggestion_id)
            return {
                "status":      "applied",
                "param_name":  ("max_stake_pct"
                                if suggestion_id == 1 else "min_p_win"),
                "value":       0.035 if suggestion_id == 1 else 0.60,
                "operation":   "scalar_set",
            }

        with patch.object(lc, "list_pending_suggestions", return_value=rows), \
             patch.object(lc, "apply_suggestion", side_effect=fake_apply):
            result = lc.apply_all_pending_suggestions(user_id="u1")

        self.assertEqual(result["status"], "applied")
        self.assertEqual(applied_ids, [1, 2], "must apply in created_at order")
        self.assertEqual(len(result["applied"]), 2)
        self.assertEqual(result["failed"], [])
        for row in result["applied"]:
            self.assertIn("display_key",      row)
            self.assertIn("display_previous", row)
            self.assertIn("display_value",    row)

    def test_partial_failure_keeps_loop_going(self):
        rows = [
            {"id": 1, "status": "pending",
             "created_at": "2026-04-26T10:00:00Z",
             "param_name": "max_stake_pct", "current_value": 0.05},
            {"id": 2, "status": "pending",
             "created_at": "2026-04-26T10:01:00Z",
             "param_name": "min_p_win",     "current_value": 0.55},
            {"id": 3, "status": "pending",
             "created_at": "2026-04-26T10:02:00Z",
             "param_name": "min_p_win",     "current_value": 0.55},
        ]

        def fake_apply(suggestion_id, *, user_id, resolved_by):
            if suggestion_id == 2:
                raise RuntimeError("simulated DB failure")
            return {
                "status":      "applied",
                "param_name":  "x",
                "value":       1.0,
                "operation":   "scalar_set",
            }

        with patch.object(lc, "list_pending_suggestions", return_value=rows), \
             patch.object(lc, "apply_suggestion", side_effect=fake_apply):
            result = lc.apply_all_pending_suggestions(user_id="u1")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["applied"]), 2)
        self.assertEqual(len(result["failed"]),  1)
        self.assertEqual(result["failed"][0]["id"], 2)

    def test_dict_set_op_gets_correct_display_key(self):
        # An archetype_stake_multiplier proposal returns operation=
        # "dict_set" with a key. The display_key formatting must produce
        # "archetype_stake_multipliers['tennis']" so the user sees
        # which archetype was retuned.
        rows = [{
            "id": 7, "status": "pending",
            "created_at": "2026-04-26T10:00:00Z",
            "param_name": "archetype_stake_multipliers",
            "current_value": 1.0,
        }]

        def fake_apply(suggestion_id, *, user_id, resolved_by):
            return {
                "status":      "applied",
                "operation":   "dict_set",
                "param_name":  "archetype_stake_multipliers",
                "key":         "tennis",
                "value":       0.5,
            }

        with patch.object(lc, "list_pending_suggestions", return_value=rows), \
             patch.object(lc, "apply_suggestion", side_effect=fake_apply):
            result = lc.apply_all_pending_suggestions(user_id="u1")

        self.assertEqual(result["status"], "applied")
        only = result["applied"][0]
        self.assertEqual(only["display_key"],
                         "archetype_stake_multipliers['tennis']")
        self.assertEqual(only["display_previous"], 1.0)
        self.assertEqual(only["display_value"],    0.5)


# ── Lifetime block in gather_cycle_data ──────────────────────────────────────
class LifetimeBlockTests(unittest.TestCase):
    def test_no_settled_rows_still_returns_lifetime_scaffold(self):
        # Empty cycle path must still surface the lifetime block.
        with patch.object(rr, "_fetch_settled_rows", return_value=[]), \
             patch.object(rr, "_fetch_lifetime_stats",
                          return_value={
                              "settled_total":  120,
                              "wins":           65,
                              "win_rate":       65 / 120,
                              "realized_pnl":   42.50,
                              "starting_cash":  1000.0,
                              "equity":         1042.50,
                              "roi":            0.0425,
                          }):
            data = rr.gather_cycle_data(user_id="u1", mode="simulation",
                                        cycle_size=50)
        self.assertEqual(data["settled_count"], 0)
        self.assertEqual(data["headline"]["n"], 0)
        lifetime = data.get("lifetime") or {}
        self.assertEqual(lifetime["settled_total"], 120)
        self.assertEqual(lifetime["wins"],          65)
        self.assertAlmostEqual(lifetime["roi"],     0.0425, places=4)

    def test_render_data_tables_includes_both_lifetime_and_cycle(self):
        # Both labelled blocks must render so dashboard ROI vs cycle-
        # window ROI reconcile at a glance.
        data = {
            "headline": {
                "n":        50,
                "pnl_usd":  10.0,
                "cost_usd": 200.0,
                "roi":      0.05,        # 5% on capital staked
                "win_rate": 0.52,
                "brier":    0.21,
            },
            "lifetime": {
                "settled_total":  120,
                "wins":           65,
                "win_rate":       65 / 120,
                "realized_pnl":   42.50,
                "starting_cash":  1000.0,
                "equity":         1042.50,
                "roi":            0.0425,  # 4.25% on starting cash
            },
            "verdict": "profitable",
        }
        out = rr.render_data_tables(data)
        self.assertIn("LIFETIME (matches dashboard)", out)
        self.assertIn("ROI (lifetime):", out)
        self.assertIn("THIS CYCLE (last 50 settled trades)", out)
        self.assertIn("ROI on staked:", out)
        self.assertIn("5.0%", out)
        self.assertIn("4.2%", out)


# ── Bookmark MAX-of-both ─────────────────────────────────────────────────────
class BookmarkTests(unittest.TestCase):
    """`_last_cycle_settled_count` is a thin DB helper. Rather than
    integration-test against Postgres, we patch `get_engine` to verify
    the SQL it issues and the MAX semantics."""

    def test_takes_max_of_reports_and_pending(self):
        from contextlib import contextmanager

        scalars_in_order = [150, 100]   # learning_reports, then pending
        observed_sql: list[str] = []

        class _FakeResult:
            def __init__(self, val): self._v = val
            def scalar(self): return self._v

        class _FakeConn:
            def execute(self, stmt, params=None):
                observed_sql.append(str(stmt))
                return _FakeResult(scalars_in_order.pop(0))

        @contextmanager
        def _begin():
            yield _FakeConn()

        class _FakeEngine:
            def begin(self): return _begin()

        with patch("db.engine.get_engine", return_value=_FakeEngine()):
            val = lc._last_cycle_settled_count(user_id="u1", mode="simulation")

        self.assertEqual(val, 150, "must return MAX of the two")
        self.assertEqual(len(observed_sql), 2,
                         "must hit both learning_reports AND "
                         "pending_suggestions")
        joined = " ".join(observed_sql).lower()
        self.assertIn("learning_reports", joined)
        self.assertIn("pending_suggestions", joined)

    def test_returns_zero_when_both_sources_empty(self):
        from contextlib import contextmanager

        class _FakeResult:
            def scalar(self): return None

        class _FakeConn:
            def execute(self, *a, **kw): return _FakeResult()

        @contextmanager
        def _begin():
            yield _FakeConn()

        class _FakeEngine:
            def begin(self): return _begin()

        with patch("db.engine.get_engine", return_value=_FakeEngine()):
            val = lc._last_cycle_settled_count(user_id="u1", mode="simulation")
        self.assertEqual(val, 0)


if __name__ == "__main__":
    unittest.main()
