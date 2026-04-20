#!/usr/bin/env python3
"""
Parameter sweep tool for the backtester.

Tests every combination of ADX_TREND_THRESHOLD × ATR_STOP_MULTIPLIER ×
ATR_TP_MULTIPLIER and ranks them by annualised Sharpe ratio.

Historical candle data is downloaded ONCE and reused for every combination,
keeping the sweep from making thousands of redundant API requests.

Usage:
    # From the trading-bot root directory:
    python -m backtester.run_sweep

Expected run time: ~30-60 minutes for 80 combinations over 2 years.
"""

import asyncio
import itertools
import time
from datetime import date, timedelta

# load_dotenv MUST run before any other import that reads os.getenv()
from dotenv import load_dotenv
load_dotenv(override=True)

from backtester.backtest_engine import BacktestEngine
from backtester.data_fetcher import HistoricalDataFetcher
import config

# ── Sweep configuration ───────────────────────────────────────────────────────

SWEEP_PARAMS = {
    "ADX_TREND_THRESHOLD": [18, 20, 22, 25, 28],
    "ATR_STOP_MULTIPLIER": [1.2, 1.5, 1.8, 2.0],
    "ATR_TP_MULTIPLIER":   [2.0, 2.5, 3.0, 3.5],
}

SWEEP_PAIRS   = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
SWEEP_START   = date(2024, 4, 15)
SWEEP_END     = date(2026, 4, 14)
SWEEP_CAPITAL = 500.0

# Must match BacktestEngine._WARMUP_DAYS so the pre-fetched slice covers warmup
_WARMUP_DAYS = 3


async def run_sweep() -> list[dict]:
    keys         = list(SWEEP_PARAMS.keys())
    values       = list(SWEEP_PARAMS.values())
    combinations = list(itertools.product(*values))

    print(f"Running {len(combinations)} parameter combinations...")
    print(f"Pairs: {SWEEP_PAIRS}")
    print(f"Period: {SWEEP_START} to {SWEEP_END}")
    print()

    # ── Pre-fetch historical data once (reused for all combinations) ──────────
    print("[sweep] Pre-fetching historical candle data (once for all combos)...")
    fetcher     = HistoricalDataFetcher()
    fetch_start = SWEEP_START - timedelta(days=_WARMUP_DAYS)
    historical_data: dict = {}
    for pair in SWEEP_PAIRS:
        historical_data[pair] = {
            "15m": fetcher.fetch_historical_candles(
                pair, "15m", fetch_start, SWEEP_END
            )
        }
    print("[sweep] Data fetch complete.")

    # ── Pre-build IndicatorCache once (indicators don't change between combos) ─
    from backtester.indicator_cache import IndicatorCache
    print("[sweep] Building indicator cache...")
    sweep_cache = IndicatorCache()
    for pair in SWEEP_PAIRS:
        n = sweep_cache.build_cache(pair, historical_data[pair]["15m"])
        print(f"[sweep]   {pair}: {n} rows")
    print("[sweep] Indicator cache ready.\n")

    results: list[dict] = []
    combo_times: list[float] = []

    for i, combo in enumerate(combinations):
        params = dict(zip(keys, combo))

        # Temporarily override config values for this combination
        original_values: dict = {}
        for k, v in params.items():
            original_values[k] = getattr(config, k)
            setattr(config, k, v)

        engine = BacktestEngine(notes=str(params))
        t0 = time.perf_counter()
        try:
            result = await engine.run(
                pairs=SWEEP_PAIRS,
                start_date=SWEEP_START,
                end_date=SWEEP_END,
                initial_capital=SWEEP_CAPITAL,
                verbose=False,
                historical_data=historical_data,
                use_fast_engine=True,
                prebuilt_cache=sweep_cache,   # skip rebuild on every combo
            )
            elapsed = time.perf_counter() - t0
            combo_times.append(elapsed)
            results.append({**params, **result})

            # ETA after first combo completes
            avg_s    = sum(combo_times) / len(combo_times)
            remaining_s = avg_s * (len(combinations) - (i + 1))
            eta_str  = f"{remaining_s / 60:.0f}min remaining" if remaining_s > 60 else f"{remaining_s:.0f}s remaining"

            print(
                f"[{i+1}/{len(combinations)}] "
                f"ADX={params['ADX_TREND_THRESHOLD']:2d} "
                f"SL={params['ATR_STOP_MULTIPLIER']:.1f} "
                f"TP={params['ATR_TP_MULTIPLIER']:.1f} "
                f"→ pnl=${result['total_pnl']:+.2f} "
                f"wr={result['win_rate']*100:.1f}% "
                f"sharpe={result['sharpe_ratio']:.2f} "
                f"dd=${result['max_drawdown']:.2f} "
                f"[{elapsed:.1f}s | {eta_str}]"
            )
        except Exception as e:
            import traceback
            print(f"[{i+1}/{len(combinations)}] FAILED: {e}")
            traceback.print_exc()
        finally:
            # Always restore original config values
            for k, v in original_values.items():
                setattr(config, k, v)

    if not results:
        print("No results — all combinations failed.")
        return results

    # Sort by Sharpe ratio (best risk-adjusted return first)
    results.sort(key=lambda x: x.get("sharpe_ratio", float("-inf")), reverse=True)

    print()
    print("=" * 70)
    print("TOP 10 PARAMETER COMBINATIONS (by Sharpe ratio)")
    print("=" * 70)
    print(
        f"{'ADX':>5} {'SL':>6} {'TP':>6} {'P&L':>10} {'WR':>7} "
        f"{'Sharpe':>8} {'Drawdown':>10}"
    )
    print("-" * 70)
    for r in results[:10]:
        print(
            f"{r['ADX_TREND_THRESHOLD']:>5} "
            f"{r['ATR_STOP_MULTIPLIER']:>6.1f} "
            f"{r['ATR_TP_MULTIPLIER']:>6.1f} "
            f"${r['total_pnl']:>+9.2f} "
            f"{r['win_rate']*100:>6.1f}% "
            f"{r['sharpe_ratio']:>8.2f} "
            f"${r['max_drawdown']:>9.2f}"
        )

    print()
    best = results[0]
    print("BEST COMBINATION:")
    print(f"  ADX_TREND_THRESHOLD = {best['ADX_TREND_THRESHOLD']}")
    print(f"  ATR_STOP_MULTIPLIER = {best['ATR_STOP_MULTIPLIER']}")
    print(f"  ATR_TP_MULTIPLIER   = {best['ATR_TP_MULTIPLIER']}")
    print(f"  P&L: ${best['total_pnl']:+.2f}")
    print(f"  Win rate: {best['win_rate']*100:.1f}%")
    print(f"  Sharpe: {best['sharpe_ratio']:.2f}")
    print(f"  Max drawdown: ${best['max_drawdown']:.2f}")
    print()
    print("NOTE: Best by Sharpe ratio, not P&L.")
    print("Sharpe > 1.5 = strong. Sharpe > 2.0 = excellent.")
    print("Do not blindly apply the best params — review top 10")
    print("and pick params that are consistent across combinations.")

    return results


if __name__ == "__main__":
    asyncio.run(run_sweep())
