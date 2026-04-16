# NOTE: This module is used by backtester/ for simulation.
# The live bot no longer uses RiskEngine for decisions.
# Claude Sonnet (engine/strategist.py) makes all live decisions.

"""
Layer F — Risk Engine.

Sizes positions and sets ATR-scaled stop-loss and take-profit levels.
Applies regime-specific and degraded-mode size multipliers from config.
Enforces the daily loss cap (config.DAILY_LOSS_CAP_USD). Halts all order
placement for the remainder of the session when the cap is hit.

Key parameters (all sourced from config.py):
  - ATR_STOP_MULTIPLIER, ATR_TP_MULTIPLIER
  - MAX_POSITION_PCT, MAX_SIMULTANEOUS_POSITIONS
  - DAILY_LOSS_CAP_USD (PROVISIONAL)
  - SIZE_MULTIPLIER_CROWDED, SIZE_MULTIPLIER_RANGE_UNSTABLE, SIZE_MULTIPLIER_EVENT_RISK
  - NEWS_DEGRADED_SIZE_MULTIPLIER
  - STARTING_CAPITAL_USD (replaced by live CCXT balance in M5)

ATR is sourced from confirmed closed 15m candles (pandas-ta ATR(14)).
Stop and take-profit distances are expressed in USD price units:
  stop_distance_usd  = atr * ATR_STOP_MULTIPLIER
  tp_distance_usd    = atr * ATR_TP_MULTIPLIER
These are applied as price offsets in calculate_entry_prices().
"""

import math
import re
import sys

import pandas as pd
import pandas_ta as ta

import config
from feeds.okx_ws import OKXWebSocketManager

# Minimum candle count for ATR(14) calculation
_MIN_ATR_CANDLES = 30

# Regime → base size multiplier mapping
_REGIME_MULTIPLIERS: dict[str, float] = {
    "TREND_UP_CLEAN":     1.0,
    "TREND_DOWN_CLEAN":   1.0,
    "TREND_UP_CROWDED":   config.SIZE_MULTIPLIER_CROWDED,
    "TREND_DOWN_CROWDED": config.SIZE_MULTIPLIER_CROWDED,
    "RANGE_BALANCED":     1.0,
    "RANGE_UNSTABLE":     config.SIZE_MULTIPLIER_RANGE_UNSTABLE,
    "EVENT_RISK":         config.SIZE_MULTIPLIER_EVENT_RISK,
    "NO_TRADE":           0.0,
}


