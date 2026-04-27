"""
Regression test for the closed-position count divergence.

The user reported a mismatch where the dashboard Snapshot showed 249
trades (156W + 93L) but the Positions page chip said `Closed (50)`.

Root cause: `/api/positions` returns the settled rows under a `LIMIT`
(default 50). The Positions page rendered the chip count from
`array.length`, which structurally maxed out at the request limit.
The Snapshot panel calls `/api/positions?limit=500`, so its number
grows to the real total; the two surfaces visibly disagreed.

Fix: `/api/positions` now also issues an unbounded `COUNT(*)` for
the same user+mode+status filter and returns it as `settled_total`.
The Positions page reads that field for the chip and the panel meta.

This test pins the new behaviour:
  * the response includes `settled_total`,
  * the count comes from a separate `COUNT(*)` query, NOT from
    `len(settled_rows)`,
  * the count is independent of the request `limit`.
"""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_api as bot_api_mod


class _FakeResult:
    """Mimics SQLAlchemy CursorResult for the two methods we use."""
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar
    def fetchall(self):
        return self._rows
    def scalar(self):
        return self._scalar


class _FakeConn:
    """Records every SQL string so the test can assert what was issued."""
    def __init__(self, scripted):
        # `scripted` is a list of (rows, scalar) tuples; one per execute call.
        self._scripted = list(scripted)
        self.statements: list[str] = []
        self.params: list[dict] = []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        self.params.append(params or {})
        rows, scalar = self._scripted.pop(0)
        return _FakeResult(rows=rows, scalar=scalar)


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn
    def begin(self):
        @contextmanager
        def _ctx():
            yield self._conn
        return _ctx()


class _StubExecutor:
    """Just enough surface for `_handle_positions`."""
    def __init__(self, user_id="u1", mode="simulation"):
        self.user_id = user_id
        self.mode    = mode
    def get_open_positions(self):
        return []  # not under test here


def _make_api():
    """Build a BotAPI with all the heavy bits stubbed out."""
    api = bot_api_mod.BotAPI.__new__(bot_api_mod.BotAPI)
    # Inline init: avoid touching Anthropic / ThreadPool / env reads.
    from concurrent.futures import ThreadPoolExecutor
    api._analyst       = None
    api._notifier      = None
    api._scheduler     = None
    api._secret        = "test"
    api._runner        = None
    api._started_at    = None
    api._pending_config = None
    api._disk_mode      = None
    api._pool           = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apitest")
    api._claude         = None
    return api


class PositionsCountTests(unittest.IsolatedAsyncioTestCase):

    async def test_response_includes_settled_total_from_count_query(self):
        # Scripted: first execute (SELECT rows) returns an empty list of
        # rows; second execute (COUNT(*)) returns 249.
        conn = _FakeConn(scripted=[([], None), ([], 249)])
        engine = _FakeEngine(conn)
        api = _make_api()

        request = make_mocked_request(
            "GET", "/api/positions",
            headers={"X-User-Id": "u1", "X-Bot-Secret": "test"},
        )

        with patch.object(api, "_user_executor", return_value=_StubExecutor()), \
             patch.object(bot_api_mod, "get_engine", return_value=engine):
            response = await api._handle_positions(request)

        import json as _json
        body = _json.loads(response.body)

        # The new contract: response carries `settled_total`.
        self.assertIn("settled_total", body)
        self.assertEqual(body["settled_total"], 249,
                         "must come from COUNT(*) not from len(settled)")
        # Sanity: the rendered list is empty (LIMIT'd to 0 rows in the
        # mock) but the count is still 249. This is the exact regression
        # the user reported: array.length != true count.
        self.assertEqual(body["settled"], [])
        self.assertNotEqual(body["settled_total"], len(body["settled"]))

    async def test_count_query_uses_same_predicate_as_select(self):
        conn = _FakeConn(scripted=[([], None), ([], 7)])
        engine = _FakeEngine(conn)
        api = _make_api()

        request = make_mocked_request(
            "GET", "/api/positions?view_mode=simulation",
            headers={"X-User-Id": "u1", "X-Bot-Secret": "test"},
        )

        with patch.object(api, "_user_executor",
                          return_value=_StubExecutor(mode="simulation")), \
             patch.object(bot_api_mod, "get_engine", return_value=engine):
            await api._handle_positions(request)

        # We expect EXACTLY two queries: the LIMIT'd SELECT, then the
        # unbounded COUNT.
        self.assertEqual(len(conn.statements), 2,
                         "must issue both the SELECT and a separate COUNT")
        select_sql, count_sql = conn.statements[0].lower(), conn.statements[1].lower()
        # Both must hit pm_positions with the user/mode/status filter.
        for sql in (select_sql, count_sql):
            self.assertIn("pm_positions", sql)
            self.assertIn("user_id",      sql)
            self.assertIn("mode",         sql)
            self.assertIn("status in ('settled', 'invalid')", sql)
        # Only the SELECT carries a LIMIT; the COUNT must be unbounded.
        self.assertIn("limit",     select_sql)
        self.assertNotIn("limit",  count_sql)
        self.assertIn("count(*)",  count_sql)

    async def test_count_independent_of_limit_param(self):
        # The user requests `?limit=10`. Even if the SELECT only returns
        # 10 rows, the chip should still render the full count.
        conn = _FakeConn(scripted=[([], None), ([], 249)])
        engine = _FakeEngine(conn)
        api = _make_api()

        request = make_mocked_request(
            "GET", "/api/positions?limit=10",
            headers={"X-User-Id": "u1", "X-Bot-Secret": "test"},
        )

        with patch.object(api, "_user_executor", return_value=_StubExecutor()), \
             patch.object(bot_api_mod, "get_engine", return_value=engine):
            response = await api._handle_positions(request)

        import json as _json
        body = _json.loads(response.body)
        self.assertEqual(body["settled_total"], 249,
                         "settled_total must NOT be capped by the LIMIT")
        # The SELECT received `lim=10`, but the COUNT got no `lim` param.
        self.assertEqual(conn.params[0].get("lim"), 10)
        self.assertNotIn("lim", conn.params[1])

    async def test_count_falls_back_to_zero_on_db_error(self):
        # Defence-in-depth: if the inner closure throws, the handler
        # logs and returns settled=[] and settled_total=0 rather than
        # 500'ing the page. The current code wraps the work in a
        # try/except that resets BOTH locals to safe values.
        class _BoomEngine:
            def begin(self):
                raise RuntimeError("simulated DB outage")

        api = _make_api()
        request = make_mocked_request(
            "GET", "/api/positions",
            headers={"X-User-Id": "u1", "X-Bot-Secret": "test"},
        )
        with patch.object(api, "_user_executor", return_value=_StubExecutor()), \
             patch.object(bot_api_mod, "get_engine", return_value=_BoomEngine()):
            response = await api._handle_positions(request)

        import json as _json
        body = _json.loads(response.body)
        self.assertEqual(body["settled"],       [])
        self.assertEqual(body["settled_total"], 0)


if __name__ == "__main__":
    unittest.main()
