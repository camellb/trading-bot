"""
Live crypto market data adapter - OKX via CCXT async.

Public endpoints only. Used by research.fetcher to inject real-time
price/order-flow context into short-horizon crypto markets so the
evaluator has ground truth rather than reasoning from stale news.

Module-level cache with 15-second TTL per symbol.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    import ccxt.async_support as ccxt_async
    _CCXT_AVAILABLE = True
except ImportError:
    _CCXT_AVAILABLE = False


log = logging.getLogger(__name__)


# ── Symbol mapping ──────────────────────────────────────────────────────────
# Keys are uppercased free-text symbols; values are CCXT spot symbols on OKX.
SYMBOL_MAP: dict[str, str] = {
    "BITCOIN":  "BTC/USDT",
    "BTC":      "BTC/USDT",
    "ETHEREUM": "ETH/USDT",
    "ETH":      "ETH/USDT",
    "SOLANA":   "SOL/USDT",
    "SOL":      "SOL/USDT",
}

# Cache TTL - short so context stays fresh for direction calls.
_CACHE_TTL_SECONDS = 15.0


@dataclass
class LiveCryptoContext:
    symbol:               str
    current_price:        float
    price_24h_ago:        Optional[float]
    change_24h_pct:       Optional[float]
    price_1h_ago:         Optional[float]
    change_1h_pct:        Optional[float]
    price_15m_ago:        Optional[float]
    change_15m_pct:       Optional[float]
    recent_ohlcv_15m:     list[tuple]   = field(default_factory=list)  # [(ts, o, h, l, c, v), ...]
    order_book_imbalance: Optional[float] = None                       # bid_depth / ask_depth
    spread_bps:           Optional[float] = None
    funding_rate:         Optional[float] = None
    volume_24h:           Optional[float] = None
    fetched_at:           datetime      = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def freshness_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds()

    def to_prompt_block(self) -> str:
        lines = [f"Symbol: {self.symbol}", f"Price: ${self.current_price:,.2f}"]
        if self.change_24h_pct is not None:
            lines.append(f"24h change: {self.change_24h_pct:+.2f}% (from ${self.price_24h_ago:,.2f})")
        if self.change_1h_pct is not None:
            lines.append(f"1h change: {self.change_1h_pct:+.2f}% (from ${self.price_1h_ago:,.2f})")
        if self.change_15m_pct is not None:
            lines.append(f"15m change: {self.change_15m_pct:+.2f}%")
        if self.spread_bps is not None:
            lines.append(f"Spread: {self.spread_bps:.1f}bps")
        if self.order_book_imbalance is not None:
            lines.append(f"Order book top-5 bid/ask depth ratio: {self.order_book_imbalance:.2f}")
        if self.funding_rate is not None:
            lines.append(f"Perp funding rate: {self.funding_rate*100:+.4f}%")
        if self.volume_24h is not None:
            lines.append(f"24h volume: {self.volume_24h:,.0f}")
        if self.recent_ohlcv_15m:
            tail = self.recent_ohlcv_15m[-4:]
            candle_strs = [
                f"O:{o:.2f} H:{h:.2f} L:{l:.2f} C:{c:.2f}"
                for (_ts, o, h, l, c, _v) in tail
            ]
            lines.append("Last 4×15m candles: " + " | ".join(candle_strs))
        lines.append(f"Fetched {self.freshness_seconds:.0f}s ago")
        return "\n".join(lines)


# ── Cache ────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, LiveCryptoContext]] = {}
_cache_lock = asyncio.Lock()


def _now_monotonic() -> float:
    try:
        return asyncio.get_event_loop().time()
    except RuntimeError:
        import time
        return time.monotonic()


def _cache_get(symbol: str) -> Optional[LiveCryptoContext]:
    entry = _cache.get(symbol)
    if not entry:
        return None
    ts, ctx = entry
    if (_now_monotonic() - ts) > _CACHE_TTL_SECONDS:
        return None
    return ctx


def _cache_put(symbol: str, ctx: LiveCryptoContext) -> None:
    _cache[symbol] = (_now_monotonic(), ctx)


# ── Main entrypoint ─────────────────────────────────────────────────────────
def resolve_symbol(free_text: str) -> Optional[str]:
    """Map a free-text token (e.g. 'Bitcoin', 'BTC') to a CCXT spot symbol."""
    if not free_text:
        return None
    return SYMBOL_MAP.get(free_text.strip().upper())


async def get_context(symbol: str) -> Optional[LiveCryptoContext]:
    """
    Fetch live market context for a CCXT spot symbol (e.g. 'BTC/USDT').

    Returns None on any failure. Cached for 15 seconds per symbol.
    """
    if not _CCXT_AVAILABLE:
        return None
    if not symbol:
        return None

    async with _cache_lock:
        cached = _cache_get(symbol)
        if cached:
            return cached

    client = ccxt_async.okx({"enableRateLimit": True})
    try:
        ticker_task = asyncio.create_task(client.fetch_ticker(symbol))
        ohlcv_task  = asyncio.create_task(client.fetch_ohlcv(symbol, timeframe="15m", limit=100))
        book_task   = asyncio.create_task(client.fetch_order_book(symbol, limit=5))

        ticker = await ticker_task
        ohlcv  = await ohlcv_task
        book   = await book_task

        current_price = float(ticker.get("last") or ticker.get("close") or 0.0)
        if current_price <= 0:
            return None

        # Reference prices from 15m candles (each candle = 15 minutes).
        # ohlcv[-1] is the in-progress candle; ohlcv[-2] is the last closed.
        def _close_at_offset(bars_back: int) -> Optional[float]:
            idx = -1 - bars_back
            if abs(idx) > len(ohlcv):
                return None
            try:
                return float(ohlcv[idx][4])
            except (IndexError, ValueError, TypeError):
                return None

        price_15m_ago = _close_at_offset(1)   # 1 bar back  ≈ 15m
        price_1h_ago  = _close_at_offset(4)   # 4 bars back = 1h
        price_24h_ago = _close_at_offset(96)  # 96 bars back = 24h

        def _pct(old: Optional[float]) -> Optional[float]:
            if old is None or old <= 0:
                return None
            return (current_price - old) / old * 100.0

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        # OKX bid/ask entries may have extra fields (count, orders) beyond [price, qty].
        bid_depth = sum(float(row[1]) for row in bids[:5]) if bids else 0.0
        ask_depth = sum(float(row[1]) for row in asks[:5]) if asks else 0.0
        imbalance = (bid_depth / ask_depth) if ask_depth > 0 else None

        spread_bps = None
        if bids and asks:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            if best_bid > 0 and best_ask > 0:
                mid = (best_bid + best_ask) / 2.0
                spread_bps = (best_ask - best_bid) / mid * 10_000.0

        funding_rate: Optional[float] = None
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        try:
            fr = await client.fetch_funding_rate(swap_symbol)
            if fr and fr.get("fundingRate") is not None:
                funding_rate = float(fr["fundingRate"])
        except Exception:
            funding_rate = None

        ohlcv_tuples = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                         float(r[4]), float(r[5])) for r in ohlcv[-5:-1]]

        ctx = LiveCryptoContext(
            symbol               = symbol,
            current_price        = current_price,
            price_24h_ago        = price_24h_ago,
            change_24h_pct       = _pct(price_24h_ago),
            price_1h_ago         = price_1h_ago,
            change_1h_pct        = _pct(price_1h_ago),
            price_15m_ago        = price_15m_ago,
            change_15m_pct       = _pct(price_15m_ago),
            recent_ohlcv_15m     = ohlcv_tuples,
            order_book_imbalance = imbalance,
            spread_bps           = spread_bps,
            funding_rate         = funding_rate,
            volume_24h           = float(ticker.get("quoteVolume") or ticker.get("baseVolume") or 0.0) or None,
            fetched_at           = datetime.now(timezone.utc),
        )

        async with _cache_lock:
            _cache_put(symbol, ctx)
        return ctx

    except Exception as exc:
        log.warning("live_crypto OKX fetch failed for %s: %s", symbol, exc)
        return None
    finally:
        try:
            await client.close()
        except Exception:
            pass
