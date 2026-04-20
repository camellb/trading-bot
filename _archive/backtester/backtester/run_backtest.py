#!/usr/bin/env python3
"""
Backtest runner — entry point for the backtesting module.

Loads .env, then runs a BacktestEngine for the specified pairs and date range.

Usage:
    # From the trading-bot root directory:
    python -m backtester.run_backtest [OPTIONS]

Options:
    --pairs     Comma-separated OKX pair IDs (default: BTC-USDT-SWAP,ETH-USDT-SWAP)
    --start     Start date YYYY-MM-DD (default: 30 days ago)
    --end       End date   YYYY-MM-DD (default: yesterday)
    --capital   Initial portfolio value in USD (default: 500.0)
    --notes     Optional free-text note saved with the run

Examples:
    # 30-day BTC backtest with default settings
    python -m backtester.run_backtest

    # Custom range, both pairs, custom capital
    python -m backtester.run_backtest \\
        --pairs BTC-USDT-SWAP,ETH-USDT-SWAP \\
        --start 2025-01-01 --end 2025-02-01 \\
        --capital 1000

    # Quick 7-day single-pair test
    python -m backtester.run_backtest \\
        --pairs BTC-USDT-SWAP \\
        --start 2025-03-01 --end 2025-03-07
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta

# load_dotenv MUST run before any other import that reads os.getenv()
from dotenv import load_dotenv
load_dotenv(override=True)

from backtester.backtest_engine import BacktestEngine


def _parse_args() -> argparse.Namespace:
    yesterday = date.today() - timedelta(days=1)
    default_start = yesterday - timedelta(days=30)

    parser = argparse.ArgumentParser(
        description="Run a backtest over historical OKX candle data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--pairs",
        default="BTC-USDT-SWAP,ETH-USDT-SWAP",
        help="Comma-separated OKX instrument IDs (default: BTC-USDT-SWAP,ETH-USDT-SWAP)",
    )
    parser.add_argument(
        "--start",
        default=default_start.isoformat(),
        help="Start date YYYY-MM-DD (default: 30 days ago)",
    )
    parser.add_argument(
        "--end",
        default=yesterday.isoformat(),
        help="End date YYYY-MM-DD inclusive (default: yesterday)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=500.0,
        help="Initial portfolio capital in USD (default: 500.0)",
    )
    parser.add_argument(
        "--notes",
        default="",
        help="Optional note saved with the backtest run",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    if not pairs:
        print("Error: --pairs must not be empty.", file=sys.stderr)
        sys.exit(1)

    try:
        start_date = date.fromisoformat(args.start)
        end_date   = date.fromisoformat(args.end)
    except ValueError as exc:
        print(f"Error: invalid date format — {exc}", file=sys.stderr)
        sys.exit(1)

    if end_date < start_date:
        print("Error: --end must be >= --start", file=sys.stderr)
        sys.exit(1)

    if args.capital <= 0:
        print("Error: --capital must be positive", file=sys.stderr)
        sys.exit(1)

    engine = BacktestEngine(notes=args.notes)
    asyncio.run(engine.run(
        pairs=pairs,
        start_date=start_date,
        end_date=end_date,
        initial_capital=args.capital,
        use_fast_engine=False,   # full production-equivalent signal chain
    ))


if __name__ == "__main__":
    main()
