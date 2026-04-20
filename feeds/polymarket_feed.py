"""
Polymarket Gamma API client — shadow mode.

Read-only. Purpose is to surface a curated candidate list of binary markets
where an LLM could plausibly add value (genuine uncertainty, liquid, resolvable
within a usable horizon), plus to pull resolved-market outcomes so the
calibration loop can score past predictions.

No trading.  No authentication.  All endpoints public.

Selection heuristic for candidates:
  * price of YES in [0.08, 0.92]  — genuine uncertainty
  * accepting orders                — market is live, not settling
  * 0 <= days_to_end <= 180         — resolves in a usable horizon
  * volume24hrClob >= min_volume_24h — enough informed flow

The heuristic deliberately excludes the ~70% of markets that trade at
<5% or >95%; those are either decided or meme-priced longshots where a
calibrated model has nothing to add.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

GAMMA_BASE = "https://gamma-api.polymarket.com"
DEFAULT_MIN_VOLUME_24H = 5_000.0
DEFAULT_MAX_DAYS_OUT    = 120
DEFAULT_MIN_DAYS_OUT    = 0
DEFAULT_MIN_P           = 0.08
DEFAULT_MAX_P           = 0.92


@dataclass
class PolyMarket:
    """Distilled view of a Polymarket market, ready to feed to Claude."""
    id:                str
    condition_id:      str
    question:          str
    description:       str
    outcome_yes:       str        # usually "Yes"; occasionally "Over"/"A"/etc.
    outcome_no:        str
    yes_price:         float      # current market price of the YES outcome
    no_price:          float
    volume_24h_clob:   float
    liquidity_num:     float
    end_date_iso:      datetime
    slug:              str
    category_hint:     Optional[str]  # events[0].ticker when available
    neg_risk:          bool = False   # part of a multi-outcome group
    group_item_title:  Optional[str] = None  # specific option label (e.g. "Spain")
    event_slug:        Optional[str] = None  # event group slug for correlation caps
    resolution_source: Optional[str] = None  # URL of the resolution authority (e.g. ESPN, Reuters)

    @property
    def days_to_end(self) -> float:
        return (self.end_date_iso - datetime.now(timezone.utc)).total_seconds() / 86400.0


def _parse_price_list(raw: str | list | None) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [float(x) for x in raw]
    try:
        return [float(x) for x in json.loads(raw)]
    except Exception:
        return []


def _parse_str_list(raw: str | list | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        return [str(x) for x in json.loads(raw)]
    except Exception:
        return []


def _parse_iso(raw: str | None) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        # Gamma sometimes returns naive ISO strings — force UTC so arithmetic
        # with tz-aware now() doesn't raise.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _has_explicit_time(raw: str | None) -> bool:
    if not raw:
        return False
    text = str(raw)
    return ("T" in text or " " in text) and ":" in text


def parse_market_end_time(m: dict) -> Optional[datetime]:
    """
    Prefer the exact market end timestamp when Gamma provides one.

    Gamma often returns `endDateIso` as a date-only string like `2026-04-18`,
    which would parse as midnight UTC and make markets appear resolved hours
    too early. We therefore prefer any field with an explicit time component.
    """
    for key in ("endDate", "endDateIso"):
        raw = m.get(key)
        if _has_explicit_time(raw):
            dt = _parse_iso(raw)
            if dt is not None:
                return dt
    for key in ("endDateIso", "endDate"):
        dt = _parse_iso(m.get(key))
        if dt is not None:
            return dt
    return None


def parse_market_event_start_time(m: dict) -> Optional[datetime]:
    raw = m.get("gameStartTime")
    dt = _parse_iso(raw) if raw else None
    return dt or parse_market_end_time(m)


def looks_like_sports_market(m: dict) -> bool:
    if m.get("sportsMarketType"):
        return True
    source = str(m.get("resolutionSource") or "").lower()
    return any(
        marker in source for marker in (
            "nba.com",
            "mlb.com",
            "premierleague.com",
            "mlssoccer.com",
            "ufc.com",
            "espn.com",
        )
    )


def _sports_runtime_estimate(m: dict) -> timedelta:
    blob = " ".join(
        str(m.get(key) or "")
        for key in ("question", "slug", "sportsMarketType", "resolutionSource", "description")
    ).lower()
    if any(token in blob for token in ("cricket", "t20", "odi", "test match")):
        return timedelta(hours=8)
    if any(token in blob for token in ("tennis", "atp", "wta")):
        return timedelta(hours=4)
    if any(token in blob for token in ("mlb", "baseball")):
        return timedelta(hours=4)
    if any(token in blob for token in ("nba", "basketball", "ufc", "mma", "boxing")):
        return timedelta(hours=3)
    if any(token in blob for token in ("soccer", "football", "premierleague", "mls", " fc ")):
        return timedelta(hours=3)
    return timedelta(hours=4)


def _settlement_grace_from_description(m: dict, default: timedelta) -> timedelta:
    desc = str(m.get("description") or "").lower()
    match = re.search(
        r"within\s+(\d+)\s+hours?\s+after\s+the\s+event'?s?\s+conclusion",
        desc,
    )
    if match:
        try:
            return timedelta(hours=max(0, int(match.group(1))))
        except Exception:
            pass
    custom_liveness = int(m.get("customLiveness") or 0)
    if custom_liveness > 0:
        return max(default, timedelta(seconds=custom_liveness))
    return default


def estimate_market_settlement_deadline(m: dict) -> Optional[datetime]:
    """
    Best-effort timestamp after which an unresolved market should be treated as
    stale rather than merely awaiting the official result.

    For sports markets, Gamma's `endDate` is typically the scheduled start time,
    not the final settlement time, so we add an event-duration estimate plus the
    resolution-source grace window when available.
    """
    event_start = parse_market_event_start_time(m)
    if event_start is None:
        return None
    if looks_like_sports_market(m):
        return (
            event_start
            + _sports_runtime_estimate(m)
            + _settlement_grace_from_description(m, timedelta(hours=2))
        )
    return event_start + _settlement_grace_from_description(m, timedelta(minutes=30))


def _as_market(m: dict) -> Optional[PolyMarket]:
    """Map a raw Gamma dict to a PolyMarket, or None if it isn't binary/parseable."""
    try:
        if m.get("negRiskOther"):
            return None
        prices   = _parse_price_list(m.get("outcomePrices"))
        outcomes = _parse_str_list(m.get("outcomes"))
        if len(prices) != 2 or len(outcomes) != 2:
            return None
        end_iso = parse_market_end_time(m)
        if end_iso is None:
            return None
        events = m.get("events") or []
        cat_hint = None
        event_slug = None
        if isinstance(events, list) and events:
            e0 = events[0] or {}
            cat_hint = e0.get("ticker") or e0.get("title") or None
            event_slug = e0.get("slug") or None
        return PolyMarket(
            id             = str(m.get("id") or ""),
            condition_id   = str(m.get("conditionId") or ""),
            question       = str(m.get("question") or "").strip(),
            description    = str(m.get("description") or "").strip(),
            outcome_yes    = outcomes[0],
            outcome_no     = outcomes[1],
            yes_price      = float(prices[0]),
            no_price       = float(prices[1]),
            volume_24h_clob = float(m.get("volume24hrClob") or 0),
            liquidity_num  = float(m.get("liquidityNum") or 0),
            end_date_iso   = end_iso,
            slug           = str(m.get("slug") or ""),
            category_hint  = cat_hint,
            neg_risk       = bool(m.get("negRisk")),
            group_item_title = (m.get("groupItemTitle") or "").strip() or None,
            event_slug     = event_slug,
            resolution_source = (m.get("resolutionSource") or "").strip() or None,
        )
    except Exception as exc:
        print(f"[polymarket] parse failed for {m.get('id')}: {exc}",
              file=sys.stderr)
        return None


