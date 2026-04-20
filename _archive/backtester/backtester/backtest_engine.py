"""
BacktestEngine — replays historical OKX candle data through the signal engine.

Runs the full A→B→C→D→F signal chain on each confirmed 15m closed bar for each
trading pair.  Positions are opened at the close of the signal bar and closed
when a subsequent bar's high or low touches the stop-loss or take-profit level.

Layer E (EventOverlay) is intentionally excluded — historical news/event data
is not available.  size_multiplier is fixed at 1.0 for all entries.

Daily loss cap is enforced per-pair per-day to match live behaviour.

Results are saved to the `backtest_runs`, `backtest_trades`, and
`backtest_signals` DB tables, and a summary is printed to the console.

Usage:
    engine = BacktestEngine(
        pairs=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        start_date=date(2025, 1, 1),
        end_date=date(2025, 2, 1),
        initial_capital=500.0,
    )
    run_id = engine.run()
"""

import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from sqlalchemy import create_engine as sa_create_engine, insert

import config
from backtester.data_fetcher import HistoricalDataFetcher
from backtester.mock_ws_manager import MockWSManager, MockHealthMonitor
from backtester.mock_regime_classifier import MockRegimeClassifier
from db.models import (
    metadata,
    backtest_runs,
    backtest_trades,
    backtest_signals,
    create_all_tables,
)
from engine.directional_bias import DirectionalBias
from engine.crypto_confirmation import CryptoConfirmation
from engine.execution_filter import ExecutionFilter
from engine.risk_engine import RiskEngine
from feeds.book_manager import BookManager

# Extra days of warmup data fetched before start_date so that
# indicator calculations (ADX=30 bars, MACD=40 bars, vol=20+100 bars)
# are fully populated from the first signal bar onward.
# 40 bars × 15m = 10h; we fetch 3 days to be safe regardless of gaps.
_WARMUP_DAYS = 3

# Intervals to load (must include "15m" for signal generation;
# "1m" and "5m" are also loaded so MockWSManager matches live structure)
_INTERVALS = ["15m"]


# ── Position dataclass ────────────────────────────────────────────────────────

@dataclass
class _SimPosition:
    pair: str
    direction: str          # 'LONG' | 'SHORT'
    entry_price: float
    size_usd: float
    stop_loss: float
    take_profit: float
    entry_time: datetime
    regime_at_entry: str
    db_trade_id: Optional[int] = None    # filled after DB insert
    gross_pnl_usd: float = 0.0           # before fees
    fee_usd: float = 0.0                 # entry + exit fee
    pnl_usd: float = 0.0                 # net (gross − fee)
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    close_reason: Optional[str] = None  # 'TP' | 'SL' | 'EOD'


