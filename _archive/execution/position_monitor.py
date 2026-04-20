"""
Position Monitor — continuously watches all open positions.

Closes positions on: stop-loss triggered, take-profit reached, or signal flip
(regime/directional bias reversal every 5 minutes).

Also requests thesis reviews from Claude every 4 hours per position, and
immediately on large losses or critical news events.

Checks run every 5 seconds using mark price from the WebSocket ticker.
Stop-loss and take-profit use mark price, not last trade price, to avoid
liquidation surprises.

Updates the trades and daily_pnl tables via db/logger.py on every state change.
Position state is held in-memory and reloaded from the trades table on startup,
so the max-positions gate is accurate across bot restarts.
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

import config
from db import logger as db_logger
from engine.regime_classifier import RegimeClassifier
from feeds.okx_ws import OKXWebSocketManager
from feeds.feed_health_monitor import FeedHealthMonitor

if TYPE_CHECKING:
    from engine.decision_loop import DecisionLoop
    from engine.strategist import Strategist
    from execution.order_manager import OrderManager

# Seconds between mark-price checks
_CHECK_INTERVAL_S = 5

# Seconds between signal-flip evaluations (avoid hammering regime classifier)
_SIGNAL_CHECK_INTERVAL_S = 300  # 5 minutes

# How often to poll the thesis review schedule
_THESIS_POLL_INTERVAL_S = 900   # 15 minutes

# Unrealised loss fraction that triggers an urgent Claude review
_URGENT_LOSS_THRESHOLD = -0.08  # −8 % of position size

# Auto-generated post-mortems for mechanical exits
_EXIT_REASON_LABELS: dict[str, str] = {
    "stop_loss":              "Stop loss triggered — price moved against the thesis.",
    "take_profit":            "Take profit reached — thesis played out as expected.",
    "signal_flip_no_trade":   "Regime shifted to NO_TRADE — exiting to reduce risk.",
    "signal_flip_short":      "Trend flipped bearish — exiting long position.",
    "signal_flip_long":       "Trend flipped bullish — exiting short position.",
}


class PositionMonitor:
    """
    Monitors all open positions and closes them when exit conditions are met.

    Position state:
      self._positions: dict[trade_id (int) → position dict]

    Position dict keys:
      pair, direction ('LONG'/'SHORT'), entry_price, size_usd,
      stop_loss, take_profit, open_at, paper, order_id,
      last_signal_check (datetime)
    """

    def __init__(
        self,
        order_manager: "OrderManager",
        ws_manager: OKXWebSocketManager,
        decision_loop: Optional["DecisionLoop"] = None,
        health_monitor: Optional[FeedHealthMonitor] = None,
        regime_classifier: Optional[RegimeClassifier] = None,
    ) -> None:
        self._om = order_manager
        self._ws = ws_manager
        self._dl = decision_loop        # optional; not used in monitoring loop directly
        self._monitor = health_monitor
        self._rc = regime_classifier
        self._positions: dict[int, dict] = {}
        self._closing:   set[int] = set()   # trade_ids currently being closed (race guard)

        # Thesis-driven exit support
        self._strategist: Optional["Strategist"] = None
        self._last_thesis_review: dict[int, datetime] = {}
        self._thesis_review_interval = timedelta(hours=4)

        # Set True when load_open_trades() raises at startup.
        # Blocks all new ENTERs via Strategist._enforce_risk_limits() until
        # reconcile_with_exchange() completes successfully.
        self._db_load_failed: bool = False

        # Set True when a ghost position's exit price cannot be recovered.
        # Blocks /resume until operator confirms via /reconciled.
        self._reconciliation_pending: bool = False

    def set_decision_loop(self, decision_loop: "DecisionLoop") -> None:
        """Wire in decision_loop after it has been constructed (breaks circular dep)."""
        self._dl = decision_loop

    def set_strategist(self, strategist: "Strategist") -> None:
        """Wire in Strategist after both objects are constructed (breaks circular dep)."""
        self._strategist = strategist

    # ── Position registration ─────────────────────────────────────────────────

    def register_position(self, trade: dict) -> None:
        """
        Register a newly filled trade for immediate monitoring.

        Called by Strategist._execute_enter right after place_order() succeeds.
        trade dict must contain: id, pair, direction, entry_price, size_usd,
        stop_loss, take_profit, timestamp_open, paper.
        Same field names as load_open_trades() rows so startup reload and
        live registration use identical code paths.
        """
        trade_id = trade["id"]
        self._positions[trade_id] = {
            "pair":              trade["pair"],
            "direction":         trade["direction"],
            "entry_price":       trade["entry_price"],
            "size_usd":          trade["size_usd"],
            "stop_loss":         trade["stop_loss"],
            "take_profit":       trade["take_profit"],
            "open_at":           trade["timestamp_open"],
            "paper":             trade["paper"],
            "order_id":          None,
            "last_signal_check": datetime.now(timezone.utc),
            "filled_qty":            trade.get("filled_qty"),
            "client_order_id":       trade.get("client_order_id"),
            "close_client_order_id": trade.get("close_client_order_id"),
        }
        print(
            f"[position_monitor] REGISTERED trade_id={trade_id} "
            f"{trade['pair']} {trade['direction']} "
            f"entry={trade['entry_price']:.2f} "
            f"stop={trade['stop_loss']:.2f} "
            f"tp={trade['take_profit']:.2f}",
            flush=True,
        )

    # ── Startup position reload ───────────────────────────────────────────────

    def _load_existing_positions(self) -> None:
        """
        Reload open positions from the DB into self._positions on startup.

        Prevents the max-positions gate from reading 0 after a bot restart:
        get_position_count() returns len(self._positions), so without this
        reload the gate always passes even when DB has open trades.

        Fields that cannot be recovered from the DB (order_id) are set to None.
        last_signal_check is reset to now() so the first signal-flip check
        happens after the normal _SIGNAL_CHECK_INTERVAL_S delay.
        """
        try:
            open_trades = db_logger.load_open_trades(config.PAPER_MODE)
        except Exception as exc:
            print(
                f"[position_monitor] CRITICAL: load_open_trades failed: {exc}\n"
                f"Position state unknown — new ENTERs blocked until reconciliation.",
                file=sys.stderr,
            )
            self._db_load_failed = True
            return
        if not open_trades:
            return

        now = datetime.now(timezone.utc)
        for row in open_trades:
            trade_id = row["id"]
            self._positions[trade_id] = {
                "pair":               row["pair"],
                "direction":          row["direction"],
                "entry_price":        row["entry_price"],
                "size_usd":           row["size_usd"],
                "stop_loss":          row["stop_loss"],
                "take_profit":        row["take_profit"],
                "open_at":            row["timestamp_open"],
                "paper":              row["paper"],
                "order_id":           None,   # not persisted — live order mgmt unaffected
                "last_signal_check":  now,
                "filled_qty":            row.get("filled_qty"),
                "client_order_id":       row.get("client_order_id"),
                "close_client_order_id": row.get("close_client_order_id"),
            }

        print(
            f"[position_monitor] Reloaded {len(open_trades)} open position(s) from DB: "
            + ", ".join(
                f"trade_id={r['id']} {r['pair']} {r['direction']}"
                for r in open_trades
            ),
            flush=True,
        )

    async def reconcile_with_exchange(self, order_manager) -> bool:
        """
        Compare DB positions against live OKX positions at startup.

        Three outcomes per pair:
          Clean:   DB and OKX agree — log info and continue.
          Orphaned (on OKX but not in DB): bot crashed between fill and DB log.
            → Set _trading_halted=True + CRITICAL Telegram.  Return False.
          Ghost    (in DB but not on OKX): position closed on exchange without DB update.
            → Attempt to match exit price from OKX closed orders (clientOrderId or
              timestamp+side). If matched: close DB with 'reconciliation_recovered'.
              If unmatched: set reconciliation_pending=True + halt.
              Remove from _positions.  Send warning Telegram.

        Returns True if state is clean (or only ghosts found), False if orphaned
        positions were detected (trading halted in that case).
        """
        loop = asyncio.get_running_loop()
        try:
            live_positions = await loop.run_in_executor(
                None, order_manager.get_live_positions
            )
        except Exception as exc:
            # Exchange state unknown — cannot safely determine if positions are orphaned.
            # Treating an empty response as "no positions" would ghost any real OKX position.
            # Halt trading and keep _db_load_failed=True so ENTERs remain blocked.
            print(
                f"[position_monitor] CRITICAL: reconcile_with_exchange: "
                f"get_live_positions failed: {exc}\n"
                f"Cannot verify exchange state at startup — trading halted.",
                file=sys.stderr,
            )
            order_manager._trading_halted = True
            # _db_load_failed remains True from _load_existing_positions()
            msg = (
                "🚨 <b>Startup reconciliation failed</b>\n"
                "OKX position fetch failed at startup. Cannot verify exchange state.\n"
                "Trading halted until /resume (after manually confirming OKX positions)."
            )
            if self._strategist is not None:
                try:
                    await self._strategist.notifier.send(msg)
                except Exception:
                    pass
            return False

        db_pairs  = {pos["pair"] for pos in self._positions.values()}
        live_pairs = {lp["pair"] for lp in live_positions}

        orphaned = live_pairs - db_pairs   # on OKX, not in DB
        ghosts   = db_pairs - live_pairs   # in DB, not on OKX

        # _db_load_failed is cleared only when both position reconciliation
        # and the open-order audit below pass cleanly.
        result = True

        if not orphaned and not ghosts:
            print("[position_monitor] Startup reconciliation: clean — DB matches exchange", flush=True)
            self._db_load_failed = False
            # fall through to open-order audit — do NOT return early here

        if orphaned:
            msg = (
                f"🚨 <b>CRITICAL: Orphaned positions at startup</b>\n"
                f"On OKX but not in DB: {', '.join(sorted(orphaned))}\n"
                f"Bot may have crashed between fill and DB log.\n"
                f"Trading halted. Check OKX and DB manually, then /resume."
            )
            print(f"[position_monitor] CRITICAL: {msg}", file=sys.stderr)
            order_manager._trading_halted = True
            self._db_load_failed = True
            if self._strategist is not None:
                try:
                    await self._strategist.notifier.send(msg)
                except Exception as notify_exc:
                    print(
                        f"[position_monitor] notifier send failed: {notify_exc}",
                        file=sys.stderr,
                    )
            result = False

        for pair in ghosts:
            ghost_ids = [
                tid for tid, pos in self._positions.items()
                if pos["pair"] == pair
            ]
            for trade_id in ghost_ids:
                position = self._positions[trade_id]
                print(
                    f"[position_monitor] Ghost position: trade_id={trade_id} {pair} "
                    f"— in DB but not on OKX. Attempting exit price recovery.",
                    file=sys.stderr,
                )

                # ── Two-attempt exit price recovery ──────────────────────────
                ghost_entry     = float(position.get("entry_price", 0) or 0)
                ghost_dir       = position.get("direction", "LONG")
                ghost_opened_at = position.get("open_at")               # datetime or None
                # close_client_order_id is written to DB BEFORE the close order is
                # submitted, so it survives a crash between close-fill and DB-update.
                # client_order_id is the ENTRY order ID — never use it to find the exit.
                ghost_close_id  = position.get("close_client_order_id") # str or None
                ccxt_sym        = (
                    f"{pair.split('-')[0]}/{pair.split('-')[1]}"
                    f":{pair.split('-')[1]}"
                )
                close_side      = "sell" if ghost_dir == "LONG" else "buy"
                matched_price: float | None = None
                matched_order: dict | None = None

                # Attempt 1: exact match by close clientOrderId + reduceOnly guard
                # Only reliable when close_client_order_id was written before the crash.
                if ghost_close_id and not config.PAPER_MODE:
                    try:
                        closed = order_manager._exchange.fetch_closed_orders(
                            ccxt_sym, limit=20
                        )
                        for o in (closed or []):
                            o_info     = o.get("info", {}) or {}
                            is_reduce  = o.get("reduceOnly", False)
                            id_matches = (
                                o.get("clientOrderId") == ghost_close_id
                                or o_info.get("clOrdId") == ghost_close_id
                            )
                            # Require reduceOnly=True: guards against accidentally
                            # matching an entry order that shares the same client ID.
                            if id_matches and is_reduce:
                                price = float(o.get("average") or o.get("price") or 0)
                                if price > 0:
                                    matched_price = price
                                    matched_order = o
                                    break
                    except Exception as exc:
                        print(
                            f"[position_monitor] ghost close_client_order_id match failed for {pair}: {exc}",
                            file=sys.stderr,
                        )

                # Attempt 2: match by timestamp + reduce-only + correct side
                if matched_price is None and ghost_opened_at and not config.PAPER_MODE:
                    try:
                        since = int(ghost_opened_at.timestamp() * 1000)
                        closed = order_manager._exchange.fetch_closed_orders(
                            ccxt_sym, since=since, limit=10
                        )
                        for o in (closed or []):
                            is_reduce  = o.get("reduceOnly", False)
                            is_side_ok = o.get("side") == close_side
                            price      = float(o.get("average") or o.get("price") or 0)
                            if is_reduce and is_side_ok and price > 0:
                                matched_price = price
                                matched_order = o
                                break
                    except Exception as exc:
                        print(
                            f"[position_monitor] ghost timestamp match failed for {pair}: {exc}",
                            file=sys.stderr,
                        )

                if matched_price is not None and matched_price > 0:
                    # Extract the exchange-reported close timestamp so daily_pnl lands
                    # on the correct UTC date (trade may have closed just before midnight).
                    actual_close_dt: datetime | None = None
                    if matched_order:
                        ts_ms = matched_order.get("timestamp")
                        if ts_ms:
                            try:
                                actual_close_dt = datetime.fromtimestamp(
                                    int(ts_ms) / 1000, tz=timezone.utc
                                )
                            except Exception:
                                pass
                    close_dt = actual_close_dt or datetime.now(timezone.utc)

                    # Matched — compute real P&L and close in DB cleanly
                    size = float(position.get("size_usd", 0) or 0)
                    if ghost_dir == "LONG":
                        actual_pnl = (matched_price - ghost_entry) / ghost_entry * size
                    else:
                        actual_pnl = (ghost_entry - matched_price) / ghost_entry * size
                    db_logger.log_trade_close(
                        trade_id, matched_price, actual_pnl, "reconciliation_recovered",
                        timestamp_close=close_dt,
                    )
                    # Repair daily_pnl aggregate using the actual close date, not now().
                    # Prevents mis-attribution when reconciliation runs after UTC midnight.
                    db_logger.upsert_daily_pnl(
                        close_dt.date(),
                        actual_pnl,
                        config.PAPER_MODE,
                    )
                    db_logger.log_reconciliation_record({
                        "pair":        pair,
                        "direction":   ghost_dir,
                        "entry_price": ghost_entry,
                        "exit_price":  matched_price,
                        "filled_qty":  position.get("filled_qty"),
                        "pnl_usd":     actual_pnl,
                        "status":      "ghost_reconciled",
                        "trade_id":    trade_id,
                        "notes":       "Exit price recovered via clientOrderId or timestamp match at startup",
                    })
                    del self._positions[trade_id]
                    print(
                        f"[position_monitor] Ghost {pair} recovered: "
                        f"exit={matched_price:.4f} pnl={actual_pnl:+.4f}",
                        flush=True,
                    )
                    if self._strategist is not None:
                        try:
                            await self._strategist.notifier.send(
                                f"⚠️ Ghost position reconciled at startup: {pair}\n"
                                f"trade_id={trade_id} — recovered exit={matched_price:.4f}, "
                                f"pnl={actual_pnl:+.4f} USD"
                            )
                        except Exception:
                            pass
                else:
                    # No reliable match — mark DB, halt, require manual reconciliation
                    db_logger.update_trade_reconciliation_pending(trade_id, True)
                    db_logger.log_reconciliation_record({
                        "pair":      pair,
                        "direction": ghost_dir,
                        "trade_id":  trade_id,
                        "status":    "ghost_pending",
                        "notes":     "Exit price could not be safely matched. Manual reconciliation required.",
                    })
                    self._positions[trade_id]["ghost_pending"] = True
                    self._reconciliation_pending = True
                    order_manager._trading_halted = True
                    result = False
                    print(
                        f"[position_monitor] CRITICAL: Ghost {pair} trade_id={trade_id} "
                        f"— exit price unrecoverable. Trading halted.",
                        file=sys.stderr,
                    )
                    if self._strategist is not None:
                        try:
                            await self._strategist.notifier.send(
                                f"🚨 <b>GHOST TRADE: Cannot safely reconcile {pair}</b>\n"
                                f"Trade ID {trade_id} — exit price unknown.\n"
                                f"Do NOT assume zero loss. Check OKX trade history.\n"
                                f"Send /reconciled after manually verifying exit price."
                            )
                        except Exception:
                            pass

        # ── Open-order audit ─────────────────────────────────────────────────
        # A resting entry order from a previous crash could fill after restart,
        # creating an untracked position.  Cancel unexpected non-protective orders.
        try:
            open_orders = await loop.run_in_executor(
                None, order_manager.get_live_open_orders
            )
        except Exception as exc:
            print(
                f"[position_monitor] reconcile_with_exchange: get_live_open_orders failed: {exc}",
                file=sys.stderr,
            )
            open_orders = None

        if open_orders is None:
            # Cannot verify whether leftover resting orders exist — halt.
            msg = (
                "🚨 <b>Startup: Cannot fetch open orders from OKX</b>\n"
                "Trading halted — leftover orders from previous session cannot be verified.\n"
                "Check OKX manually and send /resume when clear."
            )
            print(
                f"[position_monitor] CRITICAL: {msg}",
                file=sys.stderr,
            )
            order_manager._trading_halted = True
            self._db_load_failed = True
            if self._strategist is not None:
                try:
                    await self._strategist.notifier.send(msg)
                except Exception:
                    pass
            return False
        elif open_orders:
            # Flag any non-protective resting entry orders
            unexpected = [
                o for o in open_orders
                if not o.get("reduceOnly", False)
                and o.get("type") in ("limit", "market")
            ]
            if unexpected:
                pairs = {o.get("symbol", "?") for o in unexpected}
                msg = (
                    f"🚨 <b>Unexpected open orders at startup</b>\n"
                    f"Symbols: {', '.join(sorted(pairs))}\n"
                    f"{len(unexpected)} non-protective order(s) may be from a previous crash.\n"
                    f"Cancel them manually on OKX, then send /resume."
                )
                print(f"[position_monitor] CRITICAL: {msg}", file=sys.stderr)
                order_manager._trading_halted = True
                self._db_load_failed = True
                if self._strategist is not None:
                    try:
                        await self._strategist.notifier.send(msg)
                    except Exception:
                        pass
                result = False
            else:
                print(
                    f"[position_monitor] Open orders at startup: "
                    f"{len(open_orders)} protective order(s) only — OK",
                    flush=True,
                )

        return result

    # ── Main monitoring loop ──────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Run position monitoring. Two concurrent loops:
          - _price_monitor_loop: stop/TP/signal-flip checks every 5 s
          - _thesis_review_loop: 4H Claude thesis reviews, polled every 15 min

        In live mode, reconcile_with_exchange() is called after DB reload to
        catch any positions that existed on OKX at startup but were not in the
        DB (orphaned) or vice-versa (ghost).
        """
        self._load_existing_positions()
        if not config.PAPER_MODE and self._om is not None:
            await self.reconcile_with_exchange(self._om)
        print("[position_monitor] Started", flush=True)
        await asyncio.gather(
            self._price_monitor_loop(),
            self._thesis_review_loop(),
        )

    async def _price_monitor_loop(self) -> None:
        """Check stop-loss, take-profit, and signal flips every 5 seconds."""
        while True:
            # Skip checks when core feeds are stale — never close on bad data
            if (
                self._monitor is not None
                and not self._monitor.are_core_feeds_healthy()
            ):
                await asyncio.sleep(_CHECK_INTERVAL_S)
                continue
            for trade_id in list(self._positions.keys()):
                position = self._positions.get(trade_id)
                if position:
                    await self._check_position(trade_id, position)
            await asyncio.sleep(_CHECK_INTERVAL_S)

    async def _thesis_review_loop(self) -> None:
        """
        Every 15 minutes, check whether any open position is due for a 4H thesis
        review. Triggers a Claude make_decision() call with trigger_type=THESIS_REVIEW.
        """
        while True:
            await asyncio.sleep(_THESIS_POLL_INTERVAL_S)
            if self._strategist is None:
                continue
            now = datetime.now(timezone.utc)
            for trade_id in list(self._positions.keys()):
                position = self._positions.get(trade_id)
                if not position:
                    continue
                last = self._last_thesis_review.get(
                    trade_id,
                    datetime.min.replace(tzinfo=timezone.utc),
                )
                if (now - last) >= self._thesis_review_interval:
                    self._last_thesis_review[trade_id] = now
                    await self._request_thesis_review(trade_id, position)

    async def _request_thesis_review(self, trade_id: int, position: dict) -> None:
        """
        Build a focused briefing for a single position and ask Claude whether
        the thesis still holds. Claude may respond with EXIT, ADJUST, or HOLD.
        """
        if self._strategist is None:
            return

        pair   = position["pair"]
        ticker = self._ws.get_latest_ticker(pair)
        briefing = {
            "trigger_type":  "THESIS_REVIEW",
            "trigger_detail": (
                f"4-hour thesis review — {position['direction']} {pair} "
                f"(trade_id={trade_id}). "
                f"Entry: {position['entry_price']:.2f}, "
                f"Stop: {position['stop_loss']:.2f}, "
                f"Target: {position['take_profit']:.2f}"
            ),
            "pair": pair,
            "price": {
                "mark_price":   ticker.get("mark_price") if ticker else None,
                "funding_rate": ticker.get("funding_rate") if ticker else None,
            },
            "open_positions": self.get_open_positions(),
        }
        print(
            f"[position_monitor] Thesis review: trade_id={trade_id} "
            f"{position['direction']} {pair}",
            flush=True,
        )
        try:
            await self._strategist.make_decision(briefing)
        except Exception as exc:
            print(
                f"[position_monitor] Thesis review error trade_id={trade_id}: {exc}",
                file=sys.stderr,
            )

    async def trigger_urgent_review(self, trade_id: int, reason: str) -> None:
        """
        Immediately ask Claude to review a specific position.
        Called by the Scanner on large losses or critical news events.
        """
        if self._strategist is None:
            return
        position = self._positions.get(trade_id)
        if not position:
            return

        pair   = position["pair"]
        ticker = self._ws.get_latest_ticker(pair)
        briefing = {
            "trigger_type":  "URGENT_REVIEW",
            "trigger_detail": reason,
            "pair":          pair,
            "price": {
                "mark_price":   ticker.get("mark_price") if ticker else None,
                "funding_rate": ticker.get("funding_rate") if ticker else None,
            },
            "open_positions": self.get_open_positions(),
        }
        print(
            f"[position_monitor] Urgent review: trade_id={trade_id} "
            f"reason={reason[:80]}",
            flush=True,
        )
        try:
            await self._strategist.make_decision(briefing)
        except Exception as exc:
            print(
                f"[position_monitor] Urgent review error trade_id={trade_id}: {exc}",
                file=sys.stderr,
            )

    async def _check_position(self, trade_id: int, position: dict) -> None:
        """
        Check a single open position against all exit conditions.
        Uses mark price from the WebSocket ticker for stop/TP checks.
        """
        pair = position["pair"]
        ticker = self._ws.get_latest_ticker(pair)

        if not ticker:
            return  # Never close on missing data

        current_price = ticker.get("mark_price")
        if not current_price or current_price <= 0:
            return

        direction = position["direction"]

        # ── Stop-loss check ───────────────────────────────────────────────────
        if direction == "LONG" and current_price <= position["stop_loss"]:
            await self._close_position(trade_id, current_price, "stop_loss")
            return
        if direction == "SHORT" and current_price >= position["stop_loss"]:
            await self._close_position(trade_id, current_price, "stop_loss")
            return

        # ── Take-profit check ─────────────────────────────────────────────────
        if direction == "LONG" and current_price >= position["take_profit"]:
            await self._close_position(trade_id, current_price, "take_profit")
            return
        if direction == "SHORT" and current_price <= position["take_profit"]:
            await self._close_position(trade_id, current_price, "take_profit")
            return

        # ── Signal flip check (every 5 minutes) ──────────────────────────────
        now = datetime.now(timezone.utc)
        elapsed = (now - position["last_signal_check"]).total_seconds()
        if elapsed >= _SIGNAL_CHECK_INTERVAL_S and self._rc is not None:
            position["last_signal_check"] = now
            try:
                regime = self._rc.classify(pair)
                if regime["regime"] == "NO_TRADE":
                    await self._close_position(trade_id, current_price, "signal_flip_no_trade")
                    return
                # Use EMA slope (ma_slope) as a proxy for direction flip
                ma_slope = regime.get("ma_slope", 0.0) or 0.0
                if direction == "LONG" and ma_slope < 0 and regime["regime"].startswith("TREND_DOWN"):
                    await self._close_position(trade_id, current_price, "signal_flip_short")
                    return
                if direction == "SHORT" and ma_slope > 0 and regime["regime"].startswith("TREND_UP"):
                    await self._close_position(trade_id, current_price, "signal_flip_long")
                    return
            except Exception as exc:
                print(
                    f"[position_monitor] signal flip check error trade_id={trade_id}: {exc}",
                    file=sys.stderr,
                )

    # ── Position close ────────────────────────────────────────────────────────

    async def _close_position(
        self,
        trade_id: int,
        exit_price: float,
        reason: str,
    ) -> dict:
        """
        Close a position and persist results to the database.

        Paper mode: calculates P&L from price difference.
        Live mode:  delegates to OrderManager.close_position() which cancels
                    pending SL/TP orders, places a market close, then polls to
                    confirm the position is flat.  DB is only updated after the
                    exchange confirms flat.

        Re-entrant guard: self._closing prevents duplicate concurrent closes.

        Returns:
          {"success": True,  "exit_price": float, "pnl": float}
          {"success": False, "reason": str}
        """
        if trade_id in self._closing:
            return {"success": False, "reason": "already_closing"}
        if trade_id not in self._positions:
            return {"success": False, "reason": "trade_not_found"}

        self._closing.add(trade_id)
        result: dict = {"success": False, "reason": "unknown"}
        try:
            position    = self._positions[trade_id]
            direction   = position["direction"]
            entry_price = position["entry_price"]
            size_usd    = position["size_usd"]
            pair        = position["pair"]
            paper       = position["paper"]
            filled_qty  = position.get("filled_qty")

            # P&L calculated after actual exit price is confirmed (see below)
            pnl = 0.0

            # ── Live: close via OrderManager (CCXT symbol conversion handled there)
            if not config.PAPER_MODE:
                close_result = await self._om.close_position(
                    pair, direction, size_usd, entry_price, filled_qty,
                    trade_id=trade_id,
                )
                if close_result["status"] != "filled":
                    err = close_result.get("error", "exchange_close_failed")
                    print(
                        f"[position_monitor] CRITICAL: live close failed "
                        f"trade_id={trade_id} status={close_result['status']}: {err}",
                        file=sys.stderr,
                    )
                    # position_not_flat: trading is already halted in OrderManager;
                    # send Telegram via strategist notifier if available.
                    if (
                        close_result["status"] == "position_not_flat"
                        and self._strategist is not None
                    ):
                        try:
                            await self._strategist.notifier.send(
                                f"🚨 <b>Position not flat</b>\n"
                                f"trade_id={trade_id} {pair} {direction}\n{err}"
                            )
                        except Exception as notify_exc:
                            print(
                                f"[position_monitor] notifier send failed: {notify_exc}",
                                file=sys.stderr,
                            )
                    result = {"success": False, "reason": err}
                    return result  # DB not updated — position still live on exchange

                actual_price = close_result.get("exit_price") or 0.0
                price_unknown = (
                    actual_price <= 0
                    or close_result.get("price_reconciliation_required", False)
                )
                if not price_unknown:
                    exit_price = actual_price
                else:
                    # Fill price unrecoverable from exchange — mark close_reason with
                    # sentinel so the P&L kill-switch query excludes this trade, and
                    # halt trading until the operator manually reconciles on OKX.
                    print(
                        f"[position_monitor] CRITICAL: exit price unknown for "
                        f"trade_id={trade_id} — mark price {exit_price:.4f} used as "
                        f"estimate. Trade excluded from daily P&L cap until reconciled.",
                        file=sys.stderr,
                    )
                    reason = f"price_recon_pending:{reason}"

            # ── Recalculate P&L with confirmed exit price ─────────────────────
            if direction == "LONG":
                pnl = (exit_price - entry_price) / entry_price * size_usd
            else:
                pnl = (entry_price - exit_price) / entry_price * size_usd

            # ── Persist to DB ─────────────────────────────────────────────────
            db_ok = db_logger.log_trade_close(trade_id, exit_price, pnl, reason)
            if not db_ok:
                # DB write failed — keep position in memory so restart doesn't lose it
                print(
                    f"[position_monitor] CRITICAL: DB close failed trade_id={trade_id}",
                    file=sys.stderr,
                )
                if self._strategist is not None:
                    try:
                        await self._strategist.notifier.send(
                            f"🚨 <b>DB FAILURE: Could not log close</b>\n"
                            f"trade_id={trade_id} {pair} {direction}\n"
                            f"Position may reappear after restart. Check DB manually."
                        )
                    except Exception:
                        pass
                # Flag for manual reconciliation; do NOT remove from _positions
                self._positions[trade_id]["db_close_failed"] = True
                result = {"success": False, "reason": "db_close_failed"}
                return result

            # ── Price reconciliation pending — durable halt, skip daily P&L ────
            if reason.startswith("price_recon_pending:"):
                # Persist flag so /resume is blocked even after a restart.
                db_logger.update_trade_reconciliation_pending(trade_id, True)
                db_logger.log_reconciliation_record({
                    "pair":      pair,
                    "direction": direction,
                    "trade_id":  trade_id,
                    "status":    "price_recon_pending",
                    "notes":     f"Exit price unrecoverable at close. close_reason={reason}",
                })
                self._om._trading_halted = True
                msg = (
                    f"🚨 <b>Exit price unrecoverable — trade {trade_id}</b>\n"
                    f"{pair} {direction} closed but fill price not confirmed.\n"
                    f"P&L estimate ({pnl:+.4f} USD) recorded in trades table but "
                    f"<b>excluded from daily loss cap</b> until reconciled.\n"
                    f"Check OKX trade history for actual fill price, update DB, "
                    f"then /resume."
                )
                if self._strategist is not None:
                    try:
                        await self._strategist.notifier.send(msg)
                    except Exception:
                        pass
                # Write Obsidian note so post-mortem flags the estimate
                if self._strategist is not None:
                    try:
                        self._strategist.memory.write_trade_exit(
                            {
                                "id":          trade_id,
                                "pair":        pair,
                                "direction":   direction,
                                "entry_price": entry_price,
                                "exit_price":  exit_price,
                                "pnl_usd":     pnl,
                                "opened_at":   position.get("open_at"),
                                "closed_at":   datetime.now(timezone.utc),
                            },
                            reason,
                            "Exit price could not be recovered from exchange. "
                            "P&L figure uses mark price as estimate — excluded from "
                            "daily loss cap. Manual reconciliation required.",
                        )
                    except Exception:
                        pass
                # Remove from tracking (position is flat on exchange) but do NOT
                # call upsert_daily_pnl — the estimated P&L must not affect the cap.
                del self._positions[trade_id]
                result = {"success": True, "exit_price": exit_price, "pnl": pnl,
                          "price_recon_pending": True}
                return result

            pnl_ok = db_logger.upsert_daily_pnl(
                datetime.now(timezone.utc).date(), pnl, paper
            )
            if not pnl_ok:
                if self._strategist is not None:
                    try:
                        await self._strategist.notifier.send(
                            f"⚠️ Daily P&L update failed for trade {trade_id}\n"
                            f"Daily loss cap may be inaccurate today."
                        )
                    except Exception:
                        pass

            # ── Obsidian post-mortem (mechanical exits only) ──────────────────
            # Claude-driven exits (reason starts with "CLAUDE: ") are written by
            # Strategist._execute_exit after this returns — skip to avoid duplicates.
            if not reason.startswith("CLAUDE: ") and self._strategist is not None:
                post_mortem = _EXIT_REASON_LABELS.get(reason, f"Exit reason: {reason}")
                try:
                    self._strategist.memory.write_trade_exit(
                        {
                            "id":          trade_id,
                            "pair":        pair,
                            "direction":   direction,
                            "entry_price": entry_price,
                            "exit_price":  exit_price,
                            "pnl_usd":     pnl,
                            "opened_at":   position.get("open_at"),
                            "closed_at":   datetime.now(timezone.utc),
                        },
                        reason,
                        post_mortem,
                    )
                except Exception as exc:
                    print(
                        f"[position_monitor] Obsidian write error "
                        f"trade_id={trade_id}: {exc}",
                        file=sys.stderr,
                    )

            # ── Resolve calibration prediction ────────────────────────────────
            # Best-effort — failure must never block the close path.
            try:
                import calibration
                calibration.resolve_prediction_by_trade(
                    trade_id = trade_id,
                    outcome  = 1 if pnl > 0 else 0,
                    pnl_usd  = pnl,
                    note     = reason,
                )
            except Exception as _exc:
                print(f"[position_monitor] calibration resolve skipped: {_exc}",
                      file=sys.stderr)

            # ── Remove from in-memory state ───────────────────────────────────
            del self._positions[trade_id]

            # ── Console summary ───────────────────────────────────────────────
            mode_tag = "PAPER" if paper else "LIVE"
            print(
                f"[position_monitor] {mode_tag} CLOSE trade_id={trade_id} "
                f"{pair} {direction} "
                f"entry={entry_price:.2f} exit={exit_price:.2f} "
                f"pnl={pnl:+.4f} USD reason={reason}",
                flush=True,
            )
            result = {"success": True, "exit_price": exit_price, "pnl": pnl,
                      "filled_qty": filled_qty}
            return result

        finally:
            self._closing.discard(trade_id)


    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        """
        Return a list of all open positions with current unrealised P&L.
        P&L is estimated from the latest mark price.
        """
        result = []
        for trade_id, pos in self._positions.items():
            pair = pos["pair"]
            ticker = self._ws.get_latest_ticker(pair)
            current_price = ticker.get("mark_price", pos["entry_price"]) if ticker else pos["entry_price"]

            if pos["direction"] == "LONG":
                unrealised_pnl = (current_price - pos["entry_price"]) / pos["entry_price"] * pos["size_usd"]
            else:
                unrealised_pnl = (pos["entry_price"] - current_price) / pos["entry_price"] * pos["size_usd"]

            result.append({
                "trade_id": trade_id,
                **pos,
                "current_price": current_price,
                "unrealised_pnl": round(unrealised_pnl, 4),
            })
        return result

    def get_position_count(self) -> int:
        """Return the number of currently open positions."""
        return len(self._positions)