class PolymarketFeed:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session: aiohttp.ClientSession | None = session
        self._own_session = session is None

    async def __aenter__(self) -> "PolymarketFeed":
        if self._session is None:
            # Force ThreadedResolver (getaddrinfo in executor) instead of
            # aiodns/pycares.  Rapidly creating and destroying sessions with
            # aiodns can corrupt the event loop's selector state — pycares
            # registers/unregisters file descriptors on each channel, and a
            # race during cleanup can accidentally remove the HTTP-server
            # listen socket, permanently freezing the API.
            connector = aiohttp.TCPConnector(
                resolver=aiohttp.resolver.ThreadedResolver(),
                ttl_dns_cache=300,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={"User-Agent": "trading-bot/1.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self

    async def __aexit__(self, *_exc):
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict | None = None) -> list[dict] | dict | None:
        assert self._session is not None
        url = f"{GAMMA_BASE}{path}"
        try:
            async with self._session.get(url, params=params) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    print(f"[polymarket] {r.status} {url} → {body}", file=sys.stderr)
                    return None
                return await r.json()
        except Exception as exc:
            print(f"[polymarket] GET {url} failed: {exc}", file=sys.stderr)
            return None

    # ── Candidate markets ─────────────────────────────────────────────────
    async def fetch_candidate_markets(
        self,
        limit:            int   = 25,
        scan_pages:       int   = 10,      # 10 × 100 = top 1000 by 24h volume
        min_volume_24h:   float = DEFAULT_MIN_VOLUME_24H,
        min_p:            float = DEFAULT_MIN_P,
        max_p:            float = DEFAULT_MAX_P,
        min_days:         int   = DEFAULT_MIN_DAYS_OUT,
        max_days:         int   = DEFAULT_MAX_DAYS_OUT,
    ) -> list[PolyMarket]:
        """
        Return up to `limit` candidate markets, ranked by 24h CLOB volume
        among those that pass the uncertainty / horizon / liquidity gates.
        """
        all_rows: list[dict] = []
        for page in range(scan_pages):
            data = await self._get("/markets", {
                "active":    "true",
                "closed":    "false",
                "limit":     "100",
                "order":     "volume24hr",
                "ascending": "false",
                "offset":    str(page * 100),
            })
            if not isinstance(data, list) or not data:
                break
            all_rows.extend(data)

        kept: list[PolyMarket] = []
        for row in all_rows:
            if not row.get("acceptingOrders", False):
                continue
            mk = _as_market(row)
            if mk is None:
                continue
            if not (min_p <= mk.yes_price <= max_p):
                continue
            d = mk.days_to_end
            if d < min_days or d > max_days:
                continue
            if mk.volume_24h_clob < min_volume_24h:
                continue
            kept.append(mk)

        # Prioritise short-horizon markets (≤7 days) so they get evaluated first,
        # then backfill with longer-horizon markets.  Within each tier, rank by volume.
        short = [m for m in kept if m.days_to_end <= 7]
        long  = [m for m in kept if m.days_to_end > 7]
        short.sort(key=lambda m: m.volume_24h_clob, reverse=True)
        long.sort(key=lambda m: m.volume_24h_clob, reverse=True)
        return (short + long)[:limit]

    # ── Resolution lookup ─────────────────────────────────────────────────
    async def fetch_market(self, market_id: str) -> Optional[dict]:
        """Return the raw market row for a given id (needed to check resolution)."""
        data = await self._get(f"/markets/{market_id}")
        return data if isinstance(data, dict) else None

    async def fetch_many(self, market_ids: list[str]) -> dict[str, dict]:
        """
        Fetch multiple markets concurrently (bounded concurrency).
        Returns {id: market_row}. Missing entries mean the fetch failed.
        """
        sem = asyncio.Semaphore(4)
        async def one(mid: str):
            async with sem:
                return mid, await self.fetch_market(mid)
        pairs = await asyncio.gather(*(one(m) for m in market_ids))
        return {mid: row for mid, row in pairs if row is not None}
