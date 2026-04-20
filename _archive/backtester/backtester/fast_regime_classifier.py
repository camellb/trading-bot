"""
FastRegimeClassifier — applies regime and directional-bias logic to
pre-computed indicators from IndicatorCache rather than recomputing
them on every candle.

Design
------
The parameter sweep varies ADX_TREND_THRESHOLD, ATR_STOP_MULTIPLIER,
and ATR_TP_MULTIPLIER.  The underlying indicator *values* (ADX numbers,
MACD slopes, realised vol percentiles, etc.) are fixed for a given pair
and date range.  This classifier accepts the threshold values as call-
time arguments so the same instance can serve every sweep combination
without any cache rebuild.

classify()          → Layer A equivalent (regime classification)
analyse_direction() → Layer B equivalent (directional bias)
"""

import math

import config
from backtester.indicator_cache import IndicatorCache


class FastRegimeClassifier:
    """
    Reads from IndicatorCache.  All threshold parameters passed explicitly
    per call so one instance covers all 80 sweep combinations.
    """

    def __init__(self, cache: IndicatorCache) -> None:
        self._cache = cache

    # ── Layer A: Regime ───────────────────────────────────────────────────────

    def classify(
        self,
        pair: str,
        timestamp,
        adx_threshold: float,
        adx_ambiguous_low: float  = config.ADX_AMBIGUOUS_LOW,
        adx_ambiguous_high: float = config.ADX_AMBIGUOUS_HIGH,
        funding_pct: float = 50.0,   # neutral stub (real value not available historically)
        oi_delta: float    = 0.1,    # neutral stub
    ) -> dict:
        """
        Classify market regime for pair at timestamp.

        adx_threshold is passed explicitly (not read from config) so the
        sweep can test different ADX values without mutating global state.
        The config setter on ADX_TREND_THRESHOLD is still used by the slow
        engine; here we keep the two paths independent.
        """
        indicators = self._cache.get_indicators_at(pair, timestamp)
        if not indicators:
            return {"regime": "NO_TRADE", "reason": "no cached data", "pair": pair}

        adx       = indicators.get("ADX_14")
        rvol_pct  = indicators.get("RVOL_PCT")
        ema_slope = indicators.get("EMA_SLOPE")

        def _nan(v: object) -> bool:
            return v is None or (isinstance(v, float) and math.isnan(v))

        if _nan(adx):
            return {"regime": "NO_TRADE", "reason": "ADX not ready", "pair": pair}
        if _nan(rvol_pct):
            return {"regime": "NO_TRADE", "reason": "RVOL_PCT not ready", "pair": pair}

        # ADX ambiguous zone — too uncertain to classify as trend or range
        if adx_ambiguous_low <= adx <= adx_ambiguous_high:
            return {
                "regime": "NO_TRADE", "reason": "ADX in ambiguous zone",
                "adx": adx, "pair": pair,
            }

        # ── Trend ─────────────────────────────────────────────────────────────
        if adx >= adx_threshold:
            ema_up    = not _nan(ema_slope) and ema_slope > 0
            direction = "UP" if ema_up else "DOWN"
            crowded   = (
                (direction == "UP"   and funding_pct >= config.FUNDING_CROWDED_PERCENTILE)
                or
                (direction == "DOWN" and funding_pct <= config.FUNDING_SHORTS_CROWDED_PERCENTILE)
            )
            suffix = "CROWDED" if crowded else "CLEAN"
            return {
                "regime": f"TREND_{direction}_{suffix}",
                "adx": adx, "rvol_pct": rvol_pct,
                "ema_slope": ema_slope, "pair": pair,
            }

        # ── Range ─────────────────────────────────────────────────────────────
        if rvol_pct > config.RANGE_UNSTABLE_VOL_THRESHOLD:
            return {
                "regime": "RANGE_UNSTABLE",
                "adx": adx, "rvol_pct": rvol_pct, "pair": pair,
            }
        return {
            "regime": "RANGE_BALANCED",
            "adx": adx, "rvol_pct": rvol_pct, "pair": pair,
        }

    # ── Layer B: Directional bias ─────────────────────────────────────────────

    def analyse_direction(self, pair: str, timestamp) -> dict:
        """
        Fast directional bias using pre-computed indicators.

        Logic:
          LONG  — market structure BULLISH  and MACD slope > 0
          SHORT — market structure BEARISH  and MACD slope < 0
          NEUTRAL otherwise

        VWAP position (close vs intraday VWAP) is recorded for reference
        but is not used as a hard gate here because the sweep cares about
        regime × stop/TP parameters, not subtle VWAP effects.
        """
        indicators = self._cache.get_indicators_at(pair, timestamp)
        if not indicators:
            return {"signal": "NEUTRAL", "reason": "no cached data"}

        structure  = indicators.get("STRUCTURE", "NEUTRAL")
        macd_slope = indicators.get("MACD_SLOPE", 0.0)
        vwap       = indicators.get("VWAP")
        close      = indicators.get("close")

        def _nan(v: object) -> bool:
            return v is None or (isinstance(v, float) and math.isnan(v))

        if _nan(macd_slope):
            macd_slope = 0.0

        vwap_pos = "unknown"
        if not _nan(vwap) and not _nan(close):
            vwap_pos = "above" if close > vwap else "below"

        base = {
            "market_structure": structure,
            "macd_slope": macd_slope,
            "vwap_position": vwap_pos,
        }

        if structure == "BULLISH" and macd_slope > 0:
            return {**base, "signal": "LONG"}
        if structure == "BEARISH" and macd_slope < 0:
            return {**base, "signal": "SHORT"}

        return {**base, "signal": "NEUTRAL", "reason": "no confluence"}
