"""
Live equity market data adapter — yfinance (Yahoo Finance).

Used by research.fetcher to inject real-time index/ETF prices into
short-horizon equity-direction markets. yfinance is synchronous, so
calls are offloaded to a thread via asyncio.to_thread.

Module-level cache with 60-second TTL per ticker.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False


log = logging.getLogger(__name__)


# ── Ticker mapping ──────────────────────────────────────────────────────────
# Keys are uppercased free-text tokens; values are Yahoo Finance tickers.
TICKER_MAP: dict[str, str] = {
    "SPX":    "^GSPC",
    "S&P":    "^GSPC",
    "S&P500": "^GSPC",
    "SP500":  "^GSPC",
    "SPY":    "SPY",
    "NASDAQ": "^NDX",
    "NDX":    "^NDX",
    "QQQ":    "QQQ",
    "DOW":    "^DJI",
    "DJI":    "^DJI",
    "DJIA":   "^DJI",
}

_CACHE_TTL_SECONDS = 60.0


@dataclass
class LiveEquityContext:
    ticker:             str
    current_price:      float
    previous_close:     Optional[float]
    change_today_pct:   Optional[float]
    day_high:           Optional[float]
    day_low:            Optional[float]
    intraday_range_pct: Optional[float]
    volume:             Optional[float]
    avg_volume:         Optional[float]
    market_state:       Optional[str]    # REGULAR, CLOSED, PRE, POST
    fetched_at:         datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def freshness_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds()

    def to_prompt_block(self) -> str:
        lines = [f"Ticker: {self.ticker}", f"Price: {self.current_price:,.2f}"]
        if self.change_today_pct is not None and self.previous_close is not None:
            lines.append(f"Change today: {self.change_today_pct:+.2f}% (prev close {self.previous_close:,.2f})")
        if self.day_low is not None and self.day_high is not None:
            lines.append(f"Day range: {self.day_low:,.2f} — {self.day_high:,.2f}")
        if self.intraday_range_pct is not None:
            lines.append(f"Intraday range: {self.intraday_range_pct:.2f}%")
        if self.market_state:
            lines.append(f"Market state: {self.market_state}")
        if self.volume is not None:
            lines.append(f"Volume: {self.volume:,.0f}")
            if self.avg_volume and self.avg_volume > 0:
                lines.append(f"Volume vs avg: {self.volume / self.avg_volume:.2f}×")
        lines.append(f"Fetched {self.freshness_seconds:.0f}s ago")
        return "\n".join(lines)


# ── Cache ────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, LiveEquityContext]] = {}
_cache_lock = asyncio.Lock()


def _now_monotonic() -> float:
    try:
        return asyncio.get_event_loop().time()
    except RuntimeError:
        import time
        return time.monotonic()


def _cache_get(ticker: str) -> Optional[LiveEquityContext]:
    entry = _cache.get(ticker)
    if not entry:
        return None
    ts, ctx = entry
    if (_now_monotonic() - ts) > _CACHE_TTL_SECONDS:
        return None
    return ctx


def _cache_put(ticker: str, ctx: LiveEquityContext) -> None:
    _cache[ticker] = (_now_monotonic(), ctx)


# ── Main entrypoint ─────────────────────────────────────────────────────────
def resolve_ticker(free_text: str) -> Optional[str]:
    """Map a free-text token (e.g. 'Nasdaq', 'SPY') to a Yahoo ticker."""
    if not free_text:
        return None
    return TICKER_MAP.get(free_text.strip().upper())


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _fetch_sync(ticker: str) -> Optional[LiveEquityContext]:
    """Blocking yfinance call — run under asyncio.to_thread."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info  # much faster than .info

        current_price = _safe_float(info.get("last_price") or info.get("lastPrice"))
        if not current_price or current_price <= 0:
            hist = t.history(period="1d", interval="1m")
            if hist is None or hist.empty:
                return None
            current_price = float(hist["Close"].iloc[-1])

        previous_close = _safe_float(info.get("previous_close") or info.get("previousClose"))
        day_high       = _safe_float(info.get("day_high")       or info.get("dayHigh"))
        day_low        = _safe_float(info.get("day_low")        or info.get("dayLow"))
        volume         = _safe_float(info.get("last_volume")    or info.get("lastVolume"))
        avg_volume     = _safe_float(info.get("ten_day_average_volume")
                                      or info.get("three_month_average_volume"))
        market_state   = info.get("market_state") or info.get("marketState")

        change_today_pct = None
        if previous_close and previous_close > 0:
            change_today_pct = (current_price - previous_close) / previous_close * 100.0

        intraday_range_pct = None
        if day_high and day_low and day_low > 0:
            intraday_range_pct = (day_high - day_low) / day_low * 100.0

        return LiveEquityContext(
            ticker             = ticker,
            current_price      = current_price,
            previous_close     = previous_close,
            change_today_pct   = change_today_pct,
            day_high           = day_high,
            day_low            = day_low,
            intraday_range_pct = intraday_range_pct,
            volume             = volume,
            avg_volume         = avg_volume,
            market_state       = market_state if isinstance(market_state, str) else None,
            fetched_at         = datetime.now(timezone.utc),
        )
    except Exception as exc:
        log.warning("live_equity yfinance fetch failed for %s: %s", ticker, exc)
        return None


async def get_context(ticker: str) -> Optional[LiveEquityContext]:
    """
    Fetch live equity context for a Yahoo ticker (e.g. '^GSPC', 'SPY').

    Returns None on any failure. Cached for 60 seconds per ticker.
    """
    if not _YF_AVAILABLE:
        return None
    if not ticker:
        return None

    async with _cache_lock:
        cached = _cache_get(ticker)
        if cached:
            return cached

    ctx = await asyncio.to_thread(_fetch_sync, ticker)
    if ctx is None:
        return None

    async with _cache_lock:
        _cache_put(ticker, ctx)
    return ctx
