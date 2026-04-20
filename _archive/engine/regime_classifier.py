"""
Layer A — Regime Classifier.

Runs on confirmed closed 15m bars only. Never uses in-progress candle data.
Outputs one of 8 regime states. Degrades to NO_TRADE if any core feed is unhealthy.

=============================================================================
VERIFIED FIELD NAMES — OKX (updated 2026-04-14, OKX migration)
=============================================================================

CLOSED CANDLE FIELD (OKX kline WebSocket, candle channel on /business endpoint):
  data[0][8] == "1"  →  candle is confirmed closed
  Access via: ws_manager.get_closed_candles(pair, "15m")
  This method filters strictly — only returns candles where closed=True.

FUNDING RATE HISTORY  GET https://www.okx.com/api/v5/public/funding-rate-history
  Parameters: instId (e.g. "BTC-USDT-SWAP"), limit=200
  Response: {"code": "0", "data": [{
    "instId":       "BTC-USDT-SWAP",
    "fundingTime":  "1776153600000",    ← ms timestamp as STRING
    "fundingRate":  "0.0000843247781537",  ← rate as string
    "realizedRate": "0.0000843247781537",
    "formulaType":  "withRate",
    "method":       "current_period"
  }]}
  Settlements occur every 8 hours. limit=200 → ~67 days of history.
  Use "fundingRate" field for percentile calculation.
  NOTE: fundingTime is a STRING in OKX (vs integer in Binance) → int() cast required.

OI HISTORY  GET https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume
  Parameters: ccy (e.g. "BTC"), period="5m"
  Supported periods: 5m, 1H, 1D  (15m is NOT supported — verified 2026-04-14)
  Response: {"code": "0", "data": [
    ["1776177900000", "3373706587.4921", "82819158.5739"],  ← [ts, oi_usd, vol_usd]
    ...
  ]}
  Data arrives newest-first; stored as-is (most recent at data[-1]).
  Use data[][1] (OI in USD) for delta calculation (vs Binance's sumOpenInterest in BTC).
  DEVIATION from Binance: OI is in USD not base asset; period is 5m not 15m.

CURRENT OI  GET https://www.okx.com/api/v5/public/open-interest
  Parameters: instType="SWAP", instId="BTC-USDT-SWAP"
  Response: {"data": [{"oi": "...", "oiCcy": "...", "oiUsd": "...", "ts": "..."}]}
  (available but not currently used; rubik endpoint preferred for historical delta)

=============================================================================
"""

import asyncio
import math
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
import pandas as pd
import pandas_ta as ta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from feeds.okx_ws import OKXWebSocketManager
from feeds.feed_health_monitor import FeedHealthMonitor

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from feeds.deribit_feed import DeribitFeed

# ── Regime state constants ────────────────────────────────────────────────────
TREND_UP_CLEAN = "TREND_UP_CLEAN"
TREND_DOWN_CLEAN = "TREND_DOWN_CLEAN"
TREND_UP_CROWDED = "TREND_UP_CROWDED"
TREND_DOWN_CROWDED = "TREND_DOWN_CROWDED"
RANGE_BALANCED = "RANGE_BALANCED"
RANGE_UNSTABLE = "RANGE_UNSTABLE"
EVENT_RISK = "EVENT_RISK"
NO_TRADE = "NO_TRADE"

_REST_BASE = "https://www.okx.com"

# Minimum candles required for each calculation
_MIN_ADX_CANDLES = 30
_MIN_VOL_CANDLES = 20
_MIN_VOL_HISTORY = 100   # for 30-day percentile context
_MIN_EMA_CANDLES = 22    # 20-period EMA needs 20+ candles

# Lookback for funding percentile (7-day window)
_FUNDING_PERCENTILE_DAYS = 7


def _pair_to_ccy(pair: str) -> str:
    """'BTC-USDT-SWAP' → 'BTC'  (used for OKX rubik OI endpoint ccy param)"""
    return pair.split("-")[0]


