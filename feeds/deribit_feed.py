"""
Deribit DVOL Feed — fetches implied volatility index data for BTC and ETH.

Deribit is the world's largest crypto options exchange. Their DVOL index
reflects market-expected volatility — a leading indicator for regime changes.

Endpoint: GET https://www.deribit.com/api/v2/public/get_volatility_index_data
  Params:
    currency        BTC | ETH
    start_timestamp ms epoch (1 hour ago)
    end_timestamp   ms epoch (now)
    resolution      3600   (1-hour bars)
  Response: result.data = [[timestamp_ms, open, high, low, close], ...]
            latest close = current DVOL

Behaviour:
  - Caches results per currency; re-fetches at most every DERIBIT_IV_CACHE_SECONDS.
  - SOL has no DVOL; BTC DVOL is used as proxy.
  - History is trimmed to the last 24 entries (24h of hourly bars).
  - Spike = latest close is >= DERIBIT_IV_SPIKE_PCT higher than prior close.
  - High IV  (>= DERIBIT_IV_HIGH_THRESHOLD)    → 70% size multiplier.
  - Extreme IV (>= DERIBIT_IV_EXTREME_THRESHOLD) → 35% size multiplier.
  - On fetch error / unavailable → returns None (treated as normal / no override).
"""

import sys
import time
from typing import Optional

import aiohttp

import config
from feeds.feed_health_monitor import FeedHealthMonitor


class DeribitFeed:
    """Async IV-data client for Deribit DVOL index."""

    def __init__(self, health_monitor: FeedHealthMonitor) -> None:
        self._monitor = health_monitor
        self._monitor.register("deribit")

        # Cache: currency ("BTC"|"ETH") → {"data": [...], "fetched_at": float}
        self._cache: dict[str, dict] = {}

    # ── Public helpers ────────────────────────────────────────────────────────

    async def get_btc_iv(self) -> Optional[float]:
        """Return current BTC DVOL (latest hourly close), or None on error."""
        return await self._get_current_iv("BTC")

    async def get_eth_iv(self) -> Optional[float]:
        """Return current ETH DVOL (latest hourly close), or None on error."""
        return await self._get_current_iv("ETH")

    async def get_iv_for_pair(self, pair: str) -> Optional[float]:
        """
        Return the DVOL value for the given trading pair.
        SOL uses BTC DVOL as proxy (Deribit has no SOL DVOL).
        """
        ccy = self._pair_to_dvol_ccy(pair)
        return await self._get_current_iv(ccy)

    async def is_high_iv(self, pair: str) -> bool:
        """True when DVOL >= DERIBIT_IV_HIGH_THRESHOLD."""
        iv = await self.get_iv_for_pair(pair)
        if iv is None:
            return False
        return iv >= config.DERIBIT_IV_HIGH_THRESHOLD

    async def is_extreme_iv(self, pair: str) -> bool:
        """True when DVOL >= DERIBIT_IV_EXTREME_THRESHOLD."""
        iv = await self.get_iv_for_pair(pair)
        if iv is None:
            return False
        return iv >= config.DERIBIT_IV_EXTREME_THRESHOLD

    async def detect_iv_spike(self, pair: str) -> bool:
        """
        True when the latest hourly DVOL close is >= DERIBIT_IV_SPIKE_PCT
        (20% by default) above the previous hourly close.
        Requires at least 2 history entries; returns False otherwise.
        """
        ccy = self._pair_to_dvol_ccy(pair)
        history = await self._get_history(ccy)
        if not history or len(history) < 2:
            return False
        curr = float(history[-1][4])   # latest close
        prev = float(history[-2][4])   # prior close
        if prev <= 0:
            return False
        return (curr - prev) / prev >= config.DERIBIT_IV_SPIKE_PCT

    async def get_iv_size_multiplier(self, pair: str) -> float:
        """
        Return the size multiplier based on current IV:
          extreme IV → DERIBIT_IV_EXTREME_MULTIPLIER (0.35)
          high IV    → DERIBIT_IV_SIZE_MULTIPLIER    (0.70)
          normal     → 1.0
        """
        iv = await self.get_iv_for_pair(pair)
        if iv is None:
            return 1.0
        if iv >= config.DERIBIT_IV_EXTREME_THRESHOLD:
            return config.DERIBIT_IV_EXTREME_MULTIPLIER
        if iv >= config.DERIBIT_IV_HIGH_THRESHOLD:
            return config.DERIBIT_IV_SIZE_MULTIPLIER
        return 1.0

    # ── Internal fetching / caching ───────────────────────────────────────────

    @staticmethod
    def _pair_to_dvol_ccy(pair: str) -> str:
        """
        Map trading pair to DVOL currency.
        SOL-USDT-SWAP and SOL/USDT:USDT → "BTC" (proxy).
        All others: extract base currency.
        """
        upper = pair.upper()
        if "SOL" in upper:
            return "BTC"
        if "ETH" in upper:
            return "ETH"
        return "BTC"

    async def _get_current_iv(self, ccy: str) -> Optional[float]:
        """Return the latest hourly DVOL close for a currency."""
        history = await self._get_history(ccy)
        if not history:
            return None
        return float(history[-1][4])  # close price is index 4

    async def _get_history(self, ccy: str) -> Optional[list]:
        """
        Return the cached (or freshly fetched) DVOL history for the currency.
        History is a list of [timestamp_ms, open, high, low, close] entries,
        trimmed to the last 24 entries (24h).
        Returns None on fetch failure.
        """
        now = time.monotonic()
        cached = self._cache.get(ccy)
        if cached and (now - cached["fetched_at"]) < config.DERIBIT_IV_CACHE_SECONDS:
            return cached["data"]

        # Re-fetch
        data = await self._fetch_dvol(ccy)
        if data is not None:
            self._cache[ccy] = {"data": data, "fetched_at": now}
            self._monitor.report_healthy("deribit")
        else:
            self._monitor.report_degraded("deribit", f"DVOL fetch failed for {ccy}")

        return data

    async def _fetch_dvol(self, ccy: str) -> Optional[list]:
        """
        Fetch 24 hours of hourly DVOL bars from Deribit.
        Returns list of [ts_ms, open, high, low, close], most recent last.
        Returns None on any error.
        """
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 24 * 3600 * 1000  # 24 hours ago

        url = f"{config.DERIBIT_BASE_URL}/public/get_volatility_index_data"
        params = {
            "currency": ccy,
            "start_timestamp": start_ms,
            "end_timestamp": now_ms,
            "resolution": 3600,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "trading-bot/1.0"},
                ) as resp:
                    if resp.status != 200:
                        print(
                            f"[deribit] HTTP {resp.status} for {ccy} DVOL",
                            file=sys.stderr,
                        )
                        return None
                    payload = await resp.json()
                    result = payload.get("result", {})
                    data = result.get("data", [])
                    if not data:
                        print(
                            f"[deribit] Empty data for {ccy} DVOL",
                            file=sys.stderr,
                        )
                        return None
                    # Trim to last 24 entries; data is oldest-first from Deribit
                    trimmed = data[-24:]
                    print(
                        f"[deribit] {ccy} DVOL: {len(trimmed)} bars, "
                        f"latest={float(trimmed[-1][4]):.1f}"
                    )
                    return trimmed
        except Exception as exc:
            print(f"[deribit] fetch error for {ccy}: {exc}", file=sys.stderr)
            return None
