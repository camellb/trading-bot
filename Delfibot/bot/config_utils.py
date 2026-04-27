"""Shared config manipulation utilities used by bot_api."""

import os
import re


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", ""):
            return False
    raise ValueError(f"cannot coerce {v!r} to bool")


ALLOWED_CONFIG_KEYS: dict[str, type] = {
    # Global scan and safety limits. Per-user risk configuration
    # (min_ev_threshold, stake percentages, circuit breakers) lives in
    # user_config and is edited via /api/user-config.
    "PM_MAX_CONCURRENT_POSITIONS": int,
    "PM_SCAN_LIMIT":               int,
    "PM_MIN_VOLUME_24H_USD":       float,
    "PM_MAX_DAYS_TO_END":          int,
    "PM_SKIP_EXISTING_DAYS":       int,
    "PM_SCAN_ENABLED":             _to_bool,
    "PM_SCAN_INTERVAL_MINUTES":    int,
}


def persist_config_value(key: str, value) -> None:
    """Rewrite the first `KEY = …` line in config.py with the new value."""
    cfg_path = os.path.join(os.path.dirname(__file__), "config.py")
    with open(cfg_path, "r", encoding="utf-8") as f:
        src = f.read()
    pattern = re.compile(rf"(?m)^({re.escape(key)}\s*=\s*)[^\n#]*(\s*(?:#.*)?)$")
    new_literal = repr(value) if isinstance(value, str) else str(value)
    new_src, n = pattern.subn(rf"\g<1>{new_literal}\g<2>", src, count=1)
    if n != 1:
        raise RuntimeError(f"config.py has no top-level assignment for {key}")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(new_src)
