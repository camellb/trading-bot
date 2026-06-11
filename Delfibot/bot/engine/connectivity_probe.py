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

import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from db.engine import get_engine
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
