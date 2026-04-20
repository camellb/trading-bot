"""
OKX WebSocket Manager — manages all OKX perpetual swap WebSocket connections.

=============================================================================
VERIFIED PAYLOAD FIELD NAMES (from live OKX v5 WebSocket, 2026-04-14)
=============================================================================

IMPORTANT: OKX uses TWO WebSocket base URLs for different channel types.
  Candle channels  → wss://ws.okx.com:8443/ws/v5/business  (NOT /public)
  Mark-price, tickers, books → wss://ws.okx.com:8443/ws/v5/public
  Candle channels on /public return error 60018 — verified.

KLINE STREAM (channel: "candle15m", "candle5m", "candle1m" on /business endpoint)
Subscription message:
  {"op": "subscribe", "args": [{"channel": "candle15m", "instId": "BTC-USDT-SWAP"}]}
Response message:
  {
    "arg": {"channel": "candle15m", "instId": "BTC-USDT-SWAP"},
    "data": [
      ["1776177900000", "75331.5", "75359.3", "75078.2", "75127.0",
       "118715.54", "1187.1554", "89277847.76424000", "0"]
    ]
  }
  data[0] array layout:
    [0]  ts           — open_time (ms, string)
    [1]  open         — open price (string)
    [2]  high         — high price (string)
    [3]  low          — low price (string)
    [4]  close        — close price (string)
    [5]  vol          — volume in contracts (string)
    [6]  volCcy       — volume in base asset (BTC) (string)
    [7]  volCcyQuote  — volume in quote asset (USDT) (string)
    [8]  confirm      — "0" = in-progress, "1" = confirmed/closed ← CLOSED FLAG

  CLOSED CANDLE FIELD: data[0][8] == "1"  (string comparison)
  Never use a candle for regime/signal generation unless confirm == "1".

MARK PRICE STREAM (channel: "mark-price" on /public endpoint)
  {
    "arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"},
    "data": [{"instId": "BTC-USDT-SWAP", "instType": "SWAP", "markPx": "75128.0", "ts": "1776178261867"}]
  }
  Fields: markPx (string), ts (ms string)
  MISSING vs Binance: NO fundingRate, NO indexPrice in this stream.
  → fundingRate and indexPrice come from REST poll (see _funding_rest_loop).

TICKERS STREAM (channel: "tickers" on /public endpoint)
  {
    "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
    "data": [{"instType": "SWAP", "instId": "BTC-USDT-SWAP",
              "last": "75107.5", "lastSz": "2",
              "askPx": "75107.5", "askSz": "325.67",
              "bidPx": "75107.4", "bidSz": "579.84",
              "ts": "1776178230165", ...}]
  }
  Fields used: bidPx, bidSz, askPx, askSz, last, ts
  MISSING vs Binance: NO fundingRate, NO openInterest, NO markPx, NO indexPx.

ORDER BOOK STREAM (channel: "books" on /public endpoint)
  action "snapshot":
    {
      "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"},
      "action": "snapshot",
      "data": [{
        "bids": [["75128.4", "460.15", "0", "23"], ...],
        "asks": [["75128.5", "362.27", "0", "33"], ...],
        "ts": "1776178262103",
        "checksum": 1151246030,
        "seqId": 311112998808,
        "prevSeqId": -1
      }]
    }
  action "update": same structure, partial deltas.
  Each level: [price_str, qty_str, deprecated_field, order_count]
  qty_str == "0" → remove level.
  Gap detection: update.prevSeqId must equal last processed seqId.
    If mismatch → re-subscribe and rebuild from next snapshot.
  Checksum: CRC32 of alternating bid/ask format (verified 2026-04-14):
    "bid0_px:bid0_qty:ask0_px:ask0_qty:bid1_px:bid1_qty:ask1_px:ask1_qty:..."
    (top 25 bids descending + top 25 asks ascending, INTERLEAVED, original strings)

REST FUNDING RATE (GET /api/v5/public/funding-rate — current, polled every 60s)
  data[0]: { fundingRate, fundingTime, nextFundingTime, premium, ts, ... }
  fundingRate  — current period rate (string)
  premium      — (markPrice - indexPrice) / indexPrice (string)
  index_price  = mark_price / (1 + premium)   ← derived, not a direct field

REST FUNDING RATE HISTORY (GET /api/v5/public/funding-rate-history)
  data[]: { fundingRate, fundingTime, instId, instType, realizedRate, ... }
  Used by regime_classifier._get_funding_percentile().

REST OI HISTORY (GET /api/v5/rubik/stat/contracts/open-interest-volume)
  params: ccy=BTC, period=5m  (15m NOT supported; supported: 5m, 1H, 1D)
  data[]: [ts_str, oi_usd_str, vol_usd_str]
  [0] = timestamp (ms, string), [1] = OI in USD (string), [2] = vol in USD (string)
  Used by regime_classifier._get_oi_delta().

REST KLINE BACKFILL (GET /api/v5/market/candles)
  params: instId=BTC-USDT-SWAP, bar=15m, limit=200
  data[]: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
  Returns NEWEST FIRST — must be reversed for chronological order.
  confirm="0" = in-progress, confirm="1" = closed.

=============================================================================
"""

