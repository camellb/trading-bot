"""
Layer D — Execution Filter (Hard Pre-Trade Contract).

ALL five conditions must be simultaneously True or the order is blocked.
This is a hard boolean gate — no weighted scoring, no partial pass, no override.

Conditions:
  1. Spread < config.MAX_SPREAD_PCT
  2. Estimated slippage at order size < config.MAX_SLIPPAGE_PCT
  3. Near-touch book depth > config.MIN_DEPTH_MULTIPLE * intended order size
  4. Order book contra-directional imbalance < config.MAX_OB_CONTRA_IMBALANCE
  5. All exchange feeds healthy (age < config.API_HEARTBEAT_MAX_AGE_S)

Imbalance rule detail:
  get_ob_imbalance() returns bid_depth / (bid_depth + ask_depth).
  LONG  → contra side is asks. Block if bid_imbalance < (1 - MAX_OB_CONTRA_IMBALANCE)
           i.e. asks dominate more than MAX_OB_CONTRA_IMBALANCE (default 60%) of the book.
  SHORT → contra side is bids. Block if bid_imbalance > MAX_OB_CONTRA_IMBALANCE
           i.e. bids dominate more than MAX_OB_CONTRA_IMBALANCE (default 60%) of the book.

Returns a (go: bool, reason: str) tuple on every evaluation.
"""

import config
from feeds.book_manager import BookManager
from feeds.feed_health_monitor import FeedHealthMonitor


class ExecutionFilter:
    """
    Layer D: hard pre-trade execution gate.

    ALL five conditions must be True simultaneously — any single failure blocks
    the trade. Returns (go: bool, reason: str).
    """

    def __init__(
        self,
        book_manager: BookManager,
        health_monitor: FeedHealthMonitor,
    ) -> None:
        self._book = book_manager
        self._monitor = health_monitor

    def evaluate(
        self,
        pair: str,
        signal: str,
        intended_order_size_usd: float,
    ) -> tuple[bool, str]:
        """
        Evaluate all 5 Layer D conditions.

        pair: trading pair (e.g. 'BTC/USDT:USDT')
        signal: 'LONG' or 'SHORT'
        intended_order_size_usd: USD notional of the intended order

        Returns (go: bool, reason: str).
        """
        failures: list[str] = []

        # ── 1. Spread check ───────────────────────────────────────────────────
        spread_pct = self._book.get_spread_pct(pair)
        if spread_pct is None:
            failures.append("spread unavailable (order book not ready)")
        elif spread_pct >= config.MAX_SPREAD_PCT:
            failures.append(
                f"spread={spread_pct:.4f}% >= {config.MAX_SPREAD_PCT}%"
            )

        # ── 2. Slippage check ─────────────────────────────────────────────────
        # LONG = buying into ask side; SHORT = selling into bid side
        slip_side = "asks" if signal == "LONG" else "bids"
        slippage_pct = self._book.estimate_slippage_pct(
            pair, slip_side, intended_order_size_usd
        )
        if slippage_pct >= config.MAX_SLIPPAGE_PCT:
            failures.append(
                f"slippage={slippage_pct:.4f}% >= {config.MAX_SLIPPAGE_PCT}%"
            )

        # ── 3. Near-touch depth check ─────────────────────────────────────────
        depth_side = "asks" if signal == "LONG" else "bids"
        near_depth_usd = self._book.get_near_touch_depth(pair, depth_side)
        min_required_usd = config.MIN_DEPTH_MULTIPLE * intended_order_size_usd
        if near_depth_usd <= min_required_usd:
            failures.append(
                f"near-depth={near_depth_usd:.0f}USD <= "
                f"{min_required_usd:.0f}USD "
                f"({config.MIN_DEPTH_MULTIPLE}× order size)"
            )

        # ── 4. Order book imbalance check ─────────────────────────────────────
        # imbalance = bid_depth / (bid + ask); range 0.0–1.0
        imbalance = self._book.get_ob_imbalance(pair)
        if signal == "LONG":
            # For LONG: asks must not dominate > MAX_OB_CONTRA_IMBALANCE of book
            min_bid_ratio = 1.0 - config.MAX_OB_CONTRA_IMBALANCE
            if imbalance < min_bid_ratio:
                failures.append(
                    f"ask-side dominance too high for LONG "
                    f"(bid_imbalance={imbalance:.3f} < {min_bid_ratio:.3f})"
                )
        elif signal == "SHORT":
            # For SHORT: bids must not dominate > MAX_OB_CONTRA_IMBALANCE of book
            if imbalance > config.MAX_OB_CONTRA_IMBALANCE:
                failures.append(
                    f"bid-side dominance too high for SHORT "
                    f"(bid_imbalance={imbalance:.3f} > {config.MAX_OB_CONTRA_IMBALANCE:.3f})"
                )

        # ── 5. Core feed health check ─────────────────────────────────────────
        if not self._monitor.are_core_feeds_healthy():
            degraded = self._monitor.get_degraded_feeds()
            failures.append(f"core feeds degraded: {degraded}")

        # ── Result ────────────────────────────────────────────────────────────
        if failures:
            return False, "Layer D BLOCK: " + "; ".join(failures)

        spread_str = f"{spread_pct:.4f}%" if spread_pct is not None else "N/A"
        return True, (
            f"Layer D OK: spread={spread_str}, "
            f"slippage={slippage_pct:.4f}%, "
            f"depth={near_depth_usd:.0f}USD, "
            f"imbalance={imbalance:.3f}, "
            f"feeds=healthy"
        )