class RiskEngine:
    """
    Layer F: position sizing, stop-loss / take-profit calculation, and
    daily loss cap enforcement.

    get_portfolio_value() returns config.STARTING_CAPITAL_USD for now;
    replaced with a live CCXT balance query in M5.
    """

    def __init__(self, ws_manager: OKXWebSocketManager) -> None:
        self._ws = ws_manager

    # ── ATR ───────────────────────────────────────────────────────────────────

    def _get_atr(self, pair: str) -> float | None:
        """
        Calculate ATR(14) on the last 30 confirmed closed 15m candles.
        Returns the most recent ATR value, or None if insufficient data.
        ATR is expressed in price units (USD for BTC/ETH perpetuals).
        """
        candles = self._ws.get_closed_candles(pair, "15m")
        if len(candles) < _MIN_ATR_CANDLES:
            return None

        df = pd.DataFrame(candles[-_MIN_ATR_CANDLES:])
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)

        atr_series = ta.atr(df["high"], df["low"], df["close"], length=14)
        if atr_series is None or atr_series.empty:
            return None

        val = atr_series.iloc[-1]
        return float(val) if not math.isnan(val) else None

    # ── Conviction scoring ────────────────────────────────────────────────────

    def calculate_conviction_score(self, layer_results: dict) -> float:
        """
        Calculate a conviction score (0.0–1.0) from the layer results dict.

        layer_results keys:
          regime_data  – dict from regime_classifier.classify()
          layer_b      – dict from directional_bias.evaluate()
          layer_c      – dict from crypto_confirmation.evaluate()
          layer_d      – tuple (bool, str) from execution_filter.evaluate()
                         OR dict with keys spread_pct/depth_ratio/imbalance (tests)
          event_state  – dict from event_overlay.get_event_state()

        Scoring breakdown:
          Layer A (regime quality):   0.00–0.30
          Layer B (directional bias): 0.00–0.25
          Layer C (crypto conf.):     0.00–0.25
          Layer D (exec quality):     0.00–0.20
          Max total:                  1.00
        """
        score = 0.0

        regime_data = layer_results.get("regime_data") or {}
        layer_b     = layer_results.get("layer_b")     or {}
        layer_c     = layer_results.get("layer_c")     or {}
        layer_d     = layer_results.get("layer_d")

        # ── Layer A: Regime quality (0.0 to 0.30) ────────────────────────────
        _regime_scores: dict[str, float] = {
            "TREND_UP_CLEAN":     0.30,
            "TREND_DOWN_CLEAN":   0.30,
            "TREND_UP_CROWDED":   0.15,
            "TREND_DOWN_CROWDED": 0.15,
            "RANGE_BALANCED":     0.20,
            "RANGE_UNSTABLE":     0.10,
            "EVENT_RISK":         0.05,
        }
        score += _regime_scores.get(regime_data.get("regime", "NO_TRADE"), 0.0)

        # ── Layer B: Directional signal strength (0.0 to 0.25) ───────────────
        # Support both real data keys ("structure" str, "macd" str direction)
        # and test keys ("market_structure" str, "macd_slope" float).
        structure = layer_b.get("structure") or layer_b.get("market_structure", "NEUTRAL")
        macd_val  = layer_b.get("macd") or layer_b.get("macd_slope")

        if structure in ("BULLISH", "BEARISH", "LONG", "SHORT"):
            score += 0.15

        if isinstance(macd_val, (int, float)):
            if abs(float(macd_val)) > 0.001:
                score += 0.10
        elif isinstance(macd_val, str) and macd_val in ("LONG", "SHORT"):
            score += 0.10

        # ── Layer C: Crypto confirmation (0.0 to 0.25) ───────────────────────
        # Support both string-signal keys (tests) and numeric keys (real data).
        funding_signal = layer_c.get("funding_signal")
        oi_signal      = layer_c.get("oi_signal")
        basis_signal   = layer_c.get("basis_signal")

        if funding_signal is not None:
            # Test format: explicit "SUPPORTS" / "NEUTRAL" strings
            if funding_signal == "SUPPORTS":
                score += 0.10
        else:
            # Real format: percentile — mid-range = not crowded = supports
            funding_pct = layer_c.get("funding_pct")
            if funding_pct is not None and (
                config.FUNDING_SHORTS_CROWDED_PERCENTILE
                < funding_pct
                < config.FUNDING_CROWDED_PERCENTILE
            ):
                score += 0.10

        if oi_signal is not None:
            if oi_signal == "SUPPORTS":
                score += 0.10
        else:
            # Real format: positive OI delta = fresh participation = supports
            oi_delta = layer_c.get("oi_delta")
            if oi_delta is not None and float(oi_delta) > 0:
                score += 0.10

        if basis_signal is not None:
            if basis_signal == "SUPPORTS":
                score += 0.05
        else:
            # Real format: basis within normal bounds = supports
            basis_pct = layer_c.get("basis_pct")
            if basis_pct is not None and (
                config.BASIS_DISCOUNT_MAX_PCT
                <= float(basis_pct)
                <= config.BASIS_PREMIUM_MAX_PCT
            ):
                score += 0.05

        # ── Layer D: Execution quality (0.0 to 0.20) ─────────────────────────
        if isinstance(layer_d, dict):
            # Test format: dict with spread_pct, depth_ratio, imbalance
            spread_pct  = layer_d.get("spread_pct",  None)
            depth_ratio = layer_d.get("depth_ratio", None)
            imbalance   = layer_d.get("imbalance",   None)

            if spread_pct is not None and float(spread_pct) < config.MAX_SPREAD_PCT * 0.5:
                score += 0.10
            if depth_ratio is not None and float(depth_ratio) >= config.MIN_DEPTH_MULTIPLE * 2:
                score += 0.05
            if imbalance is not None and float(imbalance) < 0.3:
                score += 0.05

        elif isinstance(layer_d, (tuple, list)) and len(layer_d) >= 2:
            # Real format: (bool passed, reason_str)
            # Parse spread and imbalance from the Layer D OK reason string.
            # Award base depth score when depth check has already passed.
            d_reason = layer_d[1] or ""
            spread_match    = re.search(r"spread=([\d.]+)%",    d_reason)
            imbalance_match = re.search(r"imbalance=([\d.]+)", d_reason)

            if spread_match:
                spread_pct = float(spread_match.group(1))
                if spread_pct < config.MAX_SPREAD_PCT * 0.5:
                    score += 0.10

            # Depth passed hard check (MIN_DEPTH_MULTIPLE) — award base score
            score += 0.05

            if imbalance_match:
                imbalance = float(imbalance_match.group(1))
                if imbalance < 0.3:
                    score += 0.05

        return max(0.0, min(1.0, score))

    # ── Position sizing ───────────────────────────────────────────────────────

    def size_position(
        self,
        pair: str,
        regime: str,
        size_multiplier: float,
        portfolio_value_usd: float,
        conviction_score: float = 1.0,
    ) -> dict:
        """
        Calculate order size and ATR-based stop / take-profit distances.

        size_multiplier:  float from Layer E (0.25, 0.5, or 1.0).
        conviction_score: 0.0–1.0 from calculate_conviction_score() — scales
                          the final order size down when signal quality is weak.

        Returns dict with keys:
          order_size_usd, stop_distance_usd, take_profit_distance_usd,
          atr, regime_multiplier, conviction_score, conviction_label,
          conviction_multiplier, reason
        """
        # ── Regime multiplier ─────────────────────────────────────────────────
        regime_multiplier = _REGIME_MULTIPLIERS.get(regime, 0.0)

        # ── Conviction multiplier ─────────────────────────────────────────────
        if conviction_score >= config.CONVICTION_FULL_THRESHOLD:
            conviction_multiplier = config.CONVICTION_SIZE_MULTIPLIERS["full"]
            conviction_label = "full"
        elif conviction_score >= config.CONVICTION_HIGH_THRESHOLD:
            conviction_multiplier = config.CONVICTION_SIZE_MULTIPLIERS["high"]
            conviction_label = "high"
        elif conviction_score >= config.CONVICTION_MEDIUM_THRESHOLD:
            conviction_multiplier = config.CONVICTION_SIZE_MULTIPLIERS["medium"]
            conviction_label = "medium"
        else:
            conviction_multiplier = config.CONVICTION_SIZE_MULTIPLIERS["low"]
            conviction_label = "low"

        # ── Dynamic daily loss cap ────────────────────────────────────────────
        if config.PORTFOLIO_SCALE_ENABLED:
            dynamic_daily_cap = round(
                portfolio_value_usd * config.PORTFOLIO_DAILY_CAP_PCT, 2
            )
        else:
            dynamic_daily_cap = config.DAILY_LOSS_CAP_USD

        # ── SOL-specific overrides ────────────────────────────────────────────
        # SOL is more volatile than BTC/ETH — use wider stops/TP and smaller size
        _is_sol = pair in ("SOL-USDT-SWAP", "SOL/USDT:USDT")
        _position_pct  = config.SOL_MAX_POSITION_PCT    if _is_sol else config.MAX_POSITION_PCT
        _atr_stop_mult = config.SOL_ATR_STOP_MULTIPLIER if _is_sol else config.ATR_STOP_MULTIPLIER
        _atr_tp_mult   = config.SOL_ATR_TP_MULTIPLIER   if _is_sol else config.ATR_TP_MULTIPLIER

        # ── Order size ────────────────────────────────────────────────────────
        base_size = portfolio_value_usd * _position_pct
        raw_size = base_size * regime_multiplier * size_multiplier * conviction_multiplier
        # Never exceed base_size regardless of multipliers
        order_size_usd = round(min(raw_size, base_size), 2)

        # ── Min / max caps ────────────────────────────────────────────────────
        if order_size_usd < config.PORTFOLIO_MIN_TRADE_USD:
            # Too small to trade — return 0 so decision_loop REJECTs it
            order_size_usd = 0.0
        else:
            order_size_usd = min(order_size_usd, config.PORTFOLIO_MAX_TRADE_USD)

        # ── ATR-based stop and take-profit distances ──────────────────────────
        atr = self._get_atr(pair)

        if atr is None:
            # Flat 2% of order size as fallback
            stop_distance = round(order_size_usd * 0.02, 2)
            tp_distance = round(
                stop_distance * _atr_tp_mult / _atr_stop_mult, 2
            )
            reason = (
                f"WARNING: ATR unavailable, using 2% flat fallback "
                f"(stop={stop_distance:.2f}, tp={tp_distance:.2f}), "
                f"conviction={conviction_label} ({conviction_score:.2f})"
                + (f", SOL overrides applied" if _is_sol else "")
            )
            print(f"[risk_engine] {pair}: {reason}", file=sys.stderr)
        else:
            stop_distance = round(atr * _atr_stop_mult, 2)
            tp_distance = round(atr * _atr_tp_mult, 2)
            reason = (
                f"atr={atr:.4f}, "
                f"stop={stop_distance:.2f}, "
                f"tp={tp_distance:.2f}, "
                f"regime_mult={regime_multiplier}, "
                f"event_mult={size_multiplier}, "
                f"conviction={conviction_label} ({conviction_score:.2f})"
                + (f", SOL overrides (stop_mult={_atr_stop_mult}, tp_mult={_atr_tp_mult}, pos_pct={_position_pct})" if _is_sol else "")
            )

        return {
            "order_size_usd":             order_size_usd,
            "stop_distance_usd":          stop_distance,
            "take_profit_distance_usd":   tp_distance,
            "atr":                        atr,
            "regime_multiplier":          regime_multiplier,
            "conviction_score":           conviction_score,
            "conviction_label":           conviction_label,
            "conviction_multiplier":      conviction_multiplier,
            "dynamic_daily_cap":          dynamic_daily_cap,
            "portfolio_value":            portfolio_value_usd,
            "reason":                     reason,
        }

    # ── Entry prices ─────────────────────────────────────────────────────────

    def calculate_entry_prices(
        self,
        pair: str,
        signal: str,
        entry_price: float,
        stop_distance_usd: float,
        take_profit_distance_usd: float,
    ) -> dict:
        """
        Calculate stop-loss and take-profit levels from the entry price.

        stop_distance_usd / take_profit_distance_usd are price-distance values
        in USD (output of size_position, typically ATR-derived).

        Returns dict with keys:
          entry, stop_loss, take_profit, rr_ratio
        """
        if signal == "LONG":
            stop_loss = round(entry_price - stop_distance_usd, 2)
            take_profit = round(entry_price + take_profit_distance_usd, 2)
        else:  # SHORT
            stop_loss = round(entry_price + stop_distance_usd, 2)
            take_profit = round(entry_price - take_profit_distance_usd, 2)

        rr_ratio = (
            round(take_profit_distance_usd / stop_distance_usd, 4)
            if stop_distance_usd > 0
            else 0.0
        )

        return {
            "entry": round(entry_price, 2),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "rr_ratio": rr_ratio,
        }

    # ── Daily loss cap ────────────────────────────────────────────────────────

    def check_daily_cap(
        self,
        daily_pnl_usd: float,
        portfolio_value_usd: float = None,
    ) -> bool:
        """
        Return True if the daily loss cap has been hit.

        If portfolio_value_usd is supplied and PORTFOLIO_SCALE_ENABLED:
          cap = portfolio_value_usd * PORTFOLIO_DAILY_CAP_PCT  (dynamic)
        Else:
          cap = DAILY_LOSS_CAP_USD  (fixed config value)

        True = trading halted for remainder of the session.
        """
        if portfolio_value_usd is not None and config.PORTFOLIO_SCALE_ENABLED:
            cap = portfolio_value_usd * config.PORTFOLIO_DAILY_CAP_PCT
        else:
            cap = config.DAILY_LOSS_CAP_USD
        return abs(daily_pnl_usd) >= cap

    # ── Portfolio value ───────────────────────────────────────────────────────

    def get_portfolio_value(self, order_manager=None) -> float:
        """
        Return current portfolio value in USD.

        If PORTFOLIO_SCALE_ENABLED and order_manager is provided:
          Calls order_manager.get_portfolio_value() (live OKX balance).
          On any failure, logs a warning and falls back to STARTING_CAPITAL_USD.
        Otherwise: returns config.STARTING_CAPITAL_USD.
        """
        if config.PORTFOLIO_SCALE_ENABLED and order_manager is not None:
            try:
                value = order_manager.get_portfolio_value()
                return value
            except Exception as exc:
                print(
                    f"[risk_engine] get_portfolio_value failed: {exc} — "
                    f"falling back to STARTING_CAPITAL_USD={config.STARTING_CAPITAL_USD}",
                    file=sys.stderr,
                )
                return config.STARTING_CAPITAL_USD
        return config.STARTING_CAPITAL_USD
