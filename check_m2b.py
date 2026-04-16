"""
M2 verification — waits for 15m closed candles then runs full classify().
Also runs checks 3, 4, and 5.
"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

from db.models import create_all_tables
from feeds.feed_health_monitor import monitor
from feeds.binance_ws import BinanceWebSocketManager
from engine.regime_classifier import RegimeClassifier
from db import logger as db_logger
import config
import subprocess

create_all_tables()

VALID_REGIMES = {
    "TREND_UP_CLEAN", "TREND_DOWN_CLEAN", "TREND_UP_CROWDED", "TREND_DOWN_CROWDED",
    "RANGE_BALANCED", "RANGE_UNSTABLE", "EVENT_RISK", "NO_TRADE",
}


async def main():
    ws = BinanceWebSocketManager(monitor)
    rc = RegimeClassifier(ws, monitor)

    ws_task = asyncio.create_task(ws.start(config.TRADING_PAIRS))
    await rc.start()

    print("Waiting for first closed 15m candle (up to 15 min)...", flush=True)
    # Poll every 10s until we have at least 30 closed 15m candles (needed for ADX)
    for attempt in range(120):  # max 20 minutes
        await asyncio.sleep(10)
        btc_15m = ws.get_closed_candles("BTC/USDT:USDT", "15m")
        eth_15m = ws.get_closed_candles("ETH/USDT:USDT", "15m")
        btc_1m = ws.get_closed_candles("BTC/USDT:USDT", "1m")
        print(
            f"  t={attempt*10}s: BTC 15m closed={len(btc_15m)}, "
            f"ETH 15m closed={len(eth_15m)}, BTC 1m closed={len(btc_1m)}",
            flush=True,
        )
        if len(btc_15m) >= 2 and len(eth_15m) >= 2:
            print(f"\nGot {len(btc_15m)} BTC + {len(eth_15m)} ETH closed 15m candles — running checks", flush=True)
            break
    else:
        print("TIMEOUT: no 15m candles after 20 minutes")

    # ── CHECK 2: Regime output ─────────────────────────────────────────────────
    print("\n=== CHECK 2: Regime Output ===", flush=True)
    for pair in config.TRADING_PAIRS:
        result = rc.classify(pair)
        print(f"[REGIME] {pair}: {result}", flush=True)
        assert result["regime"] in VALID_REGIMES, f"Invalid regime: {result['regime']}"
        db_logger.log_regime(pair, result)
    print("CHECK 2 PASS: all regimes valid", flush=True)

    # Run one more iteration
    await asyncio.sleep(60)
    print("\n=== REGIME output after 60s more ===", flush=True)
    for pair in config.TRADING_PAIRS:
        result = rc.classify(pair)
        print(f"[REGIME] {pair}: {result}", flush=True)
        db_logger.log_regime(pair, result)

    # ── CHECK 3: Feed degradation ──────────────────────────────────────────────
    print("\n=== CHECK 3: Feed Degradation ===", flush=True)
    monitor.report_degraded("kline", "test degradation")
    r = rc.classify("BTC/USDT:USDT")
    print(f"  degraded → regime={r['regime']}, reason={r['reason']!r}", flush=True)
    assert r["regime"] == "NO_TRADE" and "core feed degraded" in r["reason"]
    print("  3a PASS: degradation → NO_TRADE", flush=True)

    monitor.report_healthy("kline")
    r2 = rc.classify("BTC/USDT:USDT")
    print(f"  restored → regime={r2['regime']}, reason={r2['reason']!r}", flush=True)
    assert "core feed degraded" not in r2["reason"]
    print("  3b PASS: restored → valid regime", flush=True)

    # ── CHECK 4: Data sufficiency ──────────────────────────────────────────────
    print("\n=== CHECK 4: Data Sufficiency ===", flush=True)
    for pair in config.TRADING_PAIRS:
        fp  = rc._get_funding_percentile(pair)
        oi  = rc._get_oi_delta(pair)
        adx = rc._get_adx(pair)
        vol = rc._get_realized_vol_percentile(pair)
        print(f"\n  {pair}:", flush=True)
        print(f"    funding_percentile : {fp}", flush=True)
        print(f"    oi_delta           : {oi}", flush=True)
        print(f"    adx                : {adx}", flush=True)
        print(f"    realized_vol_pct   : {vol}", flush=True)
        print(f"    classify()         : {rc.classify(pair)}", flush=True)

    # ── CHECK 5: DB rows ───────────────────────────────────────────────────────
    print("\n=== CHECK 5: DB rows ===", flush=True)
    result = subprocess.run(
        [
            "/opt/homebrew/opt/postgresql@16/bin/psql",
            "trading_bot",
            "-c",
            "SELECT pair, regime, adx, funding_pct, decision_reason FROM ticks ORDER BY id DESC LIMIT 5;",
        ],
        capture_output=True, text=True,
    )
    print(result.stdout, flush=True)
    if result.returncode != 0:
        print(f"DB error: {result.stderr}", flush=True)

    ws_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass


asyncio.run(main())
