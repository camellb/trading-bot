"""
Decision Loop — orchestrates all signal layers in sequence.

Revised layer sequence (M4/M5):
  Kill switch check (NEW in M5 — before any layer)
  Step 1: Layer A — Regime classifier
  Step 2: Layer E — Event overlay (async Claude call)
  Step 3: Layer B — Directional bias
  Step 4: Layer C — Crypto confirmation
  Step 5: Layer F partial — size_position() to get order_size_usd
  Step 6: Layer D — Execution filter (uses real order size)
  Step 7: Layer F complete — daily cap check + calculate_entry_prices()
  Step 8: Max positions gate (NEW in M5)
  Step 9: Place order + register position (NEW in M5)
  Return TRADE

Any failure returns a REJECT dict immediately. Every cycle result
(TRADE or REJECT) is written to the ticks table via db.logger.log_signal().

The start() method drives the loop from confirmed closed 15m bars.
It runs continuously alongside position_monitor.start() in asyncio.gather().
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from sqlalchemy import create_engine, select

import config
from db import logger as db_logger
from db.models import daily_pnl
from engine.crypto_confirmation import CryptoConfirmation
from engine.directional_bias import DirectionalBias
from engine.event_overlay import EventOverlay
from engine.execution_filter import ExecutionFilter
from engine.regime_classifier import RegimeClassifier
from engine.risk_engine import RiskEngine
from feeds.okx_ws import OKXWebSocketManager
from feeds.feed_health_monitor import FeedHealthMonitor

if TYPE_CHECKING:
    from execution.order_manager import OrderManager
    from execution.position_monitor import PositionMonitor
    from engine.macro_context import MacroContextEngine


class DecisionLoop:
    """
    Orchestrates all signal layers for a single trading pair per cycle.

    run_cycle(pair) is async because Layer E may call the Claude API.
    start(pairs) drives the loop from confirmed closed 15m bars.
    """

    def __init__(
        self,
        ws_manager: OKXWebSocketManager,
        health_monitor: FeedHealthMonitor,
        regime_classifier: RegimeClassifier,
        directional_bias: DirectionalBias,
        crypto_confirmation: CryptoConfirmation,
        execution_filter: ExecutionFilter,
        event_overlay: EventOverlay,
        risk_engine: RiskEngine,
        order_manager: "OrderManager",
        position_monitor: "PositionMonitor",
        macro_context: "MacroContextEngine | None" = None,
    ) -> None:
        self._ws = ws_manager
        self._monitor = health_monitor
        self._rc = regime_classifier
        self._db = directional_bias
        self._cc = crypto_confirmation
        self._ef = execution_filter
        self._eo = event_overlay
        self._re = risk_engine
        self._om = order_manager
        self._pm = position_monitor
        self.macro_context = macro_context

    # ── Kill switch ───────────────────────────────────────────────────────────

    @property
    def kill_switch_active(self) -> bool:
        """
        True when today's P&L has hit or exceeded the daily loss cap.
        Uses dynamic cap (% of live portfolio) when PORTFOLIO_SCALE_ENABLED.
        Checked as the very first step in every cycle.
        """
        portfolio_value = self._re.get_portfolio_value(self._om)
        return self._re.check_daily_cap(self._get_todays_pnl(), portfolio_value)

    # ── Entry point: bar-triggered loop ──────────────────────────────────────

    async def start(self, pairs: list[str]) -> None:
        """
        Drive the decision loop, triggering on each new confirmed closed 15m bar.

        Polls every 30 seconds for a new candle. When a new candle close_time
        is detected for a pair, run_cycle() fires for that pair.

        Warns at startup if the kill switch is already active (previous session
        losses may have carried over — the date filter in _get_todays_pnl()
        guards this correctly since it queries today's date only).
        """
        if self.kill_switch_active:
            print(
                "[decision_loop] WARNING: kill switch active at startup — "
                "daily loss cap already hit for today. All cycles will REJECT.",
                flush=True,
            )

        # Seed last-seen candle close_time per pair (0 = never seen)
        last_candle_ct: dict[str, int] = {pair: 0 for pair in pairs}

        # Wait for feeds to warm up
        await asyncio.sleep(30)
        print("[decision_loop] Started — polling for closed 15m bars every 30s", flush=True)

        while True:
            for pair in pairs:
                candles = self._ws.get_closed_candles(pair, "15m")
                if not candles:
                    continue
                latest_ct = candles[-1]["close_time"]
                if latest_ct > last_candle_ct[pair]:
                    last_candle_ct[pair] = latest_ct
                    result = await self.run_cycle(pair)
                    print(self.format_result(result), flush=True)

            await asyncio.sleep(30)

    # ── Main cycle ────────────────────────────────────────────────────────────

    async def run_cycle(self, pair: str) -> dict:
        """
        Run one full decision cycle for the given pair.

        Returns a result dict (TRADE or REJECT). Always logged to ticks table.
        """
        regime_data: dict = {"regime": "NO_TRADE"}
        event_state: dict = {"size_multiplier": 1.0, "regime_override": None,
                             "blocked": False, "reason": ""}
        layer_b: Optional[dict] = None
        layer_c: Optional[dict] = None
        layer_d: tuple = (None, None)
        sizing: Optional[dict] = None

        # ── Kill switch — first check before any layer ────────────────────────
        if self.kill_switch_active:
            daily_pnl_usd = self._get_todays_pnl()
            portfolio_value_ks = self._re.get_portfolio_value(self._om)
            cap_ks = (
                portfolio_value_ks * config.PORTFOLIO_DAILY_CAP_PCT
                if config.PORTFOLIO_SCALE_ENABLED
                else config.DAILY_LOSS_CAP_USD
            )
            # Telegram alert (deduped to once per day inside notifier)
            try:
                from feeds.telegram_notifier import notifier
                await notifier.notify_kill_switch(daily_pnl_usd)
            except Exception:
                pass
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                f"KILL SWITCH: daily loss cap hit "
                f"(USD {daily_pnl_usd:.2f} >= {cap_ks:.2f})",
            )

        # ── Step 1: Layer A — Regime ──────────────────────────────────────────
        regime_data = self._rc.classify(pair)
        if regime_data["regime"] == "NO_TRADE":
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                f"Layer A: {regime_data['reason']}",
            )

        # ── Step 2: Layer E — Event overlay ───────────────────────────────────
        event_state = await self._eo.evaluate(pair)
        if event_state["blocked"]:
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                f"Layer E: {event_state['reason']}",
            )

        # ── Step 3: Layer B — Directional bias ────────────────────────────────
        layer_b = self._db.evaluate(pair)
        if layer_b["signal"] == "NEUTRAL":
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                f"Layer B: {layer_b['reason']}",
            )

        # ── Step 4: Layer C — Crypto confirmation ─────────────────────────────
        layer_c = self._cc.evaluate(pair, layer_b["signal"])
        if not layer_c["confirmed"]:
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                f"Layer C: {layer_c['reason']}",
            )

        # ── Step 5: Layer F partial — size_position ───────────────────────────
        portfolio_value = self._re.get_portfolio_value(self._om)
        sizing = self._re.size_position(
            pair,
            regime_data["regime"],
            event_state["size_multiplier"],
            portfolio_value,
        )
        # Apply daily macro context multiplier (0.5–1.0, default 1.0)
        if self.macro_context is not None:
            macro_multiplier = self.macro_context.get_risk_multiplier()
        else:
            macro_multiplier = 1.0
        sizing["order_size_usd"] = round(
            sizing["order_size_usd"] * macro_multiplier, 2
        )
        sizing["macro_multiplier"] = macro_multiplier

        # Apply Deribit IV multiplier (1.0 = no change, 0.70 = high IV, 0.35 = extreme)
        iv_multiplier = float(regime_data.get("iv_multiplier", 1.0))
        if iv_multiplier < 1.0:
            sizing["order_size_usd"] = round(
                sizing["order_size_usd"] * iv_multiplier, 2
            )

        if sizing["order_size_usd"] <= 0:
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                f"Layer F: zero size after multipliers "
                f"(regime_mult={sizing['regime_multiplier']}, "
                f"event_mult={event_state['size_multiplier']})",
            )

        # ── Step 6: Layer D — Execution filter (with real order size) ─────────
        go, d_reason = self._ef.evaluate(pair, layer_b["signal"], sizing["order_size_usd"])
        layer_d = (go, d_reason)
        if not go:
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                d_reason,
            )

        # ── Step 7: Layer F complete — daily cap + conviction sizing + entry prices
        daily_pnl_usd = self._get_todays_pnl()
        if self._re.check_daily_cap(daily_pnl_usd, portfolio_value):
            cap_used = (
                portfolio_value * config.PORTFOLIO_DAILY_CAP_PCT
                if config.PORTFOLIO_SCALE_ENABLED
                else config.DAILY_LOSS_CAP_USD
            )
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                f"Layer F: daily loss cap hit "
                f"(USD {daily_pnl_usd:.2f} >= {cap_used:.2f})",
            )

        # All layers passed — calculate conviction and apply to final sizing
        conviction_score = self._re.calculate_conviction_score({
            "regime_data": regime_data,
            "layer_b":     layer_b,
            "layer_c":     layer_c,
            "layer_d":     layer_d,
            "event_state": event_state,
        })
        sizing = self._re.size_position(
            pair,
            regime_data["regime"],
            event_state["size_multiplier"],
            portfolio_value,
            conviction_score=conviction_score,
        )

        entry_price = self._get_current_price(pair)
        prices = self._re.calculate_entry_prices(
            pair,
            layer_b["signal"],
            entry_price,
            sizing["stop_distance_usd"],
            sizing["take_profit_distance_usd"],
        )

        # ── Step 8: Max simultaneous positions gate ───────────────────────────
        if self._pm.get_position_count() >= config.MAX_SIMULTANEOUS_POSITIONS:
            return self._reject(
                pair, regime_data, layer_b, layer_c, layer_d, event_state,
                "max simultaneous positions reached "
                f"({self._pm.get_position_count()}/{config.MAX_SIMULTANEOUS_POSITIONS})",
            )

        # ── Build TRADE result ────────────────────────────────────────────────
        result = {
            "decision": "TRADE",
            "pair": pair,
            "signal": layer_b["signal"],
            "entry_price": prices["entry"],
            "stop_loss": prices["stop_loss"],
            "take_profit": prices["take_profit"],
            "order_size_usd": sizing["order_size_usd"],
            "rr_ratio": prices["rr_ratio"],
            "regime": regime_data["regime"],
            "adx": regime_data.get("adx"),
            "realized_vol_pct": regime_data.get("realized_vol_pct"),
            "funding_pct": regime_data.get("funding_pct"),
            "oi_delta": regime_data.get("oi_delta"),
            "iv": regime_data.get("iv"),
            "iv_spike": regime_data.get("iv_spike", False),
            "iv_multiplier": regime_data.get("iv_multiplier", 1.0),
            "size_multiplier": event_state["size_multiplier"],
            "regime_multiplier": sizing["regime_multiplier"],
            "macro_multiplier": sizing.get("macro_multiplier", 1.0),
            "conviction_score": sizing["conviction_score"],
            "conviction_label": sizing["conviction_label"],
            "atr": sizing["atr"],
            "layer_b": layer_b,
            "layer_c": layer_c,
            "layer_d": layer_d,
            "event_state": event_state,
            "reject_reason": None,
        }

        # ── Step 9: Place order + register position ───────────────────────────
        try:
            fill_result = self._om.place_order(result)
            if fill_result["status"] == "filled":
                self._pm.register_position(fill_result["trade_id"], result, fill_result)
            else:
                # Order failed (e.g. no API keys) — log but still return TRADE signal
                print(
                    f"[decision_loop] Order placement failed for {pair}: "
                    f"{fill_result.get('error', 'unknown')}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(
                f"[decision_loop] Unexpected error placing order for {pair}: {exc}",
                file=sys.stderr,
            )

        db_logger.log_signal(result)
        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    def _reject(
        self,
        pair: str,
        regime_data: dict,
        layer_b: Optional[dict],
        layer_c: Optional[dict],
        layer_d: tuple,
        event_state: dict,
        reason: str,
    ) -> dict:
        """Build, log, and return a REJECT result dict."""
        result = {
            "decision": "REJECT",
            "pair": pair,
            "regime": regime_data.get("regime", "NO_TRADE"),
            "adx": regime_data.get("adx"),
            "realized_vol_pct": regime_data.get("realized_vol_pct"),
            "funding_pct": regime_data.get("funding_pct"),
            "oi_delta": regime_data.get("oi_delta"),
            "iv": regime_data.get("iv"),
            "iv_spike": regime_data.get("iv_spike", False),
            "iv_multiplier": regime_data.get("iv_multiplier", 1.0),
            "signal": layer_b["signal"] if layer_b else None,
            "layer_b": layer_b,
            "layer_c": layer_c,
            "layer_d": layer_d,
            "event_state": event_state,
            "reject_reason": reason,
            # TRADE-only fields
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "order_size_usd": None,
            "rr_ratio": None,
            "size_multiplier": event_state.get("size_multiplier", 1.0),
            "regime_multiplier": None,
            "macro_multiplier": (
                self.macro_context.get_risk_multiplier()
                if self.macro_context is not None else 1.0
            ),
            "atr": None,
        }
        db_logger.log_signal(result)
        return result

    def _get_current_price(self, pair: str) -> float:
        """
        Mid-price from order book, falling back to mark price from ticker.
        Raises ValueError if both are unavailable (Layer D should have blocked).
        """
        ob = self._ws.get_orderbook(pair)
        if ob and ob.get("bids") and ob.get("asks"):
            best_bid = max(ob["bids"].keys())
            best_ask = min(ob["asks"].keys())
            return (best_bid + best_ask) / 2.0

        ticker = self._ws.get_latest_ticker(pair)
        if ticker and ticker.get("mark_price"):
            return float(ticker["mark_price"])

        raise ValueError(
            f"_get_current_price: no price data for {pair} "
            "(order book and ticker both unavailable — Layer D should have blocked)"
        )

    def _get_todays_pnl(self) -> float:
        """
        Query daily_pnl table for today's date. Returns 0.0 if no row yet.
        Fires a loss-warning Telegram notification (deduped to once/day) when
        today's P&L is worse than -50% of DAILY_LOSS_CAP_USD.
        """
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            return 0.0
        try:
            engine = create_engine(database_url)
            today = datetime.now(timezone.utc).date()
            with engine.begin() as conn:
                result = conn.execute(
                    select(daily_pnl.c.pnl_usd).where(daily_pnl.c.date == today)
                )
                row = result.fetchone()
                pnl = float(row[0]) if row else 0.0
        except Exception as exc:
            print(f"[decision_loop] _get_todays_pnl error: {exc}", file=sys.stderr)
            return 0.0

        # Loss warning — fire once per day when 50% of daily cap is consumed
        if pnl < -(config.DAILY_LOSS_CAP_USD * 0.5):
            try:
                import asyncio
                from feeds.telegram_notifier import notifier
                loop = asyncio.get_running_loop()
                loop.create_task(notifier.notify_loss_warning(pnl))
            except Exception:
                pass

        return pnl

    # ── Formatting ────────────────────────────────────────────────────────────

    @staticmethod
    def format_result(result: dict) -> str:
        """
        Single-line human-readable log format.

        TRADE:  [DECISION] BTC/USDT:USDT: TRADE | signal=LONG | size=USD 25.00 |
                entry=84200.00 | stop=83950.00 | tp=84825.00 | R:R=2.50 | regime=RANGE_BALANCED
        REJECT: [DECISION] BTC/USDT:USDT: REJECT | Layer B: all signals neutral
        """
        pair = result.get("pair", "?")
        decision = result.get("decision", "REJECT")

        if decision == "TRADE":
            conviction = result.get("conviction_label", "?")
            conv_score = result.get("conviction_score", 0.0)
            macro_mult = result.get("macro_multiplier", 1.0)
            macro_str = (
                f" | macro={macro_mult:.2f}" if macro_mult < 1.0 else ""
            )
            iv_val = result.get("iv")
            iv_mult = result.get("iv_multiplier", 1.0)
            iv_spike = result.get("iv_spike", False)
            iv_str = ""
            if iv_val is not None:
                spike_tag = " SPIKE" if iv_spike else ""
                iv_str = f" | iv={iv_val:.1f}{spike_tag}"
                if iv_mult < 1.0:
                    iv_str += f" (x{iv_mult:.2f})"
            return (
                f"[DECISION] {pair}: TRADE | "
                f"signal={result['signal']} | "
                f"size=USD {result['order_size_usd']:.2f} | "
                f"conviction={conviction} ({conv_score:.2f})"
                f"{macro_str}"
                f"{iv_str} | "
                f"entry={result['entry_price']:.2f} | "
                f"stop={result['stop_loss']:.2f} | "
                f"tp={result['take_profit']:.2f} | "
                f"R:R={result['rr_ratio']:.2f} | "
                f"regime={result['regime']}"
            )
        else:
            return (
                f"[DECISION] {pair}: REJECT | "
                f"{result.get('reject_reason', 'unknown reason')}"
            )
