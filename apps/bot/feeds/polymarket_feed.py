"""
Polymarket Gamma API client - simulation mode.

Read-only. Purpose is to surface a curated candidate list of binary markets
where an LLM could plausibly add value (genuine uncertainty, liquid, resolvable
within a usable horizon), plus to pull resolved-market outcomes so the
calibration loop can score past predictions.

No trading.  No authentication.  All endpoints public.

Selection heuristic for candidates:
  * price of YES in [0.08, 0.92]  - genuine uncertainty
  * accepting orders                - market is live, not settling
  * 0 <= days_to_end <= 180         - resolves in a usable horizon
  * volume24hrClob >= min_volume_24h - enough informed flow

The heuristic deliberately excludes the ~70% of markets that trade at
<5% or >95%; those are either decided or meme-priced longshots where a
calibrated model has nothing to add.

Resolution-time semantics. Polymarket does NOT publish a single reliable
"the market resolves at this timestamp" field on open markets; the
canonical fields all mean different things and any of them can be wrong:

  * `endDate` / `endDateIso`  - trading-window close on the market row.
                                Often a buffered or arbitrary date set
                                at creation; the underlying outcome can
                                resolve earlier (deadline-style markets:
                                "X by April 30") or later (sports: end
                                of game vs trading-close at tip).
  * `events[0].endDate`       - resolution deadline at the event level.
                                Usually a better proxy for "by when does
                                this resolve at the latest".
  * `gameStartTime`           - sports only. Tip / kickoff / first-pitch.
                                Game finishes ~2-3h later.

`resolution_at_estimate` blends these to produce the best static guess
of when a market will actually settle. The settler refreshes this value
on every sweep so a market whose deadline shifts (or which Polymarket
has already marked closed) does not show a stale countdown on the
dashboard.
"""

from __future__ import annotations

import asyncio
import json
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

# Sports games resolve roughly this long after `gameStartTime` (tip /
# kickoff / first-pitch). 3h covers ~99% of NBA/NFL/MLB/EPL games plus
# overtime and Polymarket's settlement lag. Tighter than this and we'd
# show "now" on a still-running game; looser and we'd lie about how
# long the user is waiting.
SPORTS_RESOLUTION_BUFFER = timedelta(hours=3)


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
    end_date_iso:      datetime  # trading-window close (NOT resolution time)
    slug:              str
    category_hint:     Optional[str]  # events[0].ticker when available
    neg_risk:          bool = False   # part of a multi-outcome group
    group_item_title:  Optional[str] = None  # specific option label (e.g. "Spain")
    event_slug:        Optional[str] = None  # event group slug for correlation caps
    game_start_time:   Optional[datetime] = None  # sports only: tip/kickoff
    event_end_date:    Optional[datetime] = None  # events[0].endDate, deadline

    @property
    def days_to_end(self) -> float:
        return (self.end_date_iso - datetime.now(timezone.utc)).total_seconds() / 86400.0

    @property
    def resolution_at_estimate(self) -> datetime:
        """
        Best static guess of when this market will actually resolve.

        Order of preference:
          1. Sports markets: `gameStartTime + 3h` (covers game length +
             settlement lag). `endDate` on sports rows often equals tip
             time, which lies about how long the user is waiting.
          2. Events: `events[0].endDate` if it is later than `endDate`.
             The event-level deadline is usually the resolution deadline;
             a market whose `endDate` is earlier than its event deadline
             is a buffered trading-close, not a resolution time.
          3. Fallback: `endDate`. Used when no better signal exists.

        This is a deadline, not a guarantee. Deadline-style markets
        ("X by April 30") can resolve YES the moment the underlying
        event happens. The settler watches for actual closure and
        flips the position to settled when Polymarket marks the
        market closed.
        """
        if self.game_start_time is not None:
            return self.game_start_time + SPORTS_RESOLUTION_BUFFER
        if self.event_end_date is not None and self.event_end_date > self.end_date_iso:
            return self.event_end_date
        return self.end_date_iso


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
        # Gamma uses two slightly different shapes:
        #   '2026-04-27T01:30:00Z'         (proper ISO-8601)
        #   '2026-04-27 01:30:00+00'       (gameStartTime - space, not 'T')
        # Normalise both before fromisoformat.
        normalised = raw.replace("Z", "+00:00")
        if " " in normalised and "T" not in normalised:
            normalised = normalised.replace(" ", "T", 1)
        dt = datetime.fromisoformat(normalised)
        # Gamma sometimes returns naive ISO strings - force UTC so arithmetic
        # with tz-aware now() doesn't raise.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def extract_resolution_estimate(raw: dict) -> Optional[datetime]:
    """
    Best-guess "when does this market actually resolve" given a raw Gamma
    row. Module-level so the settler can refresh existing positions
    without reconstructing a full PolyMarket.

    Mirrors `PolyMarket.resolution_at_estimate`:
      1. sports: gameStartTime + 3h
      2. events[0].endDate when later than market-level endDate
      3. fallback: endDate / endDateIso
    Returns None when no parseable date is present.
    """
    gst = _parse_iso(raw.get("gameStartTime"))
    if gst is not None:
        return gst + SPORTS_RESOLUTION_BUFFER
    end = _parse_iso(raw.get("endDateIso") or raw.get("endDate"))
    events = raw.get("events") or []
    if isinstance(events, list) and events:
        evt_end = _parse_iso((events[0] or {}).get("endDate"))
        if evt_end is not None and (end is None or evt_end > end):
            return evt_end
    return end


def _as_market(m: dict) -> Optional[PolyMarket]:
    """Map a raw Gamma dict to a PolyMarket, or None if it isn't binary/parseable."""
    try:
        if m.get("negRiskOther"):
            return None
        prices   = _parse_price_list(m.get("outcomePrices"))
        outcomes = _parse_str_list(m.get("outcomes"))
        if len(prices) != 2 or len(outcomes) != 2:
            return None
        end_iso = _parse_iso(m.get("endDateIso") or m.get("endDate"))
        if end_iso is None:
            return None
        events = m.get("events") or []
        cat_hint = None
        event_slug = None
        evt_end = None
        if isinstance(events, list) and events:
            e0 = events[0] or {}
            cat_hint = e0.get("ticker") or e0.get("title") or None
            event_slug = e0.get("slug") or None
            evt_end = _parse_iso(e0.get("endDate"))
        game_start = _parse_iso(m.get("gameStartTime"))
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
            game_start_time = game_start,
            event_end_date = evt_end,
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
            self._session = aiohttp.ClientSession(
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
        scan_pages:       int   = 4,       # 4 × 100 = top 400 by 24h volume
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
                try:
                    row = await asyncio.wait_for(
                        self.fetch_market(mid), timeout=15,
                    )
                except (asyncio.TimeoutError, Exception):
                    return mid, None
                return mid, row
        pairs = await asyncio.gather(*(one(m) for m in market_ids))
        return {mid: row for mid, row in pairs if row is not None}
