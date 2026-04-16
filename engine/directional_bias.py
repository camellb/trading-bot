"""
Layer B — Directional Bias.

Determines trade direction using a sparse, non-collinear signal set:
  - Market structure (primary): higher highs/higher lows vs lower highs/lower lows on 15m
  - MACD slope (secondary): direction and momentum of MACD line on 15m
  - VWAP position (contextual anchor, not a vote): used as tie-breaker only

EMA crossover is intentionally excluded to avoid collinearity with MACD.
Runs only when regime is not NO_TRADE or EVENT_RISK.

Voting logic:
  1. If structure and MACD agree → that direction wins.
  2. If structure and MACD conflict → VWAP breaks the tie (price > VWAP = LONG lean).
  3. If only one of structure/MACD is non-neutral → VWAP must confirm; else NEUTRAL.
  4. If both structure and MACD are neutral → NEUTRAL.

Returns signal: 'LONG', 'SHORT', or 'NEUTRAL'
"""

import pandas as pd
import pandas_ta as ta

import config
from feeds.okx_ws import OKXWebSocketManager
from feeds.feed_health_monitor import FeedHealthMonitor

# Minimum candle counts for each indicator
_MIN_MACD_CANDLES = 40   # MACD(12,26,9) needs 35+ candles for stable output
_MIN_STRUCTURE_CANDLES = 6  # Need at least 6 to identify 3-candle swing sequences
_MIN_VWAP_CANDLES = 2


