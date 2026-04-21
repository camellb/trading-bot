"""Shared config manipulation utilities used by bot_api."""

import os
import re


ALLOWED_CONFIG_KEYS: dict[str, type] = {
    "PM_SHADOW_MIN_EDGE_BPS":      float,
    "PM_SHADOW_MIN_CONFIDENCE":    float,
    "PM_LIVE_MIN_EDGE_BPS":        float,
    "PM_LIVE_MIN_CONFIDENCE":      float,
    "PM_KELLY_FRACTION":           float,
    "PM_MAX_POSITION_PCT":         float,
    "PM_MIN_TRADE_USD":            float,
    "PM_MAX_TRADE_USD":            float,
    "PM_MAX_CONCURRENT_POSITIONS": int,
    "PM_SCAN_LIMIT":               int,
    "PM_MIN_VOLUME_24H_USD":       float,
    "PM_MAX_DAYS_TO_END":          int,
    "PM_SKIP_EXISTING_DAYS":       int,
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
