"""
IndicatorCache — computes every technical indicator ONCE for every pair
and stores the result as a DataFrame for O(1) per-bar lookups.

Why this exists
---------------
The parameter sweep tests 80 combinations of ADX_TREND_THRESHOLD ×
ATR_STOP_MULTIPLIER × ATR_TP_MULTIPLIER.  The underlying indicators
(ADX values, MACD slopes, ATR, realised vol, market structure) do not
change between combinations — only the classification thresholds do.
Building this cache once and re-using it across all 80 combinations
cuts per-combination replay time from ~15 min to ~seconds.

DataFrame index
---------------
UTC-aware pd.DatetimeIndex keyed on the candle's open_time (ms).
All numeric columns are float64.  STRUCTURE is object (str).

Columns produced
----------------
ADX_14, DMP_14, DMN_14          – ADX trend strength and directional movement
MACD_12_26_9, MACDh_12_26_9,
MACDs_12_26_9                   – MACD line, histogram, signal
EMA_20                          – 20-period exponential moving average
ATRr_14                         – 14-period ATR (RMA smoothing)
RVOL                            – annualised realised volatility (30-bar rolling)
RVOL_PCT                        – percentile rank of RVOL (rolling 2880-bar window)
MACD_SLOPE                      – 3-bar change in MACD line / 3
EMA_SLOPE                       – 2-bar % change in EMA_20
VWAP                            – intraday VWAP, resets at midnight UTC
STRUCTURE                       – BULLISH / BEARISH / NEUTRAL (3-bar pivot method)
"""

import math

import numpy as np
import pandas as pd
import pandas_ta as ta