# ── BacktestEngine ────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Runs a full backtest over historical 15m candles.

    Call run() to execute.  Returns a metrics dict that includes 'run_id'.
    All run-specific state is initialised inside run() so the same instance
    can be reused across multiple calls (used by run_sweep.py).
    """

    def __init__(self, notes: str = "") -> None:
        self.notes = notes

        # Run-specific attributes; initialised at the top of run()
        self.pairs: list[str] = []
        self.start_date: Optional[date] = None
        self.end_date: Optional[date] = None
        self.initial_capital: float = 500.0
        self._verbose: bool = True   # overridden by run(verbose=...)

        # DB engine — also ensures backtest tables exist
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL not set")
        self._db = sa_create_engine(db_url)
        create_all_tables()   # idempotent: creates new tables if not yet present

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(
        self,
        pairs: list[str],
        start_date: date,
        end_date: date,
        initial_capital: float = 500.0,
        verbose: bool = True,
        historical_data: Optional[dict] = None,
        use_fast_engine: bool = False,
        prebuilt_cache=None,   # IndicatorCache — skip rebuild if already computed
    ) -> dict:
        """
        Execute the full backtest.  Returns a metrics dict (includes 'run_id').

        Parameters
        ----------
        pairs           : OKX instrument IDs to backtest.
        start_date      : First date of the backtest window.
        end_date        : Last date (inclusive) of the backtest window.
        initial_capital : Starting portfolio value in USD.
        verbose         : When True (default) print per-trade lines.
                          The final results table is always printed.
        historical_data : Pre-fetched candle data dict (pair → interval → list).
                          When provided the API fetch step is skipped.  Pass this
                          from run_sweep.py so all combos share one download.
        use_fast_engine : When True, use IndicatorCache + FastRegimeClassifier
                          instead of the full mock signal chain.  Layers C and D
                          are skipped (confirmed/passed assumed True).  Reduces
                          per-combination time from ~15 min to ~seconds.
                          Default False keeps the full production-equivalent path
                          used by run_backtest.py.

        Steps:
          1. Fetch historical candles (with warmup period) — or use pre-fetched
          2a. Build mock components  (use_fast_engine=False)
          2b. Build IndicatorCache   (use_fast_engine=True)
          3. Replay each 15m bar chronologically
          4. Force-close any open positions at end-of-data
          5. Calculate metrics
          6. Save results to DB
          7. Print report to console
        """
        # ── Initialise per-run state ──────────────────────────────────────────
        self.pairs           = pairs
        self.start_date      = start_date
        self.end_date        = end_date
        self.initial_capital = initial_capital
        self._verbose        = verbose

        self._portfolio_value: float             = initial_capital
        self._open_positions: list[_SimPosition] = []
        self._closed_trades: list[_SimPosition]  = []
        self._signal_log: list[dict]             = []
        self._daily_pnl: dict[str, float]        = defaultdict(float)
        self._daily_loss: dict[tuple, float]     = defaultdict(float)
        self._total_bars: int                    = 0
        self._no_trade_bars: int                 = 0

        if self._verbose:
            print(
                f"\n{'='*60}\n"
                f"  BACKTEST: {', '.join(self.pairs)}\n"
                f"  Period  : {self.start_date} → {self.end_date}\n"
                f"  Capital : ${self.initial_capital:,.2f}\n"
                f"{'='*60}\n"
            )

        # ── Step 1: Fetch data (or use pre-fetched) ───────────────────────────
        fetch_start = self.start_date - timedelta(days=_WARMUP_DAYS)
        if historical_data is None:
            fetcher = HistoricalDataFetcher()
            historical_data = {}
            for pair in self.pairs:
                historical_data[pair] = {}
                for interval in _INTERVALS:
                    historical_data[pair][interval] = fetcher.fetch_historical_candles(
                        pair, interval, fetch_start, self.end_date
                    )

        # ── Step 2a/2b: Build signal components ──────────────────────────────
        if use_fast_engine:
            from backtester.indicator_cache import IndicatorCache
            from backtester.fast_regime_classifier import FastRegimeClassifier
            if prebuilt_cache is not None:
                _cache = prebuilt_cache   # reuse across sweep combinations
            else:
                _cache = IndicatorCache()
                for pair in self.pairs:
                    n = _cache.build_cache(pair, historical_data[pair]["15m"])
                    if self._verbose:
                        print(f"[backtest] Building indicator cache for {pair}..."
                              f" done ({n} rows)")
            _fast_clf = FastRegimeClassifier(_cache)
            # O(1) candle lookup by open_time_ms (avoids searching on every bar)
            _candle_by_ts: dict[str, dict[int, dict]] = {
                pair: {c["open_time"]: c for c in historical_data[pair]["15m"]}
                for pair in self.pairs
            }
            # Keep these None so the bar loop can branch cleanly
            mock_ws = mock_monitor = mock_rc = None
            dir_bias = crypto_conf = exec_filter = risk_engine = None
        else:
            mock_ws      = MockWSManager(historical_data)
            mock_monitor = MockHealthMonitor()
            mock_rc      = MockRegimeClassifier(mock_ws, mock_monitor)
            dir_bias     = DirectionalBias(mock_ws, mock_monitor)
            crypto_conf  = CryptoConfirmation(mock_ws, mock_monitor, mock_rc)
            book_mgr     = BookManager(mock_ws)
            exec_filter  = ExecutionFilter(book_mgr, mock_monitor)
            risk_engine  = RiskEngine(mock_ws)
            _cache = _fast_clf = _candle_by_ts = None

        # ── Step 3: Build sorted list of unique 15m bar timestamps ────────────
        # Collect all 15m bar open_times that fall in [start_date, end_date]
        start_ms = int(
            datetime(self.start_date.year, self.start_date.month,
                     self.start_date.day, tzinfo=timezone.utc).timestamp() * 1000
        )
        end_ms = int(
            datetime(self.end_date.year, self.end_date.month, self.end_date.day,
                     23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000
        )

        bar_times: set[int] = set()
        for pair in self.pairs:
            for c in historical_data[pair].get("15m", []):
                if start_ms <= c["open_time"] <= end_ms:
                    bar_times.add(c["open_time"])

        sorted_bar_ts = sorted(bar_times)
        if self._verbose:
            print(f"[backtest] Replaying {len(sorted_bar_ts)} unique 15m bars "
                  f"across {len(self.pairs)} pairs...\n")

        # ── Step 4: Replay bars ───────────────────────────────────────────────
        for ts_ms in sorted_bar_ts:
            bar_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

            # Slow engine: advance the mock time cursor
            if not use_fast_engine:
                mock_ws.set_current_time(bar_dt + timedelta(minutes=15))

            # Process each pair for this bar
            for pair in self.pairs:
                if use_fast_engine:
                    current_candle = _candle_by_ts[pair].get(ts_ms)
                    if current_candle is None:
                        continue
                else:
                    candles = mock_ws.get_closed_candles(pair, "15m")
                    if not candles or candles[-1]["open_time"] != ts_ms:
                        continue
                    current_candle = candles[-1]

                self._total_bars += 1

                # ── Close existing positions first ─────────────────────────
                self._check_position_close(pair, current_candle)

                # ── Run signal chain ───────────────────────────────────────
                if use_fast_engine:
                    self._run_signal_cycle_fast(
                        pair, bar_dt, current_candle, _fast_clf, _cache
                    )
                else:
                    self._run_signal_cycle(
                        pair, bar_dt, current_candle,
                        mock_rc, dir_bias, crypto_conf, exec_filter, risk_engine
                    )

        # ── Step 5: Force-close any remaining open positions at last price ────
        for pos in list(self._open_positions):
            if use_fast_engine:
                pair_candles = historical_data[pos.pair]["15m"]
            else:
                pair_candles = mock_ws.get_closed_candles(pos.pair, "15m")
            if pair_candles:
                last_price = pair_candles[-1]["close"]
                last_ts    = pair_candles[-1]["open_time"]
            else:
                last_price = pos.entry_price
                last_ts    = 0
            self._close_position(pos, last_price, last_ts, "EOD")

        # ── Step 6: Calculate metrics ─────────────────────────────────────────
        metrics = self._calculate_metrics()

        # ── Step 7: Save to DB ────────────────────────────────────────────────
        run_id = self._save_results(metrics)

        # ── Step 8: Print report ──────────────────────────────────────────────
        self._print_report(metrics, run_id)

        return {**metrics, "run_id": run_id}

    # ── Signal cycle ─────────────────────────────────────────────────────────

    def _run_signal_cycle(
        self,
        pair: str,
        bar_dt: datetime,
        candle: dict,
        mock_rc: MockRegimeClassifier,
        dir_bias: DirectionalBias,
        crypto_conf: CryptoConfirmation,
        exec_filter: ExecutionFilter,
        risk_engine: RiskEngine,
    ) -> None:
        """
        Run the full A→B→C→D→F signal chain for one bar/pair.
        Records every outcome in _signal_log.
        """
        date_str = bar_dt.strftime("%Y-%m-%d")
        entry_price = candle["close"]

        # ── Skip if already at max simultaneous positions for this pair ───────
        open_for_pair = sum(1 for p in self._open_positions if p.pair == pair)
        if open_for_pair >= 1:
            # Each pair allowed max 1 open position in backtest
            # (simplification: live bot allows MAX_SIMULTANEOUS_POSITIONS across all pairs)
            self._log_signal(bar_dt, pair, None, None, None, None,
                             "REJECT_MAX_POSITIONS", "already 1 open position for pair")
            return

        total_open = len(self._open_positions)
        if total_open >= config.MAX_SIMULTANEOUS_POSITIONS:
            self._log_signal(bar_dt, pair, None, None, None, None,
                             "REJECT_MAX_POSITIONS",
                             f"max {config.MAX_SIMULTANEOUS_POSITIONS} positions reached")
            return

        # ── Layer A: Regime ───────────────────────────────────────────────────
        try:
            regime_data = mock_rc.classify(pair)
        except Exception as exc:
            self._log_signal(bar_dt, pair, "ERROR", None, None, None,
                             "REJECT_ERROR", f"regime classify error: {exc}")
            self._no_trade_bars += 1
            return

        regime = regime_data["regime"]

        if regime in ("NO_TRADE", "EVENT_RISK"):
            self._no_trade_bars += 1
            self._log_signal(bar_dt, pair, regime, None, None, None,
                             f"REJECT_{regime}", regime_data.get("reason", ""))
            return

        # ── Layer B: Directional bias ─────────────────────────────────────────
        try:
            layer_b = dir_bias.evaluate(pair)
        except Exception as exc:
            self._log_signal(bar_dt, pair, regime, None, None, None,
                             "REJECT_ERROR", f"layer_b error: {exc}")
            return

        signal = layer_b["signal"]
        if signal == "NEUTRAL":
            self._log_signal(bar_dt, pair, regime, signal, None, None,
                             "REJECT_NEUTRAL", layer_b.get("reason", ""))
            return

        # ── Layer C: Crypto confirmation ──────────────────────────────────────
        try:
            layer_c = crypto_conf.evaluate(pair, signal)
        except Exception as exc:
            self._log_signal(bar_dt, pair, regime, signal, None, None,
                             "REJECT_ERROR", f"layer_c error: {exc}")
            return

        if not layer_c["confirmed"]:
            self._log_signal(bar_dt, pair, regime, signal, False, None,
                             "REJECT_LAYER_C", layer_c.get("reason", ""))
            return

        # ── Layer F (partial): size position ─────────────────────────────────
        try:
            conviction_score = risk_engine.calculate_conviction_score({
                "regime_data": regime_data,
                "layer_b":     layer_b,
                "layer_c":     layer_c,
                "layer_d":     None,
            })
            sizing = risk_engine.size_position(
                pair, regime,
                size_multiplier=1.0,          # no event overlay in backtest
                portfolio_value_usd=self._portfolio_value,
                conviction_score=conviction_score,
            )
        except Exception as exc:
            self._log_signal(bar_dt, pair, regime, signal, True, None,
                             "REJECT_ERROR", f"sizing error: {exc}")
            return

        order_size = sizing["order_size_usd"]
        if order_size <= 0:
            self._log_signal(bar_dt, pair, regime, signal, True, None,
                             "REJECT_SIZE_ZERO",
                             f"order_size=0 (portfolio=${self._portfolio_value:.2f})")
            return

        # ── Layer D: Execution filter ─────────────────────────────────────────
        try:
            d_passed, d_reason = exec_filter.evaluate(pair, signal, order_size)
        except Exception as exc:
            self._log_signal(bar_dt, pair, regime, signal, True, None,
                             "REJECT_ERROR", f"layer_d error: {exc}")
            return

        if not d_passed:
            self._log_signal(bar_dt, pair, regime, signal, True, False,
                             "REJECT_LAYER_D", d_reason)
            return

        # ── Daily loss cap check ──────────────────────────────────────────────
        cap_key = (date_str, pair)
        if risk_engine.check_daily_cap(self._daily_loss[cap_key], self._portfolio_value):
            self._log_signal(bar_dt, pair, regime, signal, True, True,
                             "REJECT_DAILY_CAP",
                             f"daily loss cap hit for {pair} on {date_str}")
            return

        # ── Layer F (complete): entry prices ──────────────────────────────────
        prices = risk_engine.calculate_entry_prices(
            pair, signal, entry_price,
            sizing["stop_distance_usd"],
            sizing["take_profit_distance_usd"],
        )

        # ── Open position ─────────────────────────────────────────────────────
        pos = _SimPosition(
            pair=pair,
            direction=signal,
            entry_price=prices["entry"],
            size_usd=order_size,
            stop_loss=prices["stop_loss"],
            take_profit=prices["take_profit"],
            entry_time=bar_dt,
            regime_at_entry=regime,
        )
        self._open_positions.append(pos)

        self._log_signal(bar_dt, pair, regime, signal, True, True,
                         "TRADE",
                         f"entry={prices['entry']:.2f}, sl={prices['stop_loss']:.2f}, "
                         f"tp={prices['take_profit']:.2f}, size=${order_size:.2f}, "
                         f"conviction={conviction_score:.2f}")
        if self._verbose:
            print(
                f"  [TRADE] {bar_dt.strftime('%Y-%m-%d %H:%M')} {pair} "
                f"{signal} @ {prices['entry']:.2f} "
                f"| sl={prices['stop_loss']:.2f} tp={prices['take_profit']:.2f} "
                f"| ${order_size:.2f} | {regime}"
            )

    # ── Fast signal cycle (indicator cache path) ──────────────────────────────

    def _run_signal_cycle_fast(
        self,
        pair: str,
        bar_dt: datetime,
        candle: dict,
        fast_clf,   # FastRegimeClassifier
        cache,      # IndicatorCache
    ) -> None:
        """
        Signal cycle using pre-computed indicators (Layers C and D skipped).

        Layers A and B read from IndicatorCache via FastRegimeClassifier.
        Position sizing uses the cached ATR value directly with config
        multipliers (which are temporarily overridden by run_sweep.py).
        """
        date_str    = bar_dt.strftime("%Y-%m-%d")
        entry_price = candle["close"]
        ts          = pd.Timestamp(bar_dt)   # for cache lookups

        # ── Max positions ─────────────────────────────────────────────────────
        if sum(1 for p in self._open_positions if p.pair == pair) >= 1:
            self._log_signal(bar_dt, pair, None, None, None, None,
                             "REJECT_MAX_POSITIONS", "already 1 open for pair")
            return
        if len(self._open_positions) >= config.MAX_SIMULTANEOUS_POSITIONS:
            self._log_signal(bar_dt, pair, None, None, None, None,
                             "REJECT_MAX_POSITIONS",
                             f"max {config.MAX_SIMULTANEOUS_POSITIONS} positions")
            return

        # ── Layer A: regime ───────────────────────────────────────────────────
        regime_data = fast_clf.classify(
            pair, ts,
            adx_threshold=config.ADX_TREND_THRESHOLD,
            adx_ambiguous_low=config.ADX_AMBIGUOUS_LOW,
            adx_ambiguous_high=config.ADX_AMBIGUOUS_HIGH,
        )
        regime = regime_data["regime"]

        if regime in ("NO_TRADE", "EVENT_RISK"):
            self._no_trade_bars += 1
            self._log_signal(bar_dt, pair, regime, None, None, None,
                             f"REJECT_{regime}", regime_data.get("reason", ""))
            return

        # ── Layer B: directional bias ─────────────────────────────────────────
        layer_b = fast_clf.analyse_direction(pair, ts)
        signal  = layer_b["signal"]

        if signal == "NEUTRAL":
            self._log_signal(bar_dt, pair, regime, signal, None, None,
                             "REJECT_NEUTRAL", layer_b.get("reason", ""))
            return

        # ── Sizing: ATR from cache (Layers C + D skipped) ────────────────────
        indicators = cache.get_indicators_at(pair, ts)
        atr = indicators.get("ATRr_14", 0.0)
        if not atr or (isinstance(atr, float) and math.isnan(atr)):
            self._log_signal(bar_dt, pair, regime, signal, True, True,
                             "REJECT_SIZE_ZERO", "ATR not ready")
            return

        is_sol   = pair == "SOL-USDT-SWAP"
        stop_mult   = config.SOL_ATR_STOP_MULTIPLIER if is_sol else config.ATR_STOP_MULTIPLIER
        tp_mult     = config.SOL_ATR_TP_MULTIPLIER   if is_sol else config.ATR_TP_MULTIPLIER
        max_pos_pct = config.SOL_MAX_POSITION_PCT     if is_sol else config.MAX_POSITION_PCT

        # Regime size multiplier (mirrors RiskEngine behaviour)
        size_mult = 1.0
        if regime == "RANGE_UNSTABLE":
            size_mult *= config.SIZE_MULTIPLIER_RANGE_UNSTABLE
        if "CROWDED" in regime:
            size_mult *= config.SIZE_MULTIPLIER_CROWDED

        order_size = max(
            config.PORTFOLIO_MIN_TRADE_USD,
            min(
                self._portfolio_value * max_pos_pct * size_mult,
                config.PORTFOLIO_MAX_TRADE_USD,
            ),
        )
        if order_size <= 0:
            self._log_signal(bar_dt, pair, regime, signal, True, True,
                             "REJECT_SIZE_ZERO", "order_size=0")
            return

        # ── Daily loss cap ────────────────────────────────────────────────────
        cap_key   = (date_str, pair)
        daily_cap = max(
            config.DAILY_LOSS_CAP_USD,
            self._portfolio_value * config.PORTFOLIO_DAILY_CAP_PCT,
        )
        if self._daily_loss[cap_key] >= daily_cap:
            self._log_signal(bar_dt, pair, regime, signal, True, True,
                             "REJECT_DAILY_CAP",
                             f"daily loss ${self._daily_loss[cap_key]:.2f} >= "
                             f"cap ${daily_cap:.2f}")
            return

        # ── Entry prices ──────────────────────────────────────────────────────
        stop_dist = atr * stop_mult
        tp_dist   = atr * tp_mult
        if signal == "LONG":
            stop_loss   = entry_price - stop_dist
            take_profit = entry_price + tp_dist
        else:
            stop_loss   = entry_price + stop_dist
            take_profit = entry_price - tp_dist

        # ── Open position ─────────────────────────────────────────────────────
        pos = _SimPosition(
            pair=pair,
            direction=signal,
            entry_price=entry_price,
            size_usd=order_size,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_time=bar_dt,
            regime_at_entry=regime,
        )
        self._open_positions.append(pos)

        self._log_signal(bar_dt, pair, regime, signal, True, True,
                         "TRADE",
                         f"entry={entry_price:.2f}, sl={stop_loss:.2f}, "
                         f"tp={take_profit:.2f}, size=${order_size:.2f}, fast")
        if self._verbose:
            print(
                f"  [TRADE] {bar_dt.strftime('%Y-%m-%d %H:%M')} {pair} "
                f"{signal} @ {entry_price:.2f} "
                f"| sl={stop_loss:.2f} tp={take_profit:.2f} "
                f"| ${order_size:.2f} | {regime}"
            )

    # ── Position close ────────────────────────────────────────────────────────

    def _check_position_close(self, pair: str, candle: dict) -> None:
        """
        Check if any open position for `pair` was stopped out or hit TP
        during the current candle.

        Uses candle high/low:
          LONG:  SL hit if low  <= stop_loss   / TP hit if high >= take_profit
          SHORT: SL hit if high >= stop_loss   / TP hit if low  <= take_profit

        If both SL and TP are within the candle range (gap/spike), SL wins
        (worst-case assumption, conservative).
        """
        for pos in list(self._open_positions):
            if pos.pair != pair:
                continue

            high = candle["high"]
            low  = candle["low"]
            ts   = candle["open_time"]

            sl_hit = False
            tp_hit = False

            if pos.direction == "LONG":
                sl_hit = low  <= pos.stop_loss
                tp_hit = high >= pos.take_profit
            else:  # SHORT
                sl_hit = high >= pos.stop_loss
                tp_hit = low  <= pos.take_profit

            if sl_hit and tp_hit:
                # Gap candle — assume SL (conservative)
                self._close_position(pos, pos.stop_loss, ts, "SL")
            elif sl_hit:
                self._close_position(pos, pos.stop_loss, ts, "SL")
            elif tp_hit:
                self._close_position(pos, pos.take_profit, ts, "TP")

    def _close_position(
        self, pos: _SimPosition, exit_price: float, ts_ms: int, reason: str
    ) -> None:
        """
        Close a simulated position, calculate gross PnL, deduct fees, update state.

        Fee model (both entry and exit charged at the same rate):
          fee_pct = BACKTEST_MAKER_FEE_PCT if BACKTEST_USE_MAKER_FEES
                    else BACKTEST_TAKER_FEE_PCT
          entry_fee = size_usd × fee_pct
          exit_fee  = size_usd × fee_pct
          total_fee = entry_fee + exit_fee
          net_pnl   = gross_pnl - total_fee
        """
        if pos.direction == "LONG":
            gross_pnl = (exit_price - pos.entry_price) / pos.entry_price * pos.size_usd
        else:
            gross_pnl = (pos.entry_price - exit_price) / pos.entry_price * pos.size_usd

        fee_pct = (
            config.BACKTEST_MAKER_FEE_PCT
            if config.BACKTEST_USE_MAKER_FEES
            else config.BACKTEST_TAKER_FEE_PCT
        )
        total_fee = pos.size_usd * fee_pct * 2   # entry + exit
        net_pnl   = gross_pnl - total_fee

        exit_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        date_str  = pos.entry_time.strftime("%Y-%m-%d")

        pos.exit_price   = round(exit_price, 6)
        pos.exit_time    = exit_time
        pos.close_reason = reason
        pos.gross_pnl_usd = round(gross_pnl, 6)
        pos.fee_usd      = round(total_fee, 6)
        pos.pnl_usd      = round(net_pnl, 6)

        # Update portfolio with net P&L
        self._portfolio_value += net_pnl
        self._daily_pnl[exit_time.strftime("%Y-%m-%d")] += net_pnl
        if net_pnl < 0:
            self._daily_loss[(date_str, pos.pair)] += abs(net_pnl)

        self._open_positions.remove(pos)
        self._closed_trades.append(pos)

        if self._verbose:
            icon = "✓" if net_pnl >= 0 else "✗"
            print(
                f"  [{icon} {reason}] {exit_time.strftime('%Y-%m-%d %H:%M')} "
                f"{pos.pair} {pos.direction} "
                f"exit={exit_price:.2f} gross={gross_pnl:+.4f} "
                f"fee={total_fee:.4f} net={net_pnl:+.4f} | "
                f"portfolio=${self._portfolio_value:.2f}"
            )

    # ── Signal logging ────────────────────────────────────────────────────────

    def _log_signal(
        self,
        ts: datetime,
        pair: str,
        regime: Optional[str],
        layer_b_signal: Optional[str],
        layer_c_confirmed: Optional[bool],
        layer_d_passed: Optional[bool],
        decision: str,
        reason: str,
    ) -> None:
        self._signal_log.append({
            "timestamp":        ts,
            "pair":             pair,
            "regime":           regime,
            "layer_b_signal":   layer_b_signal,
            "layer_c_confirmed": layer_c_confirmed,
            "layer_d_passed":   layer_d_passed,
            "decision":         decision,
            "reason":           reason,
        })

    # ── Metrics ───────────────────────────────────────────────────────────────

    def _calculate_metrics(self) -> dict:
        """
        Compute summary statistics from closed trades and daily PnL series.

        Returns dict with keys:
          total_trades, wins, losses, win_rate, total_pnl, avg_pnl_per_trade,
          max_drawdown, sharpe_ratio, no_trade_pct,
          by_pair, by_regime, by_close_reason
        """
        trades = self._closed_trades

        total_trades = len(trades)
        wins   = sum(1 for t in trades if t.pnl_usd >= 0)
        losses = total_trades - wins
        win_rate    = wins / total_trades if total_trades > 0 else 0.0
        total_fees  = sum(t.fee_usd for t in trades)
        gross_pnl   = sum(t.gross_pnl_usd for t in trades)
        total_pnl   = sum(t.pnl_usd for t in trades)   # net (after fees)
        avg_pnl     = total_pnl / total_trades if total_trades > 0 else 0.0

        no_trade_pct = (
            self._no_trade_bars / self._total_bars
            if self._total_bars > 0 else 0.0
        )

        # ── Max drawdown ──────────────────────────────────────────────────────
        max_drawdown = self._calc_max_drawdown()

        # ── Sharpe ratio ──────────────────────────────────────────────────────
        sharpe = self._calc_sharpe()

        # ── Per-pair breakdown ────────────────────────────────────────────────
        by_pair: dict[str, dict] = {}
        for pair in self.pairs:
            pair_trades = [t for t in trades if t.pair == pair]
            pair_wins   = sum(1 for t in pair_trades if t.pnl_usd >= 0)
            by_pair[pair] = {
                "trades":   len(pair_trades),
                "wins":     pair_wins,
                "win_rate": pair_wins / len(pair_trades) if pair_trades else 0.0,
                "total_pnl": sum(t.pnl_usd for t in pair_trades),
            }

        # ── Per-regime breakdown ──────────────────────────────────────────────
        by_regime: dict[str, dict] = {}
        for t in trades:
            r = t.regime_at_entry or "UNKNOWN"
            if r not in by_regime:
                by_regime[r] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
            by_regime[r]["trades"] += 1
            if t.pnl_usd >= 0:
                by_regime[r]["wins"] += 1
            by_regime[r]["total_pnl"] += t.pnl_usd

        # ── Close reason breakdown ────────────────────────────────────────────
        by_reason: dict[str, int] = {"TP": 0, "SL": 0, "EOD": 0}
        for t in trades:
            r = t.close_reason or "UNKNOWN"
            by_reason[r] = by_reason.get(r, 0) + 1

        return {
            "total_trades":        total_trades,
            "wins":                wins,
            "losses":              losses,
            "win_rate":            win_rate,
            "gross_pnl":           round(gross_pnl, 2),
            "total_fees":          round(total_fees, 2),
            "total_pnl":           round(total_pnl, 2),  # net after fees
            "avg_pnl_per_trade":   round(avg_pnl, 2),
            "max_drawdown":        round(max_drawdown, 2),
            "sharpe_ratio":        round(sharpe, 4),
            "no_trade_pct":        round(no_trade_pct, 4),
            "total_bars":          self._total_bars,
            "no_trade_bars":       self._no_trade_bars,
            "by_pair":             by_pair,
            "by_regime":           by_regime,
            "by_close_reason":     by_reason,
        }

    def _calc_max_drawdown(self) -> float:
        """
        Maximum drawdown from peak portfolio value (in USD).
        Uses the cumulative daily PnL series.
        """
        if not self._daily_pnl:
            return 0.0

        sorted_dates = sorted(self._daily_pnl.keys())
        cumulative = self.initial_capital
        peak = cumulative
        max_dd = 0.0

        for d in sorted_dates:
            cumulative += self._daily_pnl[d]
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_dd:
                max_dd = drawdown

        return max_dd

    def _calc_sharpe(self) -> float:
        """
        Annualised Sharpe ratio from daily PnL series (risk-free rate = 0).
        sharpe = mean(daily_pnl) / std(daily_pnl) * sqrt(252)
        Returns 0.0 if insufficient data (< 2 trading days).
        """
        daily_vals = list(self._daily_pnl.values())
        if len(daily_vals) < 2:
            return 0.0

        mean = sum(daily_vals) / len(daily_vals)
        variance = sum((v - mean) ** 2 for v in daily_vals) / (len(daily_vals) - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0

        if std == 0:
            return 0.0

        return (mean / std) * math.sqrt(252)

    # ── DB persistence ────────────────────────────────────────────────────────

    def _save_results(self, metrics: dict) -> int:
        """
        Insert backtest_runs row, all backtest_trades rows, and all
        backtest_signals rows.  Returns the run_id.
        """
        with self._db.begin() as conn:
            # ── backtest_runs ─────────────────────────────────────────────────
            result = conn.execute(
                insert(backtest_runs).values(
                    pairs=json.dumps(self.pairs),
                    start_date=self.start_date,
                    end_date=self.end_date,
                    initial_capital=self.initial_capital,
                    total_trades=metrics["total_trades"],
                    win_rate=metrics["win_rate"],
                    total_pnl=metrics["total_pnl"],
                    max_drawdown=metrics["max_drawdown"],
                    sharpe_ratio=metrics["sharpe_ratio"],
                    no_trade_pct=metrics["no_trade_pct"],
                    notes=self.notes,
                ).returning(backtest_runs.c.id)
            )
            run_id = result.scalar_one()

            # ── backtest_trades ───────────────────────────────────────────────
            if self._closed_trades:
                conn.execute(
                    insert(backtest_trades),
                    [
                        {
                            "run_id":          run_id,
                            "pair":            t.pair,
                            "direction":       t.direction,
                            "entry_time":      t.entry_time,
                            "exit_time":       t.exit_time,
                            "entry_price":     t.entry_price,
                            "exit_price":      t.exit_price,
                            "size_usd":        t.size_usd,
                            "stop_loss":       t.stop_loss,
                            "take_profit":     t.take_profit,
                            "pnl_usd":         t.pnl_usd,
                            "close_reason":    t.close_reason,
                            "regime_at_entry": t.regime_at_entry,
                        }
                        for t in self._closed_trades
                    ],
                )

            # ── backtest_signals ──────────────────────────────────────────────
            # Write in chunks to avoid huge single inserts
            chunk_size = 500
            for i in range(0, len(self._signal_log), chunk_size):
                chunk = self._signal_log[i : i + chunk_size]
                if chunk:
                    conn.execute(
                        insert(backtest_signals),
                        [
                            {
                                "run_id":            run_id,
                                "timestamp":         s["timestamp"],
                                "pair":              s["pair"],
                                "regime":            s["regime"],
                                "layer_b_signal":    s["layer_b_signal"],
                                "layer_c_confirmed": s["layer_c_confirmed"],
                                "layer_d_passed":    s["layer_d_passed"],
                                "decision":          s["decision"],
                                "reason":            s["reason"],
                            }
                            for s in chunk
                        ],
                    )

        return run_id

    # ── Report ────────────────────────────────────────────────────────────────

    def _print_report(self, metrics: dict, run_id: int) -> None:
        """Print a formatted backtest summary to stdout."""
        trades = self._closed_trades
        wins   = [t for t in trades if t.pnl_usd >= 0]
        losses = [t for t in trades if t.pnl_usd < 0]

        avg_win  = sum(t.pnl_usd for t in wins)   / len(wins)   if wins   else 0.0
        avg_loss = sum(t.pnl_usd for t in losses)  / len(losses) if losses else 0.0
        rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

        final_capital = self.initial_capital + metrics["total_pnl"]
        fee_mode = "maker" if config.BACKTEST_USE_MAKER_FEES else "taker"
        fee_pct  = (
            config.BACKTEST_MAKER_FEE_PCT
            if config.BACKTEST_USE_MAKER_FEES
            else config.BACKTEST_TAKER_FEE_PCT
        )

        print(f"\n{'='*60}")
        print(f"  BACKTEST RESULTS  (run_id={run_id})")
        print(f"{'='*60}")
        print(f"  Period       : {self.start_date} → {self.end_date}")
        print(f"  Pairs        : {', '.join(self.pairs)}")
        print(f"  Fee model    : {fee_mode} ({fee_pct*100:.3f}% per side, "
              f"{fee_pct*200:.3f}% round-trip)")
        print(f"  Bars scanned : {metrics['total_bars']} "
              f"({metrics['no_trade_bars']} NO_TRADE, "
              f"{metrics['no_trade_pct']*100:.1f}%)")
        print(f"")
        print(f"  TRADES")
        print(f"  {'─'*40}")
        print(f"  Total trades : {metrics['total_trades']}")
        print(f"  Wins / Losses: {metrics['wins']} / {metrics['losses']}")
        print(f"  Win rate     : {metrics['win_rate']*100:.1f}%")
        print(f"  Avg win      : ${avg_win:+.2f}")
        print(f"  Avg loss     : ${avg_loss:+.2f}")
        print(f"  R:R ratio    : {rr:.2f}")
        print(f"")
        print(f"  P&L")
        print(f"  {'─'*40}")
        print(f"  Gross P&L    : ${metrics['gross_pnl']:+.2f}")
        print(f"  Total fees   : ${metrics['total_fees']:.2f}  ({fee_mode}, {fee_pct*200:.3f}% r/t)")
        print(f"  Net P&L      : ${metrics['total_pnl']:+.2f}")
        print(f"  Avg / trade  : ${metrics['avg_pnl_per_trade']:+.4f}")
        print(f"  Initial cap  : ${self.initial_capital:,.2f}")
        print(f"  Final cap    : ${final_capital:,.2f}")
        net_return = (final_capital / self.initial_capital - 1) * 100
        gross_return = (self.initial_capital + metrics["gross_pnl"]) / self.initial_capital * 100 - 100
        print(f"  Gross return : {gross_return:+.2f}%")
        print(f"  Net return   : {net_return:+.2f}%")
        print(f"")
        print(f"  RISK")
        print(f"  {'─'*40}")
        print(f"  Max drawdown : ${metrics['max_drawdown']:.2f}")
        print(f"  Sharpe ratio : {metrics['sharpe_ratio']:.4f}")
        print(f"")
        print(f"  BY PAIR")
        print(f"  {'─'*40}")
        for pair, s in metrics["by_pair"].items():
            print(f"  {pair:<20} trades={s['trades']:3d}  "
                  f"wr={s['win_rate']*100:.0f}%  "
                  f"pnl=${s['total_pnl']:+.2f}")
        print(f"")
        print(f"  BY REGIME")
        print(f"  {'─'*40}")
        for regime, s in sorted(metrics["by_regime"].items()):
            wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
            print(f"  {regime:<22} trades={s['trades']:3d}  "
                  f"wr={wr:.0f}%  pnl=${s['total_pnl']:+.2f}")
        print(f"")
        print(f"  CLOSE REASONS")
        print(f"  {'─'*40}")
        for reason, count in metrics["by_close_reason"].items():
            print(f"  {reason:<8} : {count}")
        print(f"{'='*60}\n")