class DirectionalBias:
    """
    Layer B: determines trade direction from market structure, MACD slope,
    and VWAP position.

    Call evaluate(pair) to get signal dict with keys:
      signal ('LONG', 'SHORT', 'NEUTRAL'), reason, structure, macd, vwap
    """

    def __init__(
        self,
        ws_manager: OKXWebSocketManager,
        health_monitor: FeedHealthMonitor,
    ) -> None:
        self._ws = ws_manager
        self._monitor = health_monitor

    # ── Market structure ──────────────────────────────────────────────────────

    def _get_market_structure(self, pair: str) -> str:
        """
        Identify market structure using the last 6 closed 15m candles.

        Bullish (LONG):  3 consecutive higher highs AND higher lows in recent candles.
        Bearish (SHORT): 3 consecutive lower highs AND lower lows in recent candles.
        Returns 'LONG', 'SHORT', or 'NEUTRAL'.
        """
        candles = self._ws.get_closed_candles(pair, "15m")
        if len(candles) < _MIN_STRUCTURE_CANDLES:
            return "NEUTRAL"

        recent = candles[-_MIN_STRUCTURE_CANDLES:]
        highs = [float(c["high"]) for c in recent]
        lows = [float(c["low"]) for c in recent]

        # Use last 3 candle highs and lows (indices -3, -2, -1)
        hh = highs[-1] > highs[-2] > highs[-3]
        hl = lows[-1] > lows[-2] > lows[-3]
        lh = highs[-1] < highs[-2] < highs[-3]
        ll = lows[-1] < lows[-2] < lows[-3]

        if hh and hl:
            return "LONG"
        if lh and ll:
            return "SHORT"
        return "NEUTRAL"

    # ── MACD slope ────────────────────────────────────────────────────────────

    def _get_macd_slope(self, pair: str) -> str:
        """
        Calculate MACD(12, 26, 9) on 15m closes using pandas-ta.
        Returns 'LONG' if the MACD line rose on the last bar,
                'SHORT' if it fell, 'NEUTRAL' if flat or unavailable.
        """
        candles = self._ws.get_closed_candles(pair, "15m")
        if len(candles) < _MIN_MACD_CANDLES:
            return "NEUTRAL"

        closes = pd.Series([float(c["close"]) for c in candles])
        macd_df = ta.macd(closes, fast=12, slow=26, signal=9)
        if macd_df is None or macd_df.empty:
            return "NEUTRAL"

        # pandas-ta MACD line column: "MACD_12_26_9"
        macd_col = [
            c for c in macd_df.columns
            if c.startswith("MACD_")
            and not c.startswith("MACDs_")
            and not c.startswith("MACDh_")
        ]
        if not macd_col:
            return "NEUTRAL"

        macd_vals = macd_df[macd_col[0]].dropna()
        if len(macd_vals) < 2:
            return "NEUTRAL"

        slope = float(macd_vals.iloc[-1]) - float(macd_vals.iloc[-2])
        if slope > 0:
            return "LONG"
        elif slope < 0:
            return "SHORT"
        return "NEUTRAL"

    # ── VWAP tie-breaker ──────────────────────────────────────────────────────

    def _get_vwap_bias(self, pair: str) -> str:
        """
        Session VWAP from all available closed 15m candles.
        Returns 'LONG' if last close > VWAP, 'SHORT' if below, 'NEUTRAL' if equal
        or unavailable.
        """
        candles = self._ws.get_closed_candles(pair, "15m")
        if len(candles) < _MIN_VWAP_CANDLES:
            return "NEUTRAL"

        total_tpv = 0.0
        total_vol = 0.0
        for c in candles:
            typical = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
            vol = float(c["volume"])
            total_tpv += typical * vol
            total_vol += vol

        if total_vol == 0:
            return "NEUTRAL"

        vwap = total_tpv / total_vol
        last_close = float(candles[-1]["close"])

        if last_close > vwap:
            return "LONG"
        elif last_close < vwap:
            return "SHORT"
        return "NEUTRAL"

    # ── Main signal ───────────────────────────────────────────────────────────

    def evaluate(self, pair: str) -> dict:
        """
        Evaluate directional bias for the given pair.

        Returns dict with keys:
          signal ('LONG', 'SHORT', or 'NEUTRAL')
          reason (str)
          structure ('LONG', 'SHORT', 'NEUTRAL')
          macd ('LONG', 'SHORT', 'NEUTRAL')
          vwap ('LONG', 'SHORT', 'NEUTRAL')
        """
        structure = self._get_market_structure(pair)
        macd = self._get_macd_slope(pair)
        vwap = self._get_vwap_bias(pair)

        # ── Rule 1: Primary + secondary agree ────────────────────────────────
        if structure != "NEUTRAL" and structure == macd:
            return {
                "signal": structure,
                "reason": f"structure={structure} + macd={macd} agree",
                "structure": structure,
                "macd": macd,
                "vwap": vwap,
            }

        # ── Rule 2: Primary and secondary conflict → VWAP tie-break ──────────
        if structure != "NEUTRAL" and macd != "NEUTRAL" and structure != macd:
            if vwap != "NEUTRAL":
                signal = vwap
                reason = (
                    f"structure={structure} vs macd={macd} conflict; "
                    f"vwap tie-break → {vwap}"
                )
            else:
                signal = "NEUTRAL"
                reason = (
                    f"structure={structure} vs macd={macd} conflict; "
                    f"vwap neutral → NEUTRAL"
                )
            return {
                "signal": signal,
                "reason": reason,
                "structure": structure,
                "macd": macd,
                "vwap": vwap,
            }

        # ── Rule 3: Only one signal — require VWAP confirmation ───────────────
        if structure != "NEUTRAL" and macd == "NEUTRAL":
            if vwap == structure:
                signal = structure
                reason = (
                    f"structure={structure} + vwap={vwap} confirm (macd neutral)"
                )
            else:
                signal = "NEUTRAL"
                reason = (
                    f"structure={structure} not confirmed by vwap={vwap} (macd neutral)"
                )
            return {
                "signal": signal,
                "reason": reason,
                "structure": structure,
                "macd": macd,
                "vwap": vwap,
            }

        if macd != "NEUTRAL" and structure == "NEUTRAL":
            if vwap == macd:
                signal = macd
                reason = (
                    f"macd={macd} + vwap={vwap} confirm (structure neutral)"
                )
            else:
                signal = "NEUTRAL"
                reason = (
                    f"macd={macd} not confirmed by vwap={vwap} (structure neutral)"
                )
            return {
                "signal": signal,
                "reason": reason,
                "structure": structure,
                "macd": macd,
                "vwap": vwap,
            }

        # ── Rule 4: All neutral ───────────────────────────────────────────────
        return {
            "signal": "NEUTRAL",
            "reason": "all signals neutral (structure=NEUTRAL, macd=NEUTRAL)",
            "structure": structure,
            "macd": macd,
            "vwap": vwap,
        }
