"""
Tiny per-deploy bot pause state.

Originally fed the Telegram notifier's "first win" / "first loss"
one-shot messages too; those were retired with the local-first pivot.
The remaining responsibility is one boolean:
  * trading_paused   - is the bot paused (no new positions opened)?

Stored as JSON at <app-data>/data/notifier_state.json. Anchored to the
app-data directory (same place as the SQLite DB) so the file persists
across launches under PyInstaller. Anchoring at __file__ would resolve
to the per-launch _MEIxxxx tmp dir, which gets a fresh random name on
every spawn - writes would succeed but never read back.

Single-user deployment, so a file is plenty - we do not need a DB
table. Writes are atomic via os.replace(); a missing or malformed
file reads back as all-False.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Dict

from db.engine import app_data_dir


_DATA_DIR = app_data_dir() / "data"
_STATE_PATH = _DATA_DIR / "notifier_state.json"
_LOCK = threading.Lock()

_DEFAULTS: Dict[str, bool] = {
    "first_win_sent": False,
    "first_loss_sent": False,
    "trading_paused": False,
}


def _read() -> Dict[str, bool]:
    if not _STATE_PATH.exists():
        return dict(_DEFAULTS)
    try:
        with _STATE_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return {**_DEFAULTS, **{k: bool(raw.get(k, v)) for k, v in _DEFAULTS.items()}}
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULTS)


def _write(state: Dict[str, bool]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_DATA_DIR, prefix=".notifier_state.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update(key: str, value: bool) -> bool:
    with _LOCK:
        state = _read()
        prev = state.get(key, False)
        state[key] = bool(value)
        _write(state)
        return prev


def is_trading_paused() -> bool:
    return _read()["trading_paused"]


def set_trading_paused(paused: bool) -> bool:
    """Return the previous value."""
    return _update("trading_paused", paused)


def first_win_sent() -> bool:
    return _read()["first_win_sent"]


def first_loss_sent() -> bool:
    return _read()["first_loss_sent"]


def mark_first_win_if_unsent() -> bool:
    """Return True the first time this is called, False thereafter."""
    with _LOCK:
        state = _read()
        if state["first_win_sent"]:
            return False
        state["first_win_sent"] = True
        _write(state)
        return True


def mark_first_loss_if_unsent() -> bool:
    """Return True the first time this is called, False thereafter."""
    with _LOCK:
        state = _read()
        if state["first_loss_sent"]:
            return False
        state["first_loss_sent"] = True
        _write(state)
        return True