class IndicatorCache:
    """
    Pre-computes all technical indicators for each pair from raw candle dicts
    (as returned by HistoricalDataFetcher.fetch_historical_candles).
    """

    def __init__(self) -> None:
        self._cache: dict[str, pd.DataFrame] = {}

    # ── Build ─────────────────────────────────────────────────────────────────

    def build_cache(self, pair: str, candles: list[dict]) -> int:
        """
        Compute all indicators from raw candle dicts and store in the cache.

        Parameters
        ----------
        pair    : OKX instrument ID, e.g. "BTC-USDT-SWAP"
        candles : list of candle dicts with keys open_time, open, high, low,
                  close, volume (as returned by HistoricalDataFetcher)

        Returns the number of rows stored (useful for progress logging).
        """
        if not candles:
            raise ValueError(f"No candles provided for {pair}")

        df = pd.DataFrame(candles)
        df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.sort_index()

        high  = df["high"].astype(float)
        low   = df["low"].astype(float)
        close = df["close"].astype(float)
        vol   = df["volume"].astype(float)

        # ── ADX(14) ───────────────────────────────────────────────────────────
        adx_df = ta.adx(high, low, close, length=14)
        if adx_df is not None:
            df = df.join(adx_df)
        else:
            df["ADX_14"] = np.nan

        # ── MACD(12,26,9) ─────────────────────────────────────────────────────
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        if macd_df is not None:
            df = df.join(macd_df)

        # ── EMA(20) ───────────────────────────────────────────────────────────
        ema = ta.ema(close, length=20)
        df["EMA_20"] = ema if ema is not None else np.nan

        # ── ATR(14) ───────────────────────────────────────────────────────────
        atr = ta.atr(high, low, close, length=14)
        # pandas_ta names it ATRr_14 (RMA) or ATR_14 depending on version
        df["ATRr_14"] = atr.values if atr is not None else np.nan

        # ── Realised vol ──────────────────────────────────────────────────────
        # 96 fifteen-minute bars per day; annualise by √(252 × 96)
        log_ret = np.log(close / close.shift(1))
        df["RVOL"] = log_ret.rolling(30).std() * math.sqrt(252 * 96)

        # Percentile rank over a rolling 30-day (2880-bar) window.
        # min_periods=30 means we get a useful value once 30 RVOL readings exist.
        df["RVOL_PCT"] = (
            df["RVOL"].rolling(2880, min_periods=30).rank(pct=True) * 100
        )

        # ── MACD slope (3-bar change / 3) ─────────────────────────────────────
        macd_col = "MACD_12_26_9"
        df["MACD_SLOPE"] = (
            df[macd_col].diff(3) / 3 if macd_col in df.columns else np.nan
        )

        # ── EMA slope (2-bar % change) ────────────────────────────────────────
        if "EMA_20" in df.columns:
            df["EMA_SLOPE"] = (
                (df["EMA_20"] - df["EMA_20"].shift(2)) / df["EMA_20"].shift(2)
            )
        else:
            df["EMA_SLOPE"] = np.nan

        # ── VWAP (intraday, resets at midnight UTC) ───────────────────────────
        tp  = (high + low + close) / 3
        tpv = tp * vol
        day = df.index.normalize()   # floor each timestamp to midnight UTC
        df["VWAP"] = (
            tpv.groupby(day).cumsum()
            / vol.groupby(day).cumsum().replace(0, np.nan)
        )

        # ── Market structure (vectorised 3-bar swing pivot) ───────────────────
        df["STRUCTURE"] = self._compute_structure(close)

        self._cache[pair] = df
        return len(df)

    # ── Market structure ──────────────────────────────────────────────────────

    @staticmethod
    def _compute_structure(close: pd.Series) -> pd.Series:
        """
        Classify each bar as BULLISH / BEARISH / NEUTRAL using 3-bar pivots.

        Swing high at i: close[i] > close[i-1]  and  close[i] > close[i+1]
        Swing low  at i: close[i] < close[i-1]  and  close[i] < close[i+1]

        BULLISH: the 2 most-recent swing highs are ascending AND the 2
                 most-recent swing lows are ascending.
        BEARISH: both are descending.
        NEUTRAL: otherwise.

        Vectorised approach: forward-fill the (current, previous) pivot pair
        across the full index, then compare in one bulk operation.
        """
        sh_mask = (close > close.shift(1)) & (close > close.shift(-1))
        sl_mask = (close < close.shift(1)) & (close < close.shift(-1))

        sh_vals = close.where(sh_mask).dropna()
        sl_vals = close.where(sl_mask).dropna()

        if len(sh_vals) < 2 or len(sl_vals) < 2:
            return pd.Series("NEUTRAL", index=close.index, dtype=object)

        # Build a "previous pivot" series: same index as sh_vals / sl_vals,
        # but the value is the preceding pivot's price (so prev[i] = pivot[i-1]).
        sh_prev = pd.Series(
            [np.nan] + sh_vals.tolist()[:-1], index=sh_vals.index
        )
        sl_prev = pd.Series(
            [np.nan] + sl_vals.tolist()[:-1], index=sl_vals.index
        )

        # Forward-fill across the full index so every bar has the last two
        # pivot values available without a Python loop.
        last_sh1 = sh_vals.reindex(close.index, method="ffill")   # most recent
        last_sh2 = sh_prev.reindex(close.index, method="ffill")   # second most recent
        last_sl1 = sl_vals.reindex(close.index, method="ffill")
        last_sl2 = sl_prev.reindex(close.index, method="ffill")

        bullish = (last_sh1 > last_sh2) & (last_sl1 > last_sl2)
        bearish = (last_sh1 < last_sh2) & (last_sl1 < last_sl2)

        structure = pd.Series("NEUTRAL", index=close.index, dtype=object)
        structure[bullish] = "BULLISH"
        structure[bearish] = "BEARISH"
        return structure

    # ── Lookups ───────────────────────────────────────────────────────────────

    def get_indicators_at(self, pair: str, timestamp) -> dict:
        """
        Return all pre-computed indicator values as a dict for the given bar.

        timestamp may be a datetime, pd.Timestamp, or anything pd.Timestamp()
        accepts.  If there is no exact match the most recent prior bar is
        returned (pad / ffill behaviour).
        """
        df = self._cache.get(pair)
        if df is None or df.empty:
            return {}

        ts = pd.Timestamp(timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")

        try:
            row = df.loc[ts]
            if isinstance(row, pd.DataFrame):   # duplicate index guard
                row = row.iloc[-1]
            return row.to_dict()
        except KeyError:
            idx = df.index.get_indexer([ts], method="pad")[0]
            if idx < 0:
                return {}
            return df.iloc[idx].to_dict()

    def get_candles_up_to(self, pair: str, timestamp, n: int = 30) -> pd.DataFrame:
        """Return the last n rows up to and including timestamp."""
        df = self._cache.get(pair)
        if df is None or df.empty:
            return pd.DataFrame()

        ts = pd.Timestamp(timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")

        return df[df.index <= ts].tail(n)
