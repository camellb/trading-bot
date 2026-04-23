"""
Multi-tenant Telegram notifier - per-user credential lookup and opt-in no-op.

These tests stub the DB credential lookup and the aiohttp session so the
notifier can be exercised without a database, network, or real bot token.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feeds.telegram_notifier import TelegramNotifier


class _FakePostCtx:
    def __init__(self, status: int = 200, body: str = "{}"):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def text(self):
        return self._body


class _FakeSession:
    def __init__(self):
        self.post_calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, *, json):
        self.post_calls.append((url, json))
        return _FakePostCtx(status=200, body="{}")


class TelegramNotifierMultiTenantTests(unittest.TestCase):

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setUp(self):
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_configured_user_sends(self):
        notifier = TelegramNotifier()
        session = _FakeSession()
        notifier._session = session  # type: ignore[assignment]

        with patch("feeds.telegram_notifier.get_user_telegram_creds",
                   return_value=("TOKEN_ABC", "11111")):
            self._run(notifier.send("user-with-creds", "hello world"))

        self.assertEqual(len(session.post_calls), 1)
        url, payload = session.post_calls[0]
        self.assertIn("TOKEN_ABC", url)
        self.assertEqual(payload["chat_id"], "11111")
        self.assertEqual(payload["text"], "hello world")
        self.assertEqual(payload["parse_mode"], "HTML")

    def test_unconfigured_user_silently_no_ops(self):
        notifier = TelegramNotifier()
        session = _FakeSession()
        notifier._session = session  # type: ignore[assignment]

        with patch("feeds.telegram_notifier.get_user_telegram_creds",
                   return_value=None):
            self._run(notifier.send("user-without-creds", "hello world"))

        self.assertEqual(session.post_calls, [])

    def test_creds_cache_skips_second_db_lookup(self):
        notifier = TelegramNotifier()
        session = _FakeSession()
        notifier._session = session  # type: ignore[assignment]

        with patch("feeds.telegram_notifier.get_user_telegram_creds",
                   return_value=("TOKEN_ABC", "11111")) as lookup:
            self._run(notifier.send("user-with-creds", "one"))
            self._run(notifier.send("user-with-creds", "two"))
            self.assertEqual(lookup.call_count, 1)

        self.assertEqual(len(session.post_calls), 2)

    def test_invalidate_creds_forces_reload(self):
        notifier = TelegramNotifier()
        session = _FakeSession()
        notifier._session = session  # type: ignore[assignment]

        with patch("feeds.telegram_notifier.get_user_telegram_creds",
                   return_value=("TOKEN_ABC", "11111")) as lookup:
            self._run(notifier.send("user-with-creds", "one"))
            notifier.invalidate_creds("user-with-creds")
            self._run(notifier.send("user-with-creds", "two"))
            self.assertEqual(lookup.call_count, 2)

    def test_notify_settlement_no_ops_for_unconfigured_user(self):
        notifier = TelegramNotifier()
        session = _FakeSession()
        notifier._session = session  # type: ignore[assignment]
        notifier._executor = MagicMock()
        notifier._executor.get_bankroll.return_value = 1000.0

        with patch("feeds.telegram_notifier.get_user_telegram_creds",
                   return_value=None):
            self._run(notifier.notify_settlement(
                user_id="no-creds",
                position_id=1,
                question="Test?",
                side="YES",
                outcome="YES",
                pnl=5.0,
                cost=10.0,
            ))

        self.assertEqual(session.post_calls, [])


if __name__ == "__main__":
    unittest.main()
