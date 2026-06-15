"""
Polymarket connectivity probe.

Runs every 5 min from main.py's scheduler. Determines whether the
bot can (a) reach Polymarket at all, and (b) actually place trades
from this network. The state machine has three values:

    ok           Everything works.
    unreachable  Can't reach Polymarket servers (DNS hijack, ISP
                 block, regional outage, network down).
    geo_blocked  Reachable but recent orders rejected with HTTP 403
                 "Trading restricted" - typically means the VPN
                 exit node is in a blocked region, or no VPN.

User instruction 2026-06-08: "I feel like we should have some kind
of confirmation on switching it on cause I literally wouldn't know
whether it works or not if I didnt ask you." Surfaced via Telegram
on every state TRANSITION (silent on initial probe / steady-state).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests

from db.engine import app_data_dir, get_engine
from sqlalchemy import text


# Tight timeout: we'd rather mark unreachable + retry next tick than
# stall the scheduler for 30s on a dead network.
CONNECTIVITY_PROBE_TIMEOUT_SEC = 8.0
# Look for 403 / "Trading restricted" events within this window when
# deciding geo_blocked. Tight enough that a one-off rejection from
# yesterday doesn't keep the banner red forever.
RECENT_403_WINDOW_MIN = 15


def probe_polymarket_connectivity() -> dict:
    """Run one connectivity probe. Cheap (one HTTP HEAD-class call +
    one SQL count). Safe to invoke from any thread; raises only on
    truly catastrophic errors (which the caller treats as
    unreachable).

    Returns:
        {
          "state":             "ok" | "unreachable" | "geo_blocked",
          "gamma_latency_ms":  int | None,   # probe round-trip
          "recent_403_count":  int,          # window-scoped event_log count
          "detail":            str,          # human-readable summary
          "checked_at":        ISO 8601 UTC string,
        }
    """
    detail_parts: list[str] = []

    # 1. Reach test: gamma-api is public (no auth), cheap, and shares
    # the same Cloudflare front as the trading endpoints. If gamma
    # responds we're 99% sure data-api and clob can also be reached
    # (modulo per-endpoint blocks which manifest later as 4xx, caught
    # by the 403 check below).
    gamma_ok = False
    gamma_latency_ms: Optional[int] = None
    try:
        t0 = datetime.now(timezone.utc)
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 1},
            timeout=CONNECTIVITY_PROBE_TIMEOUT_SEC,
            headers={"User-Agent": "delfibot/connectivity-probe"},
        )
        gamma_latency_ms = int(
            (datetime.now(timezone.utc) - t0).total_seconds() * 1000
        )
        if r.status_code == 200:
            gamma_ok = True
        else:
            detail_parts.append(f"gamma-api returned HTTP {r.status_code}")
    except Exception as exc:
        detail_parts.append(
            f"gamma-api unreachable: {type(exc).__name__}"
        )

    # 2. Recent 403s in event_log. If gamma works but orders are being
    # rejected with "Trading restricted", the network is fine but the
    # exit IP is in Polymarket's blocklist. Common when the VPN exit
    # is geographically wrong (e.g. VPN routes through a US datacenter
    # which Polymarket blocks post-CFTC).
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=RECENT_403_WINDOW_MIN,
    )
    recent_403 = 0
    try:
        engine = get_engine()
        with engine.connect() as conn:
            count = conn.execute(text(
                "SELECT COUNT(*) FROM event_log "
                "WHERE event_type IN ('order_error','order_rejected') "
                "  AND timestamp > :cutoff "
                "  AND (description LIKE '%status_code=403%' "
                "    OR description LIKE '%Trading restricted%')"
            ), {"cutoff": cutoff}).scalar()
            recent_403 = int(count or 0)
    except Exception as exc:
        # Don't fail the probe on a DB hiccup; treat as "no recent
        # 403s seen" and let the next tick try again.
        print(
            f"[connectivity_probe] event_log read failed: {exc}",
            file=sys.stderr,
        )

    # 3. Decide state.
    if not gamma_ok:
        state = "unreachable"
        detail_parts.append("can't reach Polymarket servers")
    elif recent_403 > 0:
        state = "geo_blocked"
        detail_parts.append(
            f"{recent_403} order(s) rejected with HTTP 403 "
            f"'Trading restricted' in last {RECENT_403_WINDOW_MIN}min"
        )
    else:
        state = "ok"
        detail_parts.append(
            f"gamma-api {gamma_latency_ms}ms"
            if gamma_latency_ms is not None else "gamma-api ok"
        )

    return {
        "state": state,
        "gamma_latency_ms": gamma_latency_ms,
        "recent_403_count": recent_403,
        "detail": "; ".join(detail_parts),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# State values that mean "trades will fail; do not attempt." Includes
# both geo_blocked (orders 403) and unreachable (servers down / DNS
# hijacked). "ok" and "unknown" both pass through (the latter only
# briefly during the first ~20s after boot before the probe has run).
_BLOCKING_STATES = ("unreachable", "geo_blocked")

# Stale cache threshold. If the most recent probe is older than this,
# the cached state is no longer trustworthy (the user may have flipped
# VPN since the last probe wrote the file). The cheap gates that read
# the cache treat stale as blocked - better to skip a scan than to
# burn LLM tokens on markets we can no longer trade. Tuned tighter
# than the probe interval (60s) so a single missed probe still
# triggers the stale-equals-blocked path.
_STALE_CACHE_SEC = 120


def connectivity_blocks_trading() -> Tuple[bool, Optional[str]]:
    """Cheap read of the cached connectivity state. <1ms steady-state.

    Returns:
        (blocked, state)
          blocked - True if the latest probe says trades will fail OR
                    the cache is older than _STALE_CACHE_SEC.
          state   - The probe's state string when blocked
                    ("unreachable", "geo_blocked", "stale"), else None.

    Behaviour:
      * Missing state file (fresh boot, file not written yet) -> NOT
        blocked. The first connectivity probe fires at +20s post-boot;
        pm_scan's first fire is at +30s by default, so the file is
        usually there. If it isn't, fail-permissive so the system
        attempts work instead of silently stalling.
      * Corrupt JSON / IO error -> NOT blocked. Same rationale.
      * state == "ok" AND fresh -> NOT blocked.
      * state == "ok" BUT cache is older than _STALE_CACHE_SEC ->
        blocked with reason="stale". The user may have flipped VPN
        since the last probe wrote the file; tokens-side gates need
        fresh ground truth and should re-probe inline.
      * state in {"unreachable", "geo_blocked"} -> blocked.

    User instructions:
      * 2026-06-13: "The bot should recognise when it's geoblocked and
        should stop trying to work - it should just stop operating -
        it should pause."
      * 2026-06-15: "Delfi shouldn't try to place orders when it's in
        restricted region so why the fuck is it keep trying? It's just
        fukcing wasting my tokens. It should start as soon as you are
        in location (IP) that's not restricted." Tightened the gate:
        stale cache now reads as blocked so a freshly-flipped VPN
        can't slip through a 5-min window of stale "ok".
    """
    state_path = app_data_dir() / "connectivity_state.json"
    if not state_path.exists():
        return False, None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False, None
    state = (data or {}).get("state")
    if state in _BLOCKING_STATES:
        return True, state
    # Stale-equals-blocked check. The cheap gate cannot trust a cache
    # older than _STALE_CACHE_SEC because the user may have switched
    # network state since.
    checked_at = (data or {}).get("checked_at")
    if checked_at:
        try:
            ts = datetime.fromisoformat(checked_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
            if age_sec > _STALE_CACHE_SEC:
                return True, "stale"
        except Exception:
            # Malformed timestamp - treat as stale to be conservative.
            return True, "stale"
    return False, None
