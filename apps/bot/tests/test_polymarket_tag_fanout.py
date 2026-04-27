"""
Unit tests for tag-balanced candidate fetch in `feeds.polymarket_feed`.

Bug 2 context. Polymarket sorted by 24h volume is roughly 80% sports
because sports dominate volume. A naive top-by-volume scan therefore
crowds out politics, geopolitics, crypto, economy, etc., even though
those archetypes have shown better calibration. The fix is a tag-
balanced fan-out: query `/markets?tag_id=<n>` once per top-level tag,
take a per-tag quota of survivors, then dedupe by market.id and
re-apply the short-horizon-first global ranking.

These tests pin three properties:
  * `tag_id` is forwarded as a query parameter on every per-tag call
    (`tag_slug` is silently ignored by Gamma; the fix MUST use tag_id).
  * Quotas are honoured per tag - surplus from one tag does NOT spill
    into another tag's slot.
  * A market that holds multiple top-level tags is returned exactly
    once after dedupe, in the bucket of the first tag that picked it.

Synthetic fixtures only - no live Gamma calls.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feeds.polymarket_feed import PolymarketFeed


# ── Fixture builders ─────────────────────────────────────────────────────────
def _market_row(
    *,
    mid: str,
    yes_price: float = 0.55,
    volume_24h: float = 50_000.0,
    days_to_end: float = 3.0,
    accepting: bool = True,
    question: str | None = None,
) -> dict:
    """A synthetic Gamma `/markets` row with the fields the gates check."""
    end = datetime.now(timezone.utc) + timedelta(days=days_to_end)
    return {
        "id":              mid,
        "conditionId":     f"cond_{mid}",
        "question":        question or f"Synthetic question {mid}?",
        "description":     "synthetic",
        "outcomes":        '["Yes","No"]',
        "outcomePrices":   f'["{yes_price}","{1.0 - yes_price}"]',
        "volume24hrClob":  volume_24h,
        "liquidityNum":    1000.0,
        "endDateIso":      end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDate":         end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slug":            f"synth-{mid}",
        "acceptingOrders": accepting,
        "negRisk":         False,
        "negRiskOther":    False,
        "groupItemTitle":  "",
        "events":          [{"ticker": "synthetic", "slug": f"event-{mid}"}],
    }


class _FakeFeed(PolymarketFeed):
    """
    PolymarketFeed with `_get` swapped for a deterministic in-memory router.

    `tag_rows` is a dict of {tag_id: list[row]}. A `/markets` call with
    `params['tag_id']` returns the list for that tag (with offset / limit
    pagination respected). All other paths return None.
    """
    def __init__(self, tag_rows: dict[int, list[dict]] | None = None,
                 untagged_rows: list[dict] | None = None):
        super().__init__()
        self._tag_rows = tag_rows or {}
        self._untagged_rows = untagged_rows or []
        self.calls: list[dict] = []  # records each _get for assertions

    async def __aenter__(self):
        return self  # skip aiohttp session creation

    async def __aexit__(self, *_exc):
        return None

    async def _get(self, path: str, params: dict | None = None):  # type: ignore[override]
        params = params or {}
        self.calls.append({"path": path, "params": dict(params)})
        if path != "/markets":
            return None
        offset = int(params.get("offset", "0"))
        limit  = int(params.get("limit", "100"))
        if "tag_id" in params:
            tid = int(params["tag_id"])
            rows = self._tag_rows.get(tid, [])
        else:
            rows = self._untagged_rows
        return rows[offset:offset + limit]


def _run(coro):
    return asyncio.run(coro)


# ── tag_id query parameter ───────────────────────────────────────────────────
class TagIdQueryParamTests(unittest.TestCase):
    def test_fetch_candidates_by_tag_passes_tag_id_param(self):
        feed = _FakeFeed(tag_rows={42: [_market_row(mid="a")]})

        async def _go():
            async with feed:
                await feed.fetch_candidates_by_tag(tag_id=42, limit=5,
                                                   min_volume_24h=0.0)

        _run(_go())
        self.assertTrue(any(c["params"].get("tag_id") == "42" for c in feed.calls),
                        f"tag_id=42 was never sent. calls={feed.calls}")
        # And the value is a string-coerced int (Gamma rejects floats).
        for c in feed.calls:
            tid = c["params"].get("tag_id")
            if tid is not None:
                self.assertIsInstance(tid, str)
                int(tid)  # parses cleanly

    def test_fetch_candidates_balanced_calls_each_tag_once(self):
        feed = _FakeFeed(tag_rows={
            1: [_market_row(mid="s1")],
            2: [_market_row(mid="p1")],
        })
        async def _go():
            async with feed:
                await feed.fetch_candidates_balanced(
                    tag_quotas={1: 5, 2: 5}, min_volume_24h=0.0,
                )
        _run(_go())
        tag_ids_seen = sorted({c["params"].get("tag_id") for c in feed.calls
                                if c["params"].get("tag_id") is not None})
        self.assertEqual(tag_ids_seen, ["1", "2"])


# ── Quota enforcement ────────────────────────────────────────────────────────
class QuotaTests(unittest.TestCase):
    def test_per_tag_quota_caps_returned_markets(self):
        # Tag 1 has 50 candidates, but quota is 5 - we must keep only 5.
        feed = _FakeFeed(tag_rows={
            1: [_market_row(mid=f"s{i}", volume_24h=100_000 - i)
                for i in range(50)],
        })
        async def _go():
            async with feed:
                rows = await feed.fetch_candidates_by_tag(
                    tag_id=1, limit=5, min_volume_24h=0.0,
                )
                return rows
        rows = _run(_go())
        self.assertEqual(len(rows), 5)
        # Sorted by volume desc within short-horizon, so we keep the top 5.
        self.assertEqual([r.id for r in rows[:5]],
                         ["s0", "s1", "s2", "s3", "s4"])

    def test_surplus_from_one_tag_does_not_spill_into_another(self):
        # Tag 1 has 100 candidates with quota 5 (surplus = 95).
        # Tag 2 has 0 candidates with quota 30.
        # The fan-out MUST return only 5 markets, not redistribute the
        # 95-row surplus from sports into politics. That would silently
        # undo the bias correction.
        feed = _FakeFeed(tag_rows={
            1: [_market_row(mid=f"s{i}") for i in range(100)],
            2: [],
        })
        async def _go():
            async with feed:
                return await feed.fetch_candidates_balanced(
                    tag_quotas={1: 5, 2: 30}, min_volume_24h=0.0,
                )
        rows = _run(_go())
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertTrue(r.id.startswith("s"))


# ── Dedupe across tags ───────────────────────────────────────────────────────
class DedupeTests(unittest.TestCase):
    def test_market_in_multiple_tags_appears_only_once(self):
        # Same market id appears in tag 1 and tag 2. After dedupe it must
        # be counted once, in the bucket of the first tag (iteration
        # order of tag_quotas).
        shared = _market_row(mid="dup", volume_24h=99_999)
        feed = _FakeFeed(tag_rows={
            1: [shared, _market_row(mid="s1")],
            2: [shared, _market_row(mid="p1")],
        })
        async def _go():
            async with feed:
                return await feed.fetch_candidates_balanced(
                    tag_quotas={1: 5, 2: 5}, min_volume_24h=0.0,
                )
        rows = _run(_go())
        ids = [r.id for r in rows]
        self.assertEqual(ids.count("dup"), 1,
                         f"Duplicate market appeared {ids.count('dup')} times: {ids}")
        # And the union must contain s1 and p1.
        self.assertIn("s1", ids)
        self.assertIn("p1", ids)


# ── Gate suite still applied per tag ─────────────────────────────────────────
class GateSuiteTests(unittest.TestCase):
    def test_uncertainty_gate_drops_extreme_yes_prices(self):
        # Two markets in tag 1; one priced at 0.99 (locked YES, outside
        # uncertainty window 0.08-0.92). It must be dropped, leaving 1.
        feed = _FakeFeed(tag_rows={
            1: [
                _market_row(mid="locked",  yes_price=0.99),
                _market_row(mid="uncert", yes_price=0.55),
            ]
        })
        async def _go():
            async with feed:
                return await feed.fetch_candidates_by_tag(
                    tag_id=1, limit=10, min_volume_24h=0.0,
                )
        rows = _run(_go())
        ids = [r.id for r in rows]
        self.assertEqual(ids, ["uncert"])

    def test_volume_gate_drops_thin_markets(self):
        # Min volume 10k; one market at 5k (below) and one at 50k (above).
        feed = _FakeFeed(tag_rows={
            1: [
                _market_row(mid="thin",  volume_24h=5_000),
                _market_row(mid="thick", volume_24h=50_000),
            ]
        })
        async def _go():
            async with feed:
                return await feed.fetch_candidates_by_tag(
                    tag_id=1, limit=10, min_volume_24h=10_000,
                )
        rows = _run(_go())
        self.assertEqual([r.id for r in rows], ["thick"])

    def test_horizon_gate_drops_markets_outside_window(self):
        # Window 0-7 days; one market at 30 days (out), one at 3 days (in).
        feed = _FakeFeed(tag_rows={
            1: [
                _market_row(mid="far",  days_to_end=30),
                _market_row(mid="near", days_to_end=3),
            ]
        })
        async def _go():
            async with feed:
                return await feed.fetch_candidates_by_tag(
                    tag_id=1, limit=10, min_volume_24h=0.0,
                    min_days=0, max_days=7,
                )
        rows = _run(_go())
        self.assertEqual([r.id for r in rows], ["near"])

    def test_accepting_orders_gate_drops_settled_markets(self):
        feed = _FakeFeed(tag_rows={
            1: [
                _market_row(mid="dead",  accepting=False),
                _market_row(mid="alive", accepting=True),
            ]
        })
        async def _go():
            async with feed:
                return await feed.fetch_candidates_by_tag(
                    tag_id=1, limit=10, min_volume_24h=0.0,
                )
        rows = _run(_go())
        self.assertEqual([r.id for r in rows], ["alive"])


# ── Empty quota dict ─────────────────────────────────────────────────────────
class EmptyQuotaTests(unittest.TestCase):
    def test_empty_quota_dict_returns_empty_list(self):
        # An empty tag_quotas means "no fan-out configured" - the analyst
        # should fall back to the legacy untagged path itself, so the
        # balanced helper must just return [] without any HTTP calls.
        feed = _FakeFeed(tag_rows={1: [_market_row(mid="x")]})
        async def _go():
            async with feed:
                return await feed.fetch_candidates_balanced(
                    tag_quotas={}, min_volume_24h=0.0,
                )
        rows = _run(_go())
        self.assertEqual(rows, [])
        self.assertEqual(feed.calls, [])


# ── Short-horizon-first global ranking ───────────────────────────────────────
class ShortHorizonRankingTests(unittest.TestCase):
    def test_short_horizon_markets_come_before_long_horizon(self):
        # Tag 1 has a 30-day market at 99k volume.
        # Tag 2 has a 1-day market at 10k volume.
        # Volume alone would put tag 1 first, but the global re-rank
        # must surface the 1-day market first because short-horizon-
        # first beats raw volume rank.
        feed = _FakeFeed(tag_rows={
            1: [_market_row(mid="long",  days_to_end=30, volume_24h=99_000)],
            2: [_market_row(mid="short", days_to_end=1,  volume_24h=10_000)],
        })
        async def _go():
            async with feed:
                return await feed.fetch_candidates_balanced(
                    tag_quotas={1: 5, 2: 5}, min_volume_24h=0.0,
                    min_days=0, max_days=120,
                )
        rows = _run(_go())
        ids = [r.id for r in rows]
        self.assertEqual(ids, ["short", "long"])


if __name__ == "__main__":
    unittest.main()
