"""
Book Manager — thin wrapper over OKXWebSocketManager that exposes clean
order book accessors for Layer D (execution filter).

All methods return None or sentinel values (e.g. 999.0 for slippage) when
the order book is not available, so callers can gate safely.
"""

from typing import Optional

from feeds.okx_ws import OKXWebSocketManager


class BookManager:
    """Provides spread, depth, imbalance, and slippage estimates from the live book."""

    def __init__(self, ws_manager: OKXWebSocketManager) -> None:
        self._ws = ws_manager

    def get_spread_pct(self, pair: str) -> Optional[float]:
        """
        Best ask minus best bid divided by best ask, expressed as a percentage.
        Returns None if the order book is unavailable or empty.
        """
        ob = self._ws.get_orderbook(pair)
        if not ob or not ob["bids"] or not ob["asks"]:
            return None
        best_bid = max(ob["bids"].keys())
        best_ask = min(ob["asks"].keys())
        if best_ask == 0:
            return None
        return (best_ask - best_bid) / best_ask * 100.0

    def get_near_touch_depth(
        self, pair: str, side: str, levels: int = 5
    ) -> float:
        """
        Sum of qty * price for the top N price levels on the given side.
        side: "bids" or "asks"
        Returns total USD depth. Returns 0.0 if book unavailable.
        """
        ob = self._ws.get_orderbook(pair)
        if not ob or not ob.get(side):
            return 0.0

        if side == "bids":
            # Top N bids = highest prices
            sorted_levels = sorted(ob["bids"].items(), key=lambda x: -x[0])
        else:
            # Top N asks = lowest prices
            sorted_levels = sorted(ob["asks"].items(), key=lambda x: x[0])

        total = 0.0
        for price, qty in sorted_levels[:levels]:
            total += price * qty
        return total

    def get_ob_imbalance(self, pair: str, levels: int = 10) -> float:
        """
        Bid depth / (bid depth + ask depth) for the top N levels on each side.
        Returns a value 0.0–1.0:
          > 0.5 = bid-heavy (buy pressure)
          < 0.5 = ask-heavy (sell pressure)
        Returns 0.5 (neutral) if book unavailable.
        """
        ob = self._ws.get_orderbook(pair)
        if not ob or not ob["bids"] or not ob["asks"]:
            return 0.5

        bid_levels = sorted(ob["bids"].items(), key=lambda x: -x[0])[:levels]
        ask_levels = sorted(ob["asks"].items(), key=lambda x: x[0])[:levels]

        bid_depth = sum(p * q for p, q in bid_levels)
        ask_depth = sum(p * q for p, q in ask_levels)

        total = bid_depth + ask_depth
        if total == 0:
            return 0.5
        return bid_depth / total

    def estimate_slippage_pct(
        self, pair: str, side: str, order_size_usd: float
    ) -> float:
        """
        Walk the order book on the given side to estimate average fill price
        for a market order of order_size_usd.

        side: "bids" (for sells) or "asks" (for buys)
        Returns slippage as a percentage versus the best price.
        Returns 999.0 if book depth is insufficient — this will trigger a
        Layer D block (value exceeds config.MAX_SLIPPAGE_PCT).
        """
        ob = self._ws.get_orderbook(pair)
        if not ob or not ob.get(side):
            return 999.0

        if side == "asks":
            # Buying — walk up the ask side
            levels = sorted(ob["asks"].items(), key=lambda x: x[0])
            best_price = levels[0][0] if levels else None
        else:
            # Selling — walk down the bid side
            levels = sorted(ob["bids"].items(), key=lambda x: -x[0])
            best_price = levels[0][0] if levels else None

        if not best_price or best_price == 0:
            return 999.0

        remaining = order_size_usd
        filled_cost = 0.0
        filled_qty = 0.0

        for price, qty in levels:
            level_value = price * qty
            if remaining <= level_value:
                # Partially fill this level
                partial_qty = remaining / price
                filled_cost += remaining
                filled_qty += partial_qty
                remaining = 0.0
                break
            else:
                filled_cost += level_value
                filled_qty += qty
                remaining -= level_value

        if remaining > 0 or filled_qty == 0:
            # Not enough depth
            return 999.0

        avg_fill_price = filled_cost / filled_qty
        slippage = abs(avg_fill_price - best_price) / best_price * 100.0
        return slippage
