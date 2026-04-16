"""
MockWSManager — a drop-in replacement for OKXWebSocketManager for backtesting.

Instead of connecting to live WebSocket streams, it serves pre-loaded historical
candle data time-sliced to a configurable "current time" pointer.

MockHealthMonitor is also defined here — always reports all feeds healthy so
the engine layers never block on feed health checks during backtesting.

Usage:
    from backtester.mock_ws_manager import MockWSManager, MockHealthMonitor

    # historical_data: {pair: {interval: [candle_dicts]}}
    # candle_dicts must be sorted oldest-first
    mock_ws = MockWSManager(historical_data)
    mock_ws.set_current_time(some_datetime)

    closed = mock_ws.get_closed_candles("BTC-USDT-SWAP", "15m")
    ticker = mock_ws.get_latest_ticker("BTC-USDT-SWAP")
    ob     = mock_ws.get_orderbook("BTC-USDT-SWAP")
"""

from datetime import datetime, timezone
from typing import Optional


# ── MockHealthMonitor ─────────────────────────────────────────────────────────

class MockHealthMonitor:
    """
    Stub FeedHealthMonitor for backtesting.
    Always reports all feeds as healthy; ignores all state changes.
    """

    def register(self, name: str) -> None:
        pass

    def report_healthy(self, name: str) -> None:
        pass

    def report_reconnecting(self, name: str) -> None:
        pass

    def report_degraded(self, name: str, detail: str = "") -> None:
        pass

    def are_core_feeds_healthy(self) -> bool:
        return True

    def get_degraded_feeds(self) -> list[str]:
        return []

    def get_last_mass_degradation_ts(self):
        return None


# ── MockWSManager ─────────────────────────────────────────────────────────────

class MockWSManager:
    """
    Provides the same interface as OKXWebSocketManager but reads from
    pre-loaded historical data.

    Interface methods (matching OKXWebSocketManager):
      get_closed_candles(pair, interval) → list[dict]
      get_latest_ticker(pair)            → dict | None
      get_orderbook(pair)                → dict | None

    `set_current_time(dt)` advances the time pointer; only candles with
    open_time < current_ts_ms are returned.

    The synthetic ticker uses the most recent closed candle's close price
    as mark_price, index_price, bid, ask, and last.

    The synthetic order book always passes Layer D:
      - Very tight spread (0.01%)
      - Very large depth (1 000 000 USD per level × 20 levels per side)
      - Neutral imbalance (50/50 bid vs ask)
    This ensures execution filter never blocks in backtesting — we want to
    see how often the signal layers themselves generate entries.
    """

    def __init__(self, historical_data: dict[str, dict[str, list[dict]]]) -> None:
        """
        Args:
            historical_data: nested dict {pair: {interval: [candle_dicts]}}
                             candle_dicts must be sorted oldest-first.
        """
        self._data = historical_data
        self._current_ts_ms: int = 0

    def set_current_time(self, dt: datetime) -> None:
        """
        Advance the time pointer to `dt`.  All data accessor methods will
        only return candles with open_time strictly less than this timestamp.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self._current_ts_ms = int(dt.timestamp() * 1000)

    # ── Data accessors ────────────────────────────────────────────────────────

    def get_closed_candles(self, pair: str, interval: str) -> list[dict]:
        """
        Return all closed candles for pair/interval with open_time < current_ts.
        Returns up to 200 candles (matches live buffer size).
        """
        candles = self._data.get(pair, {}).get(interval, [])
        visible = [c for c in candles if c["open_time"] < self._current_ts_ms]
        return visible[-200:]  # cap to match live _MAX_CANDLES

    def get_latest_ticker(self, pair: str) -> Optional[dict]:
        """
        Return a synthetic ticker dict for `pair` based on the most recent
        closed 15m candle.  Returns None if no candles are visible yet.

        Keys returned match the live OKXWebSocketManager ticker:
          mark_price, index_price, funding_rate, last, bid, ask,
          bid_size, ask_size, timestamp, next_funding_time
        """
        candles = self.get_closed_candles(pair, "15m")
        if not candles:
            return None

        last_close = candles[-1]["close"]
        spread = last_close * 0.0001       # 0.01% spread (tight)
        bid = round(last_close - spread / 2, 6)
        ask = round(last_close + spread / 2, 6)

        return {
            "mark_price":        last_close,
            "index_price":       last_close,   # no basis in backtest
            "funding_rate":      0.0001,        # neutral funding rate
            "last":              last_close,
            "bid":               bid,
            "ask":               ask,
            "bid_size":          10000.0,
            "ask_size":          10000.0,
            "timestamp":         candles[-1]["open_time"],
            "next_funding_time": 0,
        }

    def get_orderbook(self, pair: str) -> Optional[dict]:
        """
        Return a synthetic deep order book that always passes Layer D.

        Characteristics:
          - Spread: ~0.01% (well below MAX_SPREAD_PCT = 0.05%)
          - Depth: 1 000 000 USD per level × 20 levels each side
          - Imbalance: exactly 0.5 (perfectly balanced)
        """
        candles = self.get_closed_candles(pair, "15m")
        if not candles:
            return None

        price = candles[-1]["close"]
        depth_qty = 1_000_000.0 / price    # contracts at this price level

        bids: dict[float, float] = {}
        asks: dict[float, float] = {}

        # 20 levels on each side, spacing 0.005% per level
        for i in range(1, 21):
            bid_px = round(price * (1 - 0.00005 * i), 6)
            ask_px = round(price * (1 + 0.00005 * i), 6)
            bids[bid_px] = depth_qty
            asks[ask_px] = depth_qty

        return {
            "bids":   bids,
            "asks":   asks,
            "seq_id": 1,
            "ready":  True,
        }
