"""
Telegram outbound notifier.

The desktop app pushes a small set of events to a user-supplied
Telegram bot so the user can see what Delfi is doing without keeping
the dashboard open. The bot is BYO: the user creates a bot via
@BotFather, gets a token, finds their numeric chat id (e.g. via
@userinfobot), and pastes both into Settings -> Notifications.

The token is a secret (anyone with it can read messages and post on
behalf of the bot) so it lives in the OS keychain. The chat id is
just a recipient identifier and lives in `user_config.telegram_chat_id`.

This module is a thin client. We do not run a long-lived Telegram
process, listen for commands, or render Markdown templates: the
desktop dashboard is the control surface, Telegram is read-only push.

Failure policy: every send is best-effort. If Telegram is unreachable
or the token is wrong we log to stderr and return False. Notification
delivery never blocks trading.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Optional, Tuple

from engine.user_config import (
    DEFAULT_USER_ID,
    get_telegram_bot_token,
    get_user_config,
)


_API_BASE = "https://api.telegram.org"
_DEFAULT_TIMEOUT = 6.0  # short on purpose, we never want to block a hot path


def _post(token: str, method: str, payload: dict, *, timeout: float) -> Tuple[bool, Optional[str]]:
    """POST to Telegram's bot API. Returns (ok, error_str_or_None).

    Defence in depth: we set a per-call socket-level timeout AND pass
    `timeout=` to urlopen. Some PyInstaller-frozen Pythons have been
    seen to ignore the urlopen timeout during the TLS handshake, so
    the socket default acts as a hard ceiling.
    """
    url = f"{_API_BASE}/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
    )
    import socket as _socket
    _prev_default = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(timeout)
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Telegram returns a JSON body with `description` on errors.
            try:
                data = json.loads(exc.read())
                return False, str(data.get("description") or f"HTTP {exc.code}")
            except Exception:
                return False, f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return False, f"could not reach Telegram: {exc.reason}"
        except _socket.timeout:
            return False, "Telegram took too long to respond (timeout)"
        except Exception as exc:
            return False, f"send failed: {exc}"
    finally:
        _socket.setdefaulttimeout(_prev_default)

    try:
        data = json.loads(raw)
    except Exception:
        return False, "malformed Telegram response"
    if not data.get("ok"):
        return False, str(data.get("description") or "Telegram rejected the request")
    return True, None


def send_test(token: str, chat_id: str, *, timeout: float = _DEFAULT_TIMEOUT) -> Tuple[bool, Optional[str]]:
    """Probe a brand-new (token, chat_id) pair from the Settings page.

    Used by `/api/config/telegram/test`. Sends a short verification
    message; the user sees it on their phone and clicks Save. We do
    NOT persist anything from inside this function, so a failed test
    leaves keychain + DB untouched.
    """
    if not token or not token.strip():
        return False, "bot token is empty"
    if not chat_id or not chat_id.strip():
        return False, "chat id is empty"
    return _post(
        token.strip(),
        "sendMessage",
        {
            "chat_id":   chat_id.strip(),
            "text":      "Delfi connected. You'll see trades, settlements, and risk events here.",
            "parse_mode": "HTML",
        },
        timeout=timeout,
    )


def notify(
    text: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bool:
    """Best-effort send to whatever Telegram chat the user configured.

    Called from `db.logger.log_event` so every in-app event also pushes
    to Telegram when configured. Returns True on send, False on any
    failure (incl. "not configured"); never raises.
    """
    try:
        token = get_telegram_bot_token()
        if not token:
            return False
        cfg = get_user_config(user_id)
        chat_id = (cfg.telegram_chat_id or "").strip()
        if not chat_id:
            return False
        # Telegram caps a single sendMessage at 4096 chars. Trim with a
        # leading ellipsis if anything trips that on real event copy.
        body = text if len(text) <= 4096 else text[:4093] + "..."
        ok, err = _post(
            token,
            "sendMessage",
            {
                "chat_id":   chat_id,
                "text":      body,
                "parse_mode": "HTML",
                # Keep the chat tidy. Trades fire fast; we don't want a
                # 30-line preview from URL unfurls.
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        if not ok:
            print(f"[telegram_notifier] send failed: {err}", file=sys.stderr)
        return ok
    except Exception as exc:
        print(f"[telegram_notifier] notify error: {exc}", file=sys.stderr)
        return False
