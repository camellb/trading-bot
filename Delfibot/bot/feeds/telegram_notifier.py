"""
Telegram outbound notifier + inbound command listener.

The desktop app pushes a small set of events to a user-supplied
Telegram bot so the user can see what Delfi is doing without keeping
the dashboard open. The bot is BYO: the user creates a bot via
@BotFather, gets a token, finds their numeric chat id (e.g. via
@userinfobot), and pastes both into Settings -> Notifications.

The token is a secret (anyone with it can read messages and post on
behalf of the bot) so it lives in the OS keychain. The chat id is
just a recipient identifier and lives in `user_config.telegram_chat_id`.

Inbound commands:
  /help    - list commands
  /status  - portfolio summary + open positions
  /pause   - halt new trades (existing positions keep settling)
  /resume  - undo /pause
  /apply   - apply ALL pending learning-cycle suggestions
  /reject  - skip the oldest pending suggestion
  /start   - greeting (auto-fires on first message after BotFather setup)

The listener is a single daemon thread that long-polls
`/getUpdates` with a 5s timeout and a chat-id allowlist so other
people who message the bot get ignored. start_command_listener() is
idempotent and survives token/chat-id changes - call it again after
PUT /api/config/telegram and it'll restart against the new creds.

Failure policy: every outbound send is best-effort. If Telegram is
unreachable or the token is wrong we log to stderr and return False.
Notification delivery never blocks trading.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
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

# Listener state. One module-level thread is enough for a single-user
# desktop app. start_command_listener() builds it lazily from the
# current (token, chat_id) pair; if either changes we tear the thread
# down and respawn with the new creds.
_listener_thread: Optional[threading.Thread] = None
_listener_stop:   Optional[threading.Event]  = None
_listener_creds:  Tuple[Optional[str], Optional[str]] = (None, None)
_listener_lock = threading.Lock()


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


# ── Inbound commands ────────────────────────────────────────────────────────

# All user-facing copy is rendered via feeds.telegram_messages so
# Telegram output matches the Messages Spec v1 verbatim. Local
# import to keep module import time small (and avoid a circular at
# module-load if telegram_messages ever grows engine deps).
def _tm():
    from feeds import telegram_messages as _mod
    return _mod


def _handle_help(token: str, chat_id: str) -> None:
    _post(token, "sendMessage",
          {"chat_id": chat_id, "text": _tm().help_text(), "parse_mode": "HTML"},
          timeout=_DEFAULT_TIMEOUT)


def _handle_start(token: str, chat_id: str) -> None:
    _post(token, "sendMessage",
          {"chat_id": chat_id, "text": _tm().welcome(), "parse_mode": "HTML"},
          timeout=_DEFAULT_TIMEOUT)


def _handle_status(token: str, chat_id: str) -> None:
    """Pull portfolio stats + render via tm.status (Messages Spec v1)."""
    try:
        from engine.notifier_state import is_trading_paused
        from execution.pm_executor import PMExecutor
        executor = PMExecutor(DEFAULT_USER_ID)
        stats = executor.get_portfolio_stats()
        open_rows = executor.get_open_positions()

        pos_lines = []
        for p in open_rows[:10]:
            q = (p.get("question") or "")[:60]
            pos_lines.append(
                f"• {p.get('side','?')} ${float(p.get('cost_usd',0)):.2f}  {q}"
            )
        if len(open_rows) > 10:
            pos_lines.append(f"...and {len(open_rows) - 10} more.")
        positions_block = "\n".join(pos_lines) or "(none)"

        settled_total = int(stats.get("settled_total", 0))
        wins = int(stats.get("settled_wins", 0))
        losses = max(0, settled_total - wins)
        win_pct = (wins / settled_total * 100.0) if settled_total else 0.0

        text = _tm().status(
            paused=is_trading_paused(),
            mode=str(stats.get("mode", "simulation")),
            bankroll=float(stats.get("bankroll", 0.0)),
            open_positions=int(stats.get("open_positions", 0)),
            open_cost=float(stats.get("open_cost", 0.0)),
            wins=wins,
            losses=losses,
            win_pct=win_pct,
            realized_pnl=float(stats.get("realized_pnl", 0.0)),
            positions_block=positions_block,
        )
    except Exception as exc:
        print(f"[telegram_notifier] /status error: {exc}", file=sys.stderr)
        text = _tm().generic_error(context="Status", detail=str(exc))
    _post(token, "sendMessage",
          {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
           "disable_web_page_preview": True},
          timeout=_DEFAULT_TIMEOUT)


def _handle_pause(token: str, chat_id: str) -> None:
    from engine.notifier_state import is_trading_paused, set_trading_paused
    if is_trading_paused():
        text = _tm().already_paused()
    else:
        set_trading_paused(True)
        text = _tm().paused()
    _post(token, "sendMessage",
          {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
          timeout=_DEFAULT_TIMEOUT)


def _handle_resume(token: str, chat_id: str) -> None:
    from engine.notifier_state import is_trading_paused, set_trading_paused
    if not is_trading_paused():
        text = _tm().already_running()
    else:
        set_trading_paused(False)
        text = _tm().resumed()
    _post(token, "sendMessage",
          {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
          timeout=_DEFAULT_TIMEOUT)


def _handle_apply(token: str, chat_id: str) -> None:
    """Apply every pending suggestion in the learning queue at once.

    Apply-all matches the Intelligence page's bulk-apply: users almost
    always want every proposal from the most recent review, applying
    one row at a time is tedious. The Intelligence page still has
    per-row Apply/Skip buttons for finer control.
    """
    try:
        from engine.learning_cadence import apply_all_pending_suggestions
        result = apply_all_pending_suggestions(
            user_id=DEFAULT_USER_ID, resolved_by="telegram",
        )
    except Exception as exc:
        print(f"[telegram_notifier] /apply error: {exc}", file=sys.stderr)
        _post(token, "sendMessage",
              {"chat_id": chat_id,
               "text": _tm().generic_error(context="Applying change", detail=str(exc)),
               "parse_mode": "HTML"},
              timeout=_DEFAULT_TIMEOUT)
        return
    if result.get("status") == "none":
        text = _tm().nothing_pending()
    else:
        text = _tm().calibration_applied_all(
            applied=result.get("applied") or [],
            failed=result.get("failed") or [],
        )
    _post(token, "sendMessage",
          {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
          timeout=_DEFAULT_TIMEOUT)


def _handle_reject(token: str, chat_id: str) -> None:
    """Skip the oldest pending suggestion. Messages Spec v1."""
    try:
        from engine.learning_cadence import skip_next_pending_suggestion
        result = skip_next_pending_suggestion(
            user_id=DEFAULT_USER_ID, resolved_by="telegram",
        )
    except Exception as exc:
        print(f"[telegram_notifier] /reject error: {exc}", file=sys.stderr)
        _post(token, "sendMessage",
              {"chat_id": chat_id,
               "text": _tm().generic_error(context="Declining change", detail=str(exc)),
               "parse_mode": "HTML"},
              timeout=_DEFAULT_TIMEOUT)
        return
    text = _tm().calibration_declined() if result.get("status") == "skipped" else _tm().nothing_pending()
    _post(token, "sendMessage",
          {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
          timeout=_DEFAULT_TIMEOUT)


_COMMANDS = {
    "/help":   _handle_help,
    "/status": _handle_status,
    "/pause":  _handle_pause,
    "/resume": _handle_resume,
    "/apply":  _handle_apply,
    "/reject": _handle_reject,
    "/start":  _handle_start,
}


def _poll_loop(token: str, chat_id: str, stop: threading.Event) -> None:
    """Long-poll Telegram for updates and dispatch matching commands.

    Keeps an `offset` cursor so we never re-handle a message after a
    crash + restart. Skips messages from any chat other than the
    configured chat_id (the bot might end up in groups; we only obey
    its owner). Bot username suffix on commands ("/status@DelfiBot")
    is stripped so commands work in groups too.
    """
    base = f"{_API_BASE}/bot{token}"
    offset = 0

    # Initialize offset: skip any messages that were sent while Delfi
    # was offline. Without this, after a long stop the bot would fire
    # every queued /status from the past day.
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/getUpdates?offset=-1&timeout=0&allowed_updates=%5B%22message%22%5D"
            ),
            timeout=10,
        ) as r:
            init_data = json.loads(r.read())
        if init_data.get("ok"):
            results = init_data.get("result", [])
            if results:
                offset = results[-1]["update_id"] + 1
    except Exception as exc:
        print(f"[telegram_notifier] poll init warning: {exc}", file=sys.stderr)

    while not stop.is_set():
        try:
            url = (
                f"{base}/getUpdates"
                f"?offset={offset}&timeout=5&allowed_updates=%5B%22message%22%5D"
            )
            with urllib.request.urlopen(
                urllib.request.Request(url), timeout=15,
            ) as r:
                data = json.loads(r.read())
            if not data.get("ok"):
                stop.wait(5)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                sender = str((msg.get("chat") or {}).get("id", ""))
                if sender != chat_id:
                    # Ignore other chats (group adoption / strangers).
                    continue
                raw = (msg.get("text") or "").strip()
                if not raw:
                    continue
                # "/status@DelfiBot foo" -> "/status"
                cmd = raw.split()[0].split("@", 1)[0].lower()
                fn = _COMMANDS.get(cmd)
                if fn is None:
                    continue
                try:
                    fn(token, chat_id)
                except Exception as exc:
                    print(f"[telegram_notifier] dispatch {cmd} error: {exc}",
                          file=sys.stderr)
        except urllib.error.URLError as exc:
            # Network blip; back off briefly. stop.wait() returns True
            # when the event fires so we exit promptly on shutdown.
            print(f"[telegram_notifier] poll URL error: {exc}", file=sys.stderr)
            if stop.wait(5):
                return
        except Exception as exc:
            print(f"[telegram_notifier] poll loop error: {exc}", file=sys.stderr)
            if stop.wait(5):
                return


def start_command_listener() -> bool:
    """Start (or restart) the Telegram command listener.

    Idempotent. Reads the current (token, chat_id) from keychain + DB:
      - Both empty -> stop any running listener, return False (no-op).
      - Match the running listener's creds -> return True (already up).
      - Differ from the running listener -> stop + respawn.
    """
    global _listener_thread, _listener_stop, _listener_creds

    token = (get_telegram_bot_token() or "").strip()
    cfg = get_user_config()
    chat_id = (cfg.telegram_chat_id or "").strip()

    with _listener_lock:
        if not token or not chat_id:
            # Telegram disabled. Tear down any running listener.
            if _listener_stop:
                _listener_stop.set()
            _listener_thread = None
            _listener_stop = None
            _listener_creds = (None, None)
            return False

        if (_listener_thread and _listener_thread.is_alive()
                and _listener_creds == (token, chat_id)):
            return True  # already running with these creds

        # Either nothing running or the creds changed. Stop + respawn.
        if _listener_stop:
            _listener_stop.set()

        stop = threading.Event()
        thread = threading.Thread(
            target=_poll_loop,
            args=(token, chat_id, stop),
            daemon=True,
            name="delfi-telegram-poll",
        )
        thread.start()
        _listener_thread = thread
        _listener_stop = stop
        _listener_creds = (token, chat_id)
        return True


def stop_command_listener() -> None:
    """Signal the listener to exit. Safe to call when nothing's running."""
    global _listener_thread, _listener_stop, _listener_creds
    with _listener_lock:
        if _listener_stop:
            _listener_stop.set()
        _listener_thread = None
        _listener_stop = None
        _listener_creds = (None, None)
