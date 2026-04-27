"""
Send-only Telegram notifier for the Delfi desktop app.

Background
==========
The SaaS notifier was a long-running process with two halves:
  * inbound polling (`/apply`, `/skip`, `/pause` commands)
  * outbound notifications (position opened, settled, daily summary, ...)

The local desktop app retires the inbound side completely: every action
the user used to type into Telegram is now an in-app button (Apply,
Skip, Snooze, Pause, Resume, etc.). What's left is purely outbound -
the user opts in once, pastes a bot token + chat id, and Delfi ships
short status pings to the user's own DM with their own bot.

This module is therefore a tiny HTTP client around the Bot API. No
polling thread, no command router, no per-deploy daemon. It calls
`api.telegram.org/bot<TOKEN>/sendMessage` synchronously when the engine
wants to notify, gates by per-category notification prefs from
`engine/user_config.py:should_notify`, and tolerates any HTTP error
without crashing the engine (notifications are best-effort).

Usage
=====
    from feeds import telegram_notifier

    telegram_notifier.notify(
        category="position_opened",
        text="Delfi opened YES on 'Lakers vs Celtics'  $4.32",
    )

    # Used by the Settings UI to validate the user's setup.
    ok, detail = telegram_notifier.send_test_message()

The notifier reads the bot token from the OS keychain via
`engine.user_config.get_telegram_bot_token()` and the chat id from the
SQLite-backed user_config row. If either is missing, every call returns
silently (no raised exceptions) so the engine never blocks on Telegram.

Trust + safety
==============
Outbound only. The token is keychain-backed; we never log it, never
echo it back through any API, never write it to disk. The only network
target is `api.telegram.org`. No file uploads, no media, no inline
keyboards - plain text Markdown messages.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from engine.user_config import (
    DEFAULT_USER_ID,
    NOTIFICATION_CATEGORIES,
    get_telegram_bot_token,
    get_user_config,
    should_notify,
)


_TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT_S = 5.0


def _post(token: str, path: str, payload: dict) -> Tuple[bool, str]:
    """POST `payload` as JSON to api.telegram.org/bot<token>/<path>.

    Returns `(ok, detail)`. Detail is the API response 'description'
    on failure or the empty string on success. Never raises.
    """
    url = f"{_TELEGRAM_API}/bot{token}/{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if not data.get("ok"):
                return False, str(data.get("description") or "telegram API rejected")
            return True, ""
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8", errors="replace"))
            return False, str(data.get("description") or f"HTTP {exc.code}")
        except Exception:
            return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"network error: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def is_configured(user_id: str = DEFAULT_USER_ID) -> bool:
    """True iff the user has both a bot token (keychain) and a chat id (DB)."""
    if not get_telegram_bot_token():
        return False
    cfg = get_user_config(user_id)
    return bool(cfg.telegram_chat_id and cfg.telegram_chat_id.strip())


def notify(
    *,
    category: str,
    text: str,
    user_id: str = DEFAULT_USER_ID,
    parse_mode: Optional[str] = "Markdown",
) -> Tuple[bool, str]:
    """Send `text` to the user's Telegram DM if they opted in.

    Returns `(ok, detail)`. `ok=False` does not raise: a missing token,
    a disabled category, or a network error is logged to stderr and
    swallowed so the engine never blocks on Telegram.
    """
    if category and category not in NOTIFICATION_CATEGORIES:
        # Unknown categories are tolerated - we still gate on should_notify
        # which permits unknown labels but logs the typo.
        print(f"[telegram] unknown category {category!r}", file=sys.stderr)

    if not should_notify(user_id, category):
        return False, "category disabled"

    token = get_telegram_bot_token()
    if not token:
        return False, "no bot token"

    cfg = get_user_config(user_id)
    chat_id = (cfg.telegram_chat_id or "").strip()
    if not chat_id:
        return False, "no chat id"

    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    ok, detail = _post(token, "sendMessage", payload)
    if not ok:
        print(f"[telegram] notify failed ({category}): {detail}", file=sys.stderr)
    return ok, detail


def send_test_message(user_id: str = DEFAULT_USER_ID) -> Tuple[bool, str]:
    """Used by the Settings UI to validate the user's bot token + chat id.

    Bypasses `should_notify` so the user can test even with every
    category disabled. Returns the same `(ok, detail)` shape as
    `notify`.
    """
    token = get_telegram_bot_token()
    if not token:
        return False, "no bot token"

    cfg = get_user_config(user_id)
    chat_id = (cfg.telegram_chat_id or "").strip()
    if not chat_id:
        return False, "no chat id"

    return _post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": (
            "Delfi test message. Notifications are wired up correctly. "
            "You can disable individual categories in Settings -> Notifications."
        ),
        "disable_web_page_preview": True,
    })
