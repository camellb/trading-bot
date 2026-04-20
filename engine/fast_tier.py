"""
Fast-tier event-driven re-evaluation — skeleton.

The slow tier (pm_analyst scan loop) discovers markets and evaluates them
on a 5-minute cadence. The fast tier monitors already-known markets for
price moves and news triggers, re-evaluating only when conditions change.

This is a skeleton — full implementation requires:
  * Polymarket CLOB WebSocket for real-time price streaming
  * News stream listener with keyword matching
  * Cached research bundles for delta-only re-evaluation

Current implementation: Gamma API polling for price-move detection on
watched markets (open positions + recently evaluated markets).

Future: Replace polling with CLOB WebSocket for sub-second latency.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text

import config
from db.engine import get_engine


@dataclass
class WatchedMarket:
    """A market being monitored for price changes."""
    market_id:        str
    question:         str
    last_yes_price:   float
    last_checked:     datetime
    claude_probability: float  # from most recent evaluation
    position_id:      Optional[int] = None  # if we have an open position


@dataclass
class PriceAlert:
    """Fired when a watched market's price moves significantly."""
    market_id:   str
    old_price:   float
    new_price:   float
    move_bps:    float
    has_position: bool
    timestamp:   datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FastTierMonitor:
    """
    Monitors watched markets for price moves via Gamma API polling.

    This is the polling-based implementation. When CLOB WebSocket
    credentials are available, replace _poll_prices() with a
    WebSocket handler for real-time updates.
    """

    def __init__(
        self,
        move_threshold_bps: float = 200.0,  # 2pp move triggers alert
        poll_interval_seconds: float = 30.0,
    ):
        self.move_threshold_bps = move_threshold_bps
        self.poll_interval_seconds = poll_interval_seconds
        self._watched: dict[str, WatchedMarket] = {}
        self._alerts: list[PriceAlert] = []
        self._running = False

    def add_watch(self, market_id: str, question: str, yes_price: float,
                  claude_probability: float, position_id: Optional[int] = None):
        """Add or update a watched market."""
        self._watched[market_id] = WatchedMarket(
            market_id=market_id,
            question=question,
            last_yes_price=yes_price,
            last_checked=datetime.now(timezone.utc),
            claude_probability=claude_probability,
            position_id=position_id,
        )

    def remove_watch(self, market_id: str):
        """Stop watching a market (e.g., after settlement)."""
        self._watched.pop(market_id, None)

    def get_alerts(self, clear: bool = True) -> list[PriceAlert]:
        """Return and optionally clear pending alerts."""
        alerts = list(self._alerts)
        if clear:
            self._alerts.clear()
        return alerts

    def get_watched_count(self) -> int:
        return len(self._watched)

    async def poll_once(self):
        """
        Check all watched markets for price moves via Gamma API.

        For each market where price moved > threshold, generate a PriceAlert.
        """
        if not self._watched:
            return

        try:
            from feeds.polymarket_feed import PolymarketFeed
            async with PolymarketFeed() as feed:
                market_ids = list(self._watched.keys())
                raw_markets = await feed.fetch_many(market_ids)

                now = datetime.now(timezone.utc)
                for mid, raw in raw_markets.items():
                    watched = self._watched.get(mid)
                    if not watched:
                        continue

                    prices = raw.get("outcomePrices")
                    if not prices:
                        continue

                    import json
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    if not isinstance(prices, list) or len(prices) < 1:
                        continue

                    new_price = float(prices[0])
                    old_price = watched.last_yes_price
                    move_bps = abs(new_price - old_price) * 10_000

                    if move_bps >= self.move_threshold_bps:
                        alert = PriceAlert(
                            market_id=mid,
                            old_price=old_price,
                            new_price=new_price,
                            move_bps=move_bps,
                            has_position=watched.position_id is not None,
                        )
                        self._alerts.append(alert)
                        print(
                            f"[fast_tier] price alert: {watched.question[:50]} "
                            f"{old_price:.3f} → {new_price:.3f} "
                            f"({move_bps:.0f}bps)",
                            flush=True,
                        )

                    # Update tracked price
                    watched.last_yes_price = new_price
                    watched.last_checked = now

        except Exception as exc:
            print(f"[fast_tier] poll error: {exc}", file=sys.stderr)

    async def run_loop(self):
        """
        Background polling loop. Call as an asyncio task.

        In production, replace this with a CLOB WebSocket connection
        for real-time price streaming.
        """
        self._running = True
        print(f"[fast_tier] started monitoring {len(self._watched)} markets "
              f"(poll every {self.poll_interval_seconds}s)", flush=True)

        while self._running:
            await self.poll_once()
            await asyncio.sleep(self.poll_interval_seconds)

    def stop(self):
        self._running = False

    def load_open_positions(self):
        """
        Load all open positions as watched markets.
        Called at startup to begin monitoring.
        """
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text(
                    "SELECT id, market_id, question, entry_price, "
                    "       claude_probability, side "
                    "FROM pm_positions "
                    "WHERE status = 'open' "
                    "ORDER BY created_at DESC"
                )).fetchall()

                for row in rows:
                    pos_id = int(row[0])
                    market_id = str(row[1])
                    question = str(row[2] or "")
                    entry_price = float(row[3])
                    claude_p = float(row[4]) if row[4] else 0.5
                    side = str(row[5])

                    # Reconstruct YES price from entry
                    yes_price = entry_price if side == "YES" else (1.0 - entry_price)

                    self.add_watch(
                        market_id=market_id,
                        question=question,
                        yes_price=yes_price,
                        claude_probability=claude_p,
                        position_id=pos_id,
                    )

                print(f"[fast_tier] loaded {len(rows)} open positions for monitoring",
                      flush=True)
        except Exception as exc:
            print(f"[fast_tier] load_open_positions failed: {exc}",
                  file=sys.stderr)

    def get_status(self) -> dict:
        """Dashboard-friendly status."""
        return {
            "watched_markets": len(self._watched),
            "pending_alerts": len(self._alerts),
            "running": self._running,
            "markets": [
                {
                    "market_id": w.market_id,
                    "question": w.question[:80],
                    "last_price": w.last_yes_price,
                    "claude_p": w.claude_probability,
                    "has_position": w.position_id is not None,
                    "last_checked": w.last_checked.isoformat(),
                }
                for w in list(self._watched.values())[:20]  # cap for API response size
            ],
        }