import asyncio
import json
import sys
import time
import zlib
from collections import deque
from typing import Optional

import aiohttp
import websockets
import websockets.exceptions

import config
from feeds.feed_health_monitor import FeedHealthMonitor

# ── WebSocket base URLs ───────────────────────────────────────────────────────
# Candle channels require the /business endpoint (NOT /public — verified 2026-04-14)
_WS_PUBLIC_LIVE      = "wss://ws.okx.com:8443/ws/v5/public"
_WS_BUSINESS_LIVE    = "wss://ws.okx.com:8443/ws/v5/business"
_WS_PUBLIC_DEMO      = "wss://wspap.okx.com:8443/ws/v5/public"
_WS_BUSINESS_DEMO    = "wss://wspap.okx.com:8443/ws/v5/business"

# ── REST base URL ─────────────────────────────────────────────────────────────
_REST_BASE = "https://www.okx.com"

# ── Kline intervals to subscribe to per pair ─────────────────────────────────
_KLINE_INTERVALS = ["1m", "5m", "15m", "1H"]

# Max candles to keep per pair per interval
_MAX_CANDLES = 200

# Reconnect delay in seconds
_RECONNECT_DELAY_S = 2

# How often to poll REST for current funding rate + index price (seconds)
_FUNDING_POLL_INTERVAL_S = 60


def _ws_public() -> str:
    """Return correct public WS URL based on PAPER_MODE."""
    return _WS_PUBLIC_DEMO if config.PAPER_MODE else _WS_PUBLIC_LIVE


def _ws_business() -> str:
    """Return correct business WS URL based on PAPER_MODE."""
    return _WS_BUSINESS_DEMO if config.PAPER_MODE else _WS_BUSINESS_LIVE


def okx_id_to_ccxt(inst_id: str) -> str:
    """
    Convert OKX instrument ID to CCXT unified symbol.
    'BTC-USDT-SWAP' → 'BTC/USDT:USDT'
    Used when placing orders via CCXT.
    """
    parts = inst_id.split("-")   # ["BTC", "USDT", "SWAP"]
    base, quote = parts[0], parts[1]
    return f"{base}/{quote}:{quote}"


def _compute_ob_checksum(bids: dict, asks: dict) -> int:
    """
    Compute OKX order book checksum for validation.

    Algorithm (OKX v5 specification — verified 2026-04-14):
      1. Sort bids NUMERICALLY descending, asks NUMERICALLY ascending.
      2. Take top 25 of each.
      3. Interleave bid/ask ALTERNATELY:
           bid[0].price:bid[0].qty:ask[0].price:ask[0].qty:
           bid[1].price:bid[1].qty:ask[1].price:ask[1].qty:...
         NOT bids-then-asks. Verified by live snapshot comparison.
      4. CRC32 of the UTF-8 encoded string.
      5. Return as signed 32-bit integer.

    IMPORTANT: prices sorted NUMERICALLY (float key), not lexicographically.
    Lexicographic sort ("9.5" > "10.0") gives wrong order → wrong checksum.
    """
    top_bids = sorted(bids.items(), key=lambda kv: float(kv[0]), reverse=True)[:25]
    top_asks = sorted(asks.items(), key=lambda kv: float(kv[0]))[:25]

    # Alternate: bid0, ask0, bid1, ask1, ...
    parts: list[str] = []
    for b, a in zip(top_bids, top_asks):
        parts.append(f"{b[0]}:{b[1]}")
        parts.append(f"{a[0]}:{a[1]}")
    # Append remaining levels if bid/ask counts differ
    for b in top_bids[len(top_asks):]:
        parts.append(f"{b[0]}:{b[1]}")
    for a in top_asks[len(top_bids):]:
        parts.append(f"{a[0]}:{a[1]}")

    raw = zlib.crc32(":".join(parts).encode())
    return raw if raw < 2**31 else raw - 2**32