class RegimeClassifier:
    """
    Layer A: classifies market regime from confirmed closed 15m bars.
    classify() is the main entry point. It is synchronous and safe to call
    from any context — it reads pre-fetched cached data.
    """

    def __init__(
        self,
        ws_manager: OKXWebSocketManager,
        health_monitor: FeedHealthMonitor,
        deribit_feed: "DeribitFeed | None" = None,
    ) -> None:
        self._ws = ws_manager
        self._monitor = health_monitor
        self._deribit = deribit_feed
        self._scheduler: Optional[AsyncIOScheduler] = None

        # Cache: pair → list of funding settlement dicts (oldest first)
        self._funding_history: dict[str, list[dict]] = {}
        # Cache: pair → list of OI history dicts (oldest first)
        self._oi_history: dict[str, list[dict]] = {}

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Fetch historical data immediately, then schedule periodic refresh."""
        for pair in config.TRADING_PAIRS:
            await self._refresh_historical_data(pair)

        self._scheduler = AsyncIOScheduler()
        for pair in config.TRADING_PAIRS:
            self._scheduler.add_job(
                self._refresh_historical_data,
                trigger="interval",
                hours=config.FUNDING_CACHE_REFRESH_HOURS,
                args=[pair],
                id=f"hist_refresh_{pair}",
            )
        self._scheduler.start()
        print(
            f"[regime] Historical data loaded. "
            f"Refresh every {config.FUNDING_CACHE_REFRESH_HOURS}h via APScheduler."
        )

    # ── Historical data fetching ───────────────────────────────────────────────

    async def _refresh_historical_data(self, pair: str) -> None:
        """
        Fetch and cache funding rate history and OI history for the given pair.
        Stores results in self._funding_history[pair] and self._oi_history[pair].
        Logs errors to stderr without raising.

        OKX endpoints (verified 2026-04-14):
          Funding history: GET /api/v5/public/funding-rate-history
            params: instId=BTC-USDT-SWAP, limit=200
            response: data[] = [{fundingRate, fundingTime (string ms), ...}]
          OI history: GET /api/v5/rubik/stat/contracts/open-interest-volume
            params: ccy=BTC, period=5m  (15m NOT supported by OKX)
            response: data[] = [[ts_str, oi_usd_str, vol_usd_str], ...]
            data arrives newest-first; _get_oi_delta() uses [-2] and [-1].
        """
        ccy = _pair_to_ccy(pair)
        headers = {"User-Agent": "trading-bot/1.0"}

        async with aiohttp.ClientSession() as session:
            # Funding rate history
            try:
                async with session.get(
                    f"{_REST_BASE}/api/v5/public/funding-rate-history",
                    params={"instId": pair, "limit": 200},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        raw = await resp.json()
                        data = raw.get("data", [])
                        self._funding_history[pair] = data
                        print(
                            f"[regime] {pair}: loaded {len(data)} funding settlements"
                        )
                    else:
                        print(
                            f"[regime] {pair}: funding history HTTP {resp.status}",
                            file=sys.stderr,
                        )
            except Exception as exc:
                print(
                    f"[regime] {pair}: funding history fetch error: {exc}",
                    file=sys.stderr,
                )

            # OI history — OKX rubik endpoint, period=5m (15m not supported)
            try:
                async with session.get(
                    f"{_REST_BASE}/api/v5/rubik/stat/contracts/open-interest-volume",
                    params={"ccy": ccy, "period": "5m"},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        raw = await resp.json()
                        data = raw.get("data", [])
                        self._oi_history[pair] = data
                        print(
                            f"[regime] {pair}: loaded {len(data)} OI history entries"
                        )
                    else:
                        print(
                            f"[regime] {pair}: OI history HTTP {resp.status}",
                            file=sys.stderr,
                        )
            except Exception as exc:
                print(
                    f"[regime] {pair}: OI history fetch error: {exc}", file=sys.stderr
                )

    # ── Indicator helpers ─────────────────────────────────────────────────────

    def _get_funding_percentile(self, pair: str) -> Optional[float]:
        """
        Return where the current live funding rate sits within the 7-day
        historical distribution (0.0–100.0). Returns None if insufficient data.

        Live funding rate comes from the ticker WebSocket (field "r", verified M1).
        Historical distribution comes from the cached fundingRate REST data.
        """
        ticker = self._ws.get_latest_ticker(pair)
        if not ticker:
            return None
        live_rate = ticker.get("funding_rate")
        if live_rate is None:
            return None

        history = self._funding_history.get(pair, [])
        if not history:
            return None

        # Filter to last 7 days
        # OKX fundingTime is a STRING (ms) — cast to int before comparison
        cutoff_ms = (
            datetime.now(timezone.utc) - timedelta(days=_FUNDING_PERCENTILE_DAYS)
        ).timestamp() * 1000
        rates = [
            float(e["fundingRate"])
            for e in history
            if int(e.get("fundingTime", 0)) >= cutoff_ms
        ]
        if len(rates) < 3:
            return None

        # Percentile rank: fraction of historical rates <= live_rate
        below = sum(1 for r in rates if r <= live_rate)
        return (below / len(rates)) * 100.0

    def _get_oi_delta(self, pair: str) -> Optional[float]:
        """
        Return the percentage change in OI over the last interval.
        Uses the two most recent OI history entries.
        Positive = OI growing (fresh participation), negative = OI falling (covering).
        Returns None if fewer than 2 entries available.

        OKX rubik endpoint format: data[] = [[ts_str, oi_usd_str, vol_usd_str], ...]
        Arrives newest-first, so history[0] is most recent, history[-1] is oldest.
        Use history[0] (newest) and history[1] (second newest) for delta.
        OI is in USD (not base asset) — delta calculation is identical regardless.
        """
        history = self._oi_history.get(pair, [])
        if len(history) < 2:
            return None
        # history[0] = newest entry, history[1] = previous entry
        curr_oi = float(history[0][1])   # oi_usd at index 1
        prev_oi = float(history[1][1])
        if prev_oi == 0:
            return None
        return (curr_oi - prev_oi) / prev_oi * 100.0

    def _get_realized_vol_percentile(self, pair: str) -> Optional[float]:
        """
        Calculate 15m realised volatility using the last 20 closed 15m candles
        (std dev of log returns, annualised to hourly scale).
        Compare against the full available history to get percentile rank.
        Returns 0.0–100.0, or None if insufficient data.
        """
        candles = self._ws.get_closed_candles(pair, "15m")
        if len(candles) < _MIN_VOL_CANDLES + 1:
            return None

        closes = [c["close"] for c in candles]
        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
        ]

        # Current vol: std of last _MIN_VOL_CANDLES log returns
        recent = log_returns[-_MIN_VOL_CANDLES:]
        if len(recent) < 2:
            return None
        mean = sum(recent) / len(recent)
        variance = sum((r - mean) ** 2 for r in recent) / (len(recent) - 1)
        current_vol = math.sqrt(variance)

        # Historical distribution: rolling windows over all available candles
        # Need at least _MIN_VOL_HISTORY + _MIN_VOL_CANDLES candles for meaningful context
        if len(log_returns) < _MIN_VOL_CANDLES:
            return None

        hist_vols = []
        for i in range(_MIN_VOL_CANDLES, len(log_returns) + 1):
            window = log_returns[i - _MIN_VOL_CANDLES: i]
            if len(window) < 2:
                continue
            m = sum(window) / len(window)
            v = sum((r - m) ** 2 for r in window) / (len(window) - 1)
            hist_vols.append(math.sqrt(v))

        if not hist_vols:
            return None

        below = sum(1 for v in hist_vols if v <= current_vol)
        return (below / len(hist_vols)) * 100.0

    def _get_adx(self, pair: str) -> Optional[float]:
        """
        Calculate ADX from the last 30 closed 15m candles using pandas-ta.
        Returns the most recent ADX value, or None if insufficient data.
        """
        candles = self._ws.get_closed_candles(pair, "15m")
        if len(candles) < _MIN_ADX_CANDLES:
            return None

        df = pd.DataFrame(candles[-_MIN_ADX_CANDLES:])
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)

        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_df is None or adx_df.empty:
            return None

        # pandas-ta names the ADX column "ADX_14"
        adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
        if not adx_col:
            return None

        val = adx_df[adx_col[0]].iloc[-1]
        return float(val) if not math.isnan(val) else None

    def _get_ma_slope(self, pair: str) -> float:
        """
        Calculate the slope of the 20-period EMA on 15m closes.
        Returns (ema[-1] - ema[-2]) / ema[-2] as a fractional change.
        Positive = upward slope, negative = downward slope.
        Returns 0.0 if insufficient data (treated as neutral/ambiguous).
        """
        candles = self._ws.get_closed_candles(pair, "15m")
        if len(candles) < _MIN_EMA_CANDLES:
            return 0.0

        closes = pd.Series([float(c["close"]) for c in candles])
        ema = closes.ewm(span=20, adjust=False).mean()

        if len(ema) < 2:
            return 0.0
        e_prev = ema.iloc[-2]
        e_curr = ema.iloc[-1]
        if e_prev == 0:
            return 0.0
        return float((e_curr - e_prev) / e_prev)

    # ── Main classification ───────────────────────────────────────────────────

    def classify(self, pair: str) -> dict:
        """
        Classify the current market regime for the given pair.

        Returns a dict with keys:
          regime, adx, realized_vol_pct, funding_pct, oi_delta, ma_slope, reason,
          iv, iv_spike, iv_multiplier

        Classification order (must be evaluated in this exact sequence):
          1. Feed health check → NO_TRADE if any core feed degraded
          2. Data sufficiency check → NO_TRADE if ADX or vol unavailable
          3. ADX ambiguous band → NO_TRADE (early return — no IV override)
          4. Vol outside band + ADX ranging → RANGE_UNSTABLE (early return — no IV override)
          5. ADX trending → TREND_{direction}_{CLEAN|CROWDED} (falls through to IV check)
          6. ADX ranging → RANGE_BALANCED or RANGE_UNSTABLE (falls through to IV check)
          7. Deribit IV override → annotate iv, iv_spike, iv_multiplier on result
        """
        result = {
            "regime": NO_TRADE,
            "adx": None,
            "realized_vol_pct": None,
            "funding_pct": None,
            "oi_delta": None,
            "ma_slope": None,
            "reason": "",
            "iv": None,
            "iv_spike": False,
            "iv_multiplier": 1.0,
        }

        # ── Step 1: Feed health check ─────────────────────────────────────────
        if not self._monitor.are_core_feeds_healthy():
            degraded = self._monitor.get_degraded_feeds()
            result["reason"] = f"core feed degraded: {degraded}"
            return result

        # ── Step 2: Gather indicators ─────────────────────────────────────────
        adx = self._get_adx(pair)
        vol_pct = self._get_realized_vol_percentile(pair)
        funding_pct = self._get_funding_percentile(pair)
        oi_delta = self._get_oi_delta(pair)
        ma_slope = self._get_ma_slope(pair)

        result["adx"] = adx
        result["realized_vol_pct"] = vol_pct
        result["funding_pct"] = funding_pct
        result["oi_delta"] = oi_delta
        result["ma_slope"] = ma_slope

        if adx is None or vol_pct is None:
            result["reason"] = (
                f"insufficient candle data "
                f"(adx={'ok' if adx is not None else 'None'}, "
                f"vol={'ok' if vol_pct is not None else 'None'})"
            )
            return result

        # ── Step 3: ADX ambiguous band → NO_TRADE ────────────────────────────
        if config.ADX_AMBIGUOUS_LOW <= adx <= config.ADX_AMBIGUOUS_HIGH:
            result["reason"] = (
                f"ADX in ambiguous band ({adx:.1f} in "
                f"[{config.ADX_AMBIGUOUS_LOW}, {config.ADX_AMBIGUOUS_HIGH}])"
            )
            return result

        # ── Step 4: Vol outside normal band + ADX ranging → RANGE_UNSTABLE ───
        vol_outside_band = (
            vol_pct < config.REALIZED_VOL_LOW_PCT
            or vol_pct > config.REALIZED_VOL_HIGH_PCT
        )
        if vol_outside_band and adx < config.ADX_TREND_THRESHOLD:
            result["regime"] = RANGE_UNSTABLE
            result["reason"] = (
                f"ranging (adx={adx:.1f}) + vol outside band "
                f"(vol_pct={vol_pct:.1f})"
            )
            return result

        # ── Step 5: ADX trending ──────────────────────────────────────────────
        if adx >= config.ADX_TREND_THRESHOLD:
            direction = "UP" if ma_slope > 0 else "DOWN"
            crowded = False
            crowd_reason = ""

            if funding_pct is not None:
                if direction == "UP" and funding_pct >= config.FUNDING_CROWDED_PERCENTILE:
                    crowded = True
                    crowd_reason = (
                        f"longs crowded (funding_pct={funding_pct:.1f} >= "
                        f"{config.FUNDING_CROWDED_PERCENTILE})"
                    )
                elif direction == "DOWN" and funding_pct <= config.FUNDING_SHORTS_CROWDED_PERCENTILE:
                    crowded = True
                    crowd_reason = (
                        f"shorts crowded (funding_pct={funding_pct:.1f} <= "
                        f"{config.FUNDING_SHORTS_CROWDED_PERCENTILE})"
                    )

            vol_note = f", vol_outside_band=True" if vol_outside_band else ""
            if crowded:
                result["regime"] = (
                    TREND_UP_CROWDED if direction == "UP" else TREND_DOWN_CROWDED
                )
                result["reason"] = (
                    f"trending {direction} (adx={adx:.1f}, slope={ma_slope:.6f}), "
                    f"{crowd_reason}{vol_note}"
                )
            else:
                result["regime"] = (
                    TREND_UP_CLEAN if direction == "UP" else TREND_DOWN_CLEAN
                )
                fp_str = f"{funding_pct:.1f}" if funding_pct is not None else "N/A"
                result["reason"] = (
                    f"trending {direction} (adx={adx:.1f}, slope={ma_slope:.6f}), "
                    f"funding_pct={fp_str}{vol_note}"
                )
            # Fall through to IV override block below

        # ── Step 6: ADX ranging ───────────────────────────────────────────────
        elif adx < config.ADX_TREND_THRESHOLD:
            if vol_pct > config.RANGE_UNSTABLE_VOL_THRESHOLD:
                result["regime"] = RANGE_UNSTABLE
                result["reason"] = (
                    f"ranging (adx={adx:.1f}) + elevated vol "
                    f"(vol_pct={vol_pct:.1f} > {config.RANGE_UNSTABLE_VOL_THRESHOLD})"
                )
            else:
                result["regime"] = RANGE_BALANCED
                fp_str = f"{funding_pct:.1f}" if funding_pct is not None else "N/A"
                result["reason"] = (
                    f"ranging (adx={adx:.1f}), vol balanced "
                    f"(vol_pct={vol_pct:.1f}), "
                    f"funding_pct={fp_str}"
                )
            # Fall through to IV override block below

        # ── Step 7: Deribit IV override ───────────────────────────────────────
        # Only annotates — does NOT change regime state, just records IV data
        # and iv_multiplier for the decision loop to apply to position sizing.
        # classify() is synchronous; IV data is fetched async and cached.
        # On first call, cache is empty → iv=None, multiplier=1.0 (safe default).
        if self._deribit is not None:
            # Use the cached value synchronously (DeribitFeed._cache)
            ccy = "ETH" if "ETH" in pair.upper() else "BTC"
            cached = self._deribit._cache.get(ccy)
            if cached and cached.get("data"):
                data = cached["data"]
                current_iv = float(data[-1][4])
                result["iv"] = current_iv

                # Spike detection
                if len(data) >= 2:
                    prev_iv = float(data[-2][4])
                    if prev_iv > 0:
                        pct_chg = (current_iv - prev_iv) / prev_iv
                        result["iv_spike"] = pct_chg >= config.DERIBIT_IV_SPIKE_PCT

                # Size multiplier
                if current_iv >= config.DERIBIT_IV_EXTREME_THRESHOLD:
                    result["iv_multiplier"] = config.DERIBIT_IV_EXTREME_MULTIPLIER
                elif current_iv >= config.DERIBIT_IV_HIGH_THRESHOLD:
                    result["iv_multiplier"] = config.DERIBIT_IV_SIZE_MULTIPLIER
                else:
                    result["iv_multiplier"] = 1.0

                # Append IV note to reason string
                spike_note = " [IV SPIKE]" if result["iv_spike"] else ""
                result["reason"] += (
                    f", iv={current_iv:.1f}{spike_note}"
                    f", iv_mult={result['iv_multiplier']:.2f}"
                )

        return result