class OKXWebSocketManager:
    """
    Manages OKX perpetual swap WebSocket connections for kline, ticker
    (mark-price + tickers), and order book streams. Each connection runs
    in its own asyncio task with auto-reconnect.

    Public interface is identical to the former BinanceWebSocketManager:
      get_closed_candles(pair, interval) → list[dict]
      get_latest_ticker(pair)           → dict | None
      get_orderbook(pair)               → dict | None
      start(pairs)                      async
    """

    def __init__(self, health_monitor: FeedHealthMonitor) -> None:
        self._monitor = health_monitor
        self._monitor.register("kline")
        self._monitor.register("ticker")
        self._monitor.register("markprice")
        self._monitor.register("orderbook")

        # klines[pair][interval] = deque of candle dicts (max _MAX_CANDLES)
        self.klines: dict[str, dict[str, deque]] = {}
        # ticker[pair] = latest unified ticker dict
        self.ticker: dict[str, dict] = {}
        # orderbook[pair] = {"bids": {str_price→str_qty}, "asks": {...},
        #                    "seq_id": int, "ready": bool}
        self.orderbook: dict[str, dict] = {}

        # Internal stop flag
        self._stop = False
        # Per-pair ob rebuild lock
        self._ob_rebuilding: dict[str, bool] = {}

    # ── Reconnect helpers ─────────────────────────────────────────────────────

    async def _reconnect_sleep(self) -> None:
        """
        Sleep before a reconnect attempt.

        Normally waits _RECONNECT_DELAY_S (2s). If a mass-degradation event
        (network interruption) was detected within the last 10 seconds, skip
        the delay so all streams reconnect immediately in parallel rather than
        each waiting their own timer.
        """
        from datetime import datetime, timezone
        mass_ts = self._monitor.get_last_mass_degradation_ts()
        if mass_ts is not None:
            age_s = (datetime.now(timezone.utc) - mass_ts).total_seconds()
            if age_s <= 10.0:
                print("[okx_ws] mass reconnect — skipping delay", flush=True)
                return
        await asyncio.sleep(_RECONNECT_DELAY_S)

    # ── Public start ──────────────────────────────────────────────────────────

    async def start(self, pairs: list[str]) -> None:
        """
        Backfill kline history from REST, then start all WebSocket connections.

        OKX REST candle endpoint verified 2026-04-14:
          GET /api/v5/market/candles  →  list of arrays, NEWEST FIRST.
          array layout: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
          confirm="1" → closed candle; confirm="0" → in-progress.
          Response reversed to oldest-first before storing.
        """
        for pair in pairs:
            self.klines[pair] = {
                interval: deque(maxlen=_MAX_CANDLES)
                for interval in _KLINE_INTERVALS
            }
            self.ticker[pair] = {}
            self.orderbook[pair] = {
                "bids": {},
                "asks": {},
                "seq_id": 0,
                "ready": False,
            }
            self._ob_rebuilding[pair] = False

        # Backfill closed candles before opening streams
        for pair in pairs:
            for interval in _KLINE_INTERVALS:
                await self._backfill_klines(pair, interval)

        tasks = []
        # Candle streams — on /business endpoint, one task per pair per interval
        for pair in pairs:
            for interval in _KLINE_INTERVALS:
                tasks.append(asyncio.create_task(
                    self._kline_loop(pair, interval),
                    name=f"kline_{pair}_{interval}",
                ))
        # Mark-price stream — /public endpoint, all pairs on one connection
        tasks.append(asyncio.create_task(
            self._markprice_loop(pairs), name="markprice"
        ))
        # Tickers stream — /public endpoint, all pairs on one connection
        tasks.append(asyncio.create_task(
            self._tickers_loop(pairs), name="tickers"
        ))
        # Order book streams — /public endpoint, one task per pair
        for pair in pairs:
            tasks.append(asyncio.create_task(
                self._orderbook_loop(pair), name=f"orderbook_{pair}"
            ))
        # REST funding rate poll — updates ticker[pair]["funding_rate"] + "index_price"
        tasks.append(asyncio.create_task(
            self._funding_rest_loop(pairs), name="funding_rest"
        ))

        print(
            f"[okx_ws] Starting {len(tasks)} tasks for {pairs} "
            f"({'demo' if config.PAPER_MODE else 'live'})"
        )
        await asyncio.gather(*tasks)

    # ── REST kline backfill ───────────────────────────────────────────────────

    async def _backfill_klines(self, pair: str, interval: str) -> None:
        """
        Pre-seed kline buffer with REST historical data.

        OKX candle endpoint returns NEWEST FIRST. We reverse to oldest-first.
        The first element (index 0) after reversal is the oldest candle.
        In-progress candles have confirm="0"; all others are confirm="1" (closed).
        We load ALL entries (including the current in-progress candle), marking
        it as not closed so get_closed_candles() skips it.
        """
        url = f"{_REST_BASE}/api/v5/market/candles"
        okx_bar = f"{interval[:-1]}{'m' if interval.endswith('m') else interval[-1].upper()}"
        # OKX bar param: "1m", "5m", "15m" — same format as our _KLINE_INTERVALS
        params = {"instId": pair, "bar": interval, "limit": str(_MAX_CANDLES)}
        headers = {"User-Agent": "trading-bot/1.0"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        print(
                            f"[okx_ws] backfill {pair} {interval}: HTTP {resp.status}",
                            file=sys.stderr,
                        )
                        return
                    raw = await resp.json()

            entries = raw.get("data", [])
            if not entries:
                print(f"[okx_ws] backfill {pair} {interval}: empty data", file=sys.stderr)
                return

            # Reverse so oldest entry is first (OKX returns newest first)
            entries = list(reversed(entries))
            buf = self.klines[pair][interval]
            closed_count = 0
            for entry in entries:
                is_closed = entry[8] == "1"
                buf.append({
                    "open_time":  int(entry[0]),
                    "open":       float(entry[1]),
                    "high":       float(entry[2]),
                    "low":        float(entry[3]),
                    "close":      float(entry[4]),
                    "volume":     float(entry[6]),   # volCcy = base asset volume
                    "close_time": int(entry[0]) + _interval_ms(interval) - 1,
                    "closed":     is_closed,
                })
                if is_closed:
                    closed_count += 1

            print(
                f"[okx_ws] backfilled {closed_count} closed "
                f"{interval} candles for {pair}"
            )
        except Exception as exc:
            print(
                f"[okx_ws] backfill error {pair} {interval}: {exc}",
                file=sys.stderr,
            )

    # ── Kline stream (/business endpoint) ────────────────────────────────────

    async def _kline_loop(self, pair: str, interval: str) -> None:
        """
        Subscribe to candle channel on /business endpoint.
        Candle channel names: "candle1m", "candle5m", "candle15m" etc.
        Verified: these channels ONLY exist on /business, not /public.
        """
        channel = f"candle{interval}"
        first_connect = True
        while not self._stop:
            try:
                async with websockets.connect(
                    _ws_business(), ping_interval=20
                ) as ws:
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": [{"channel": channel, "instId": pair}],
                    }))
                    print(f"[okx_ws] kline subscribed: {channel} {pair}")

                    # ── Gap backfill on reconnect ─────────────────────────────
                    # Skip on the very first connect (startup backfill already ran).
                    if not first_connect:
                        buf = self.klines[pair][interval]
                        if buf:
                            last_close_time = buf[-1].get("close_time", 0)
                            import time as _t
                            now_ms = int(_t.time() * 1000)
                            gap_ms = now_ms - last_close_time
                            if gap_ms > _interval_ms(interval):
                                print(
                                    f"[okx_ws] gap detected {channel} {pair}: "
                                    f"{gap_ms / 1000:.0f}s — clearing and backfilling",
                                    flush=True,
                                )
                                buf.clear()
                                await self._backfill_klines(pair, interval)
                    first_connect = False

                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("event"):
                            if msg.get("event") == "error":
                                print(
                                    f"[okx_ws] kline sub error {channel}: "
                                    f"{msg.get('msg')}",
                                    file=sys.stderr,
                                )
                            continue
                        if msg.get("arg", {}).get("channel") == channel:
                            self._process_kline(pair, interval, msg)

            except websockets.exceptions.ConnectionClosed as exc:
                print(f"[okx_ws] kline {channel} {pair} closed: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[okx_ws] kline {channel} {pair} error: {exc}", file=sys.stderr)

            if self._stop:
                break
            self._monitor.report_reconnecting("kline")
            print(f"[okx_ws] kline {channel} {pair} reconnecting in {_RECONNECT_DELAY_S}s...")
            await self._reconnect_sleep()

    def _process_kline(self, pair: str, interval: str, msg: dict) -> None:
        """
        Parse an OKX candle WebSocket message and store it.

        VERIFIED: The closed-candle field is data[0][8] (string).
        "1" = confirmed/closed. "0" = in-progress.
        Only report_healthy("kline") on closed candles.
        """
        data = msg.get("data", [])
        if not data:
            return
        entry = data[0]
        is_closed = entry[8] == "1"
        candle = {
            "open_time": int(entry[0]),
            "open":      float(entry[1]),
            "high":      float(entry[2]),
            "low":       float(entry[3]),
            "close":     float(entry[4]),
            "volume":    float(entry[6]),   # volCcy = base asset volume (BTC/ETH)
            "close_time": int(entry[0]) + _interval_ms(interval) - 1,
            "closed":    is_closed,         # data[0][8] == "1" is the verified closed flag
        }

        buf = self.klines[pair][interval]
        if buf and buf[-1]["open_time"] == candle["open_time"]:
            buf[-1] = candle   # update in-progress candle
        else:
            buf.append(candle)

        if is_closed:
            self._monitor.report_healthy("kline")

    # ── Mark-price stream (/public endpoint) ──────────────────────────────────

    async def _markprice_loop(self, pairs: list[str]) -> None:
        """
        Subscribe to mark-price channel for all pairs on /public endpoint.
        Updates self.ticker[pair]["mark_price"].
        fundingRate and indexPrice are NOT present in this stream;
        they are populated by _funding_rest_loop().
        """
        args = [{"channel": "mark-price", "instId": p} for p in pairs]
        while not self._stop:
            try:
                async with websockets.connect(
                    _ws_public(), ping_interval=20
                ) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    print("[okx_ws] mark-price subscribed")

                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("event"):
                            continue
                        if msg.get("arg", {}).get("channel") != "mark-price":
                            continue
                        for entry in msg.get("data", []):
                            inst = entry.get("instId", "")
                            if inst not in self.ticker:
                                continue
                            self.ticker[inst].update({
                                "mark_price": float(entry["markPx"]),
                                "timestamp":  int(entry["ts"]),
                            })
                            self._monitor.report_healthy("markprice")

            except websockets.exceptions.ConnectionClosed as exc:
                print(f"[okx_ws] mark-price closed: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[okx_ws] mark-price error: {exc}", file=sys.stderr)

            if self._stop:
                break
            self._monitor.report_reconnecting("markprice")
            print(f"[okx_ws] mark-price reconnecting in {_RECONNECT_DELAY_S}s...")
            await self._reconnect_sleep()

    # ── Tickers stream (/public endpoint) ─────────────────────────────────────

    async def _tickers_loop(self, pairs: list[str]) -> None:
        """
        Subscribe to tickers channel for all pairs on /public endpoint.
        Provides bid/ask spread and last price.
        Used by execution_filter (spread check) and get_latest_ticker().
        fundingRate is NOT present in this stream.
        """
        args = [{"channel": "tickers", "instId": p} for p in pairs]
        while not self._stop:
            try:
                async with websockets.connect(
                    _ws_public(), ping_interval=20
                ) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    print("[okx_ws] tickers subscribed")

                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("event"):
                            continue
                        if msg.get("arg", {}).get("channel") != "tickers":
                            continue
                        for entry in msg.get("data", []):
                            inst = entry.get("instId", "")
                            if inst not in self.ticker:
                                continue
                            self.ticker[inst].update({
                                "bid":       float(entry["bidPx"]),
                                "bid_size":  float(entry["bidSz"]),
                                "ask":       float(entry["askPx"]),
                                "ask_size":  float(entry["askSz"]),
                                "last":      float(entry["last"]),
                                "timestamp": int(entry["ts"]),
                            })
                            self._monitor.report_healthy("ticker")

            except websockets.exceptions.ConnectionClosed as exc:
                print(f"[okx_ws] tickers closed: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[okx_ws] tickers error: {exc}", file=sys.stderr)

            if self._stop:
                break
            self._monitor.report_reconnecting("ticker")
            print(f"[okx_ws] tickers reconnecting in {_RECONNECT_DELAY_S}s...")
            await self._reconnect_sleep()

    # ── REST funding rate poll ─────────────────────────────────────────────────

    async def _funding_rest_loop(self, pairs: list[str]) -> None:
        """
        Poll GET /api/v5/public/funding-rate every 60s to get:
          - current funding rate  → ticker[pair]["funding_rate"]
          - index price (derived) → ticker[pair]["index_price"]
          - next funding time     → ticker[pair]["next_funding_time"]

        OKX mark-price and tickers WS streams do NOT include funding rate or
        index price (verified 2026-04-14). This REST poll fills that gap.

        index_price derivation:
          premium = (markPrice - indexPrice) / indexPrice
          → indexPrice = markPrice / (1 + premium)
        """
        headers = {"User-Agent": "trading-bot/1.0"}
        while not self._stop:
            for pair in pairs:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{_REST_BASE}/api/v5/public/funding-rate",
                            params={"instId": pair},
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    entry = (data.get("data") or [None])[0]
                    if not entry:
                        continue

                    funding_rate = float(entry["fundingRate"])
                    premium = float(entry.get("premium", "0") or "0")
                    mark_price = self.ticker.get(pair, {}).get("mark_price")
                    index_price = None
                    if mark_price and (1.0 + premium) != 0:
                        index_price = mark_price / (1.0 + premium)

                    self.ticker[pair].update({
                        "funding_rate":      funding_rate,
                        "index_price":       index_price,
                        "next_funding_time": int(entry.get("nextFundingTime", 0)),
                    })
                except Exception as exc:
                    print(
                        f"[okx_ws] funding REST poll error {pair}: {exc}",
                        file=sys.stderr,
                    )

            await asyncio.sleep(_FUNDING_POLL_INTERVAL_S)

    # ── Order book stream (/public endpoint) ──────────────────────────────────

    async def _orderbook_loop(self, pair: str) -> None:
        """
        Maintain a local order book using OKX "books" channel (400 levels).

        OKX order book protocol (verified 2026-04-14):
          - action "snapshot": rebuild book from scratch.
          - action "update": apply deltas.
          - Each level: [price_str, qty_str, deprecated, order_count]
            qty_str == "0" → remove level.
          - Gap detection: update.prevSeqId must equal last processed seqId.
            Mismatch → re-subscribe and rebuild from next snapshot.
          - Checksum: CRC32 of top-25 bids+asks concatenated as "p:q:p:q:..."
            On checksum mismatch → log and rebuild.
          - Prices stored as strings to enable exact checksum computation.
        """
        while not self._stop:
            try:
                self._ob_rebuilding[pair] = True
                self.orderbook[pair]["ready"] = False
                self._monitor.report_reconnecting("orderbook")

                async with websockets.connect(
                    _ws_public(), ping_interval=20
                ) as ws:
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": [{"channel": "books", "instId": pair}],
                    }))
                    print(f"[okx_ws] orderbook subscribed: {pair}")

                    bids: dict[str, str] = {}
                    asks: dict[str, str] = {}
                    last_seq_id: int = -1

                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("event"):
                            continue

                        action = msg.get("action", "")
                        data_list = msg.get("data", [])
                        if not data_list:
                            continue
                        d = data_list[0]
                        seq_id = int(d.get("seqId", -1))
                        prev_seq_id = int(d.get("prevSeqId", -1))

                        if action == "snapshot":
                            bids = {e[0]: e[1] for e in d.get("bids", [])}
                            asks = {e[0]: e[1] for e in d.get("asks", [])}
                            last_seq_id = seq_id
                            # Validate snapshot checksum
                            expected = int(d.get("checksum", 0))
                            got = _compute_ob_checksum(bids, asks)
                            if got != expected:
                                print(
                                    f"[okx_ws] orderbook snapshot checksum mismatch "
                                    f"for {pair}: got={got} expected={expected}",
                                    file=sys.stderr,
                                )
                                # Rebuild from next snapshot
                                break
                            self.orderbook[pair] = {
                                "bids": bids,
                                "asks": asks,
                                "seq_id": last_seq_id,
                                "ready": True,
                            }
                            self._ob_rebuilding[pair] = False
                            self._monitor.report_healthy("orderbook")
                            print(
                                f"[okx_ws] orderbook ready for {pair} "
                                f"(seqId={last_seq_id})"
                            )

                        elif action == "update":
                            if last_seq_id == -1:
                                # Snapshot not yet received — discard
                                continue
                            # Gap detection: prevSeqId must equal our last seqId
                            if prev_seq_id != last_seq_id:
                                print(
                                    f"[okx_ws] orderbook gap for {pair}: "
                                    f"prevSeqId={prev_seq_id} != "
                                    f"last_seq_id={last_seq_id}",
                                    file=sys.stderr,
                                )
                                self._monitor.report_reconnecting("orderbook")
                                break  # Force rebuild on reconnect

                            # Apply delta
                            bids, asks = _apply_ob_delta(bids, asks, d)
                            last_seq_id = seq_id

                            # Validate checksum on every update
                            expected = int(d.get("checksum", 0))
                            got = _compute_ob_checksum(bids, asks)
                            if got != expected:
                                print(
                                    f"[okx_ws] orderbook update checksum mismatch "
                                    f"for {pair}: got={got} expected={expected}",
                                    file=sys.stderr,
                                )
                                self._monitor.report_reconnecting("orderbook")
                                break

                            self.orderbook[pair]["bids"] = bids
                            self.orderbook[pair]["asks"] = asks
                            self.orderbook[pair]["seq_id"] = last_seq_id
                            self._monitor.report_healthy("orderbook")

            except websockets.exceptions.ConnectionClosed as exc:
                print(f"[okx_ws] orderbook {pair} closed: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[okx_ws] orderbook {pair} error: {exc}", file=sys.stderr)

            if self._stop:
                break
            self._monitor.report_reconnecting("orderbook")
            self.orderbook[pair]["ready"] = False
            print(f"[okx_ws] orderbook {pair} reconnecting in {_RECONNECT_DELAY_S}s...")
            await self._reconnect_sleep()

    # ── Public accessors ──────────────────────────────────────────────────────

    def get_closed_candles(self, pair: str, interval: str) -> list[dict]:
        """
        Return only confirmed closed candles for the given pair and interval.
        NEVER returns in-progress candles (confirm must be "1" → closed=True).
        Used by regime classifier — must be strictly closed-bar data only.
        """
        buf = self.klines.get(pair, {}).get(interval, deque())
        return [c for c in buf if c["closed"]]

    def get_latest_ticker(self, pair: str) -> dict | None:
        """
        Return latest unified ticker dict for pair, or None if unavailable.
        Keys: mark_price, index_price, funding_rate, bid, ask, last, timestamp,
              next_funding_time.
        mark_price populated by mark-price WS stream.
        funding_rate and index_price populated by REST poll (may lag up to 60s).
        bid/ask populated by tickers WS stream.
        """
        data = self.ticker.get(pair)
        return data if data else None

    def get_orderbook(self, pair: str) -> dict | None:
        """
        Return current order book snapshot or None if book is rebuilding.
        Returned dict has float-keyed bids/asks for compatibility with
        execution_filter and book_manager.
        Layer D must check for None and block if book is not ready.
        """
        ob = self.orderbook.get(pair)
        if not ob or not ob.get("ready"):
            return None
        # Convert string-price dict to float-price dict for engine compatibility
        return {
            "bids": {float(p): float(q) for p, q in ob["bids"].items()},
            "asks": {float(p): float(q) for p, q in ob["asks"].items()},
            "seq_id": ob["seq_id"],
            "ready":  True,
        }

    def force_disconnect(self, feed: str = "orderbook") -> None:
        """
        Test helper: force the orderbook to rebuild by clearing ready flag.
        """
        for pair in self.orderbook:
            if feed == "orderbook":
                self.orderbook[pair]["ready"] = False
                self._monitor.report_reconnecting("orderbook")


# ── Module-level helpers ───────────────────────────────────────────────────────

def _interval_ms(interval: str) -> int:
    """Convert interval string to milliseconds. '15m' → 900000."""
    units = {"m": 60_000, "H": 3_600_000, "D": 86_400_000}
    return int(interval[:-1]) * units[interval[-1]]


def _apply_ob_delta(
    bids: dict[str, str], asks: dict[str, str], delta: dict
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Apply an OKX order book delta update.
    Each level: [price_str, qty_str, deprecated, order_count]
    qty_str == "0" → remove level.
    """
    for level in delta.get("bids", []):
        price, qty = level[0], level[1]
        if qty == "0":
            bids.pop(price, None)
        else:
            bids[price] = qty

    for level in delta.get("asks", []):
        price, qty = level[0], level[1]
        if qty == "0":
            asks.pop(price, None)
        else:
            asks[price] = qty

    return bids, asks
