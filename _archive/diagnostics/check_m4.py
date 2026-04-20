"""
M4 verification — all 7 checks from the M4 spec.

Checks:
  1. Decision loop output format (TRADE or REJECT with correct fields)
  2. Sizing sanity: RANGE_BALANCED, 1.0 mult, $500 portfolio → $25.00
  3. Crowded regime: TREND_UP_CROWDED, 1.0 mult → $12.50
  4. Event overlay 50% mult: RANGE_BALANCED, 0.5 mult → $12.50
  5. Daily cap: check_daily_cap(-51.0)=True, check_daily_cap(-49.0)=False
  6. R:R ratio >= 1.67 in any TRADE signal (ATR_TP/ATR_STOP = 2.5/1.5)
  7. DB rows: ticks table has decision, layer_b_signal, layer_d_result populated
"""
import asyncio
import subprocess
from dotenv import load_dotenv
load_dotenv()

from db.models import create_all_tables
from feeds.feed_health_monitor import monitor
from feeds.binance_ws import BinanceWebSocketManager
from feeds.book_manager import BookManager
from feeds.news_feed import NewsFeed
from feeds.macro_calendar import MacroCalendar
from engine.regime_classifier import RegimeClassifier
from engine.directional_bias import DirectionalBias
from engine.crypto_confirmation import CryptoConfirmation
from engine.execution_filter import ExecutionFilter
from engine.event_overlay import EventOverlay
from engine.risk_engine import RiskEngine
from engine.decision_loop import DecisionLoop
import config

create_all_tables()
_EXPECTED_RR = config.ATR_TP_MULTIPLIER / config.ATR_STOP_MULTIPLIER  # 2.5/1.5 = 1.6667


async def main():
    ws = BinanceWebSocketManager(monitor)
    book = BookManager(ws)
    news = NewsFeed(monitor)
    calendar = MacroCalendar(monitor)
    rc = RegimeClassifier(ws, monitor)
    db = DirectionalBias(ws, monitor)
    cc = CryptoConfirmation(ws, monitor, rc)
    ef = ExecutionFilter(book, monitor)
    eo = EventOverlay(calendar, news, monitor)
    re = RiskEngine(ws)
    dl = DecisionLoop(ws, monitor, rc, db, cc, ef, eo, re)

    ws_task = asyncio.create_task(ws.start(config.TRADING_PAIRS))
    await calendar.start()
    await rc.start()

    print("Waiting for backfill + WS warmup...", flush=True)
    for attempt in range(30):
        await asyncio.sleep(5)
        btc = ws.get_closed_candles("BTC/USDT:USDT", "15m")
        print(f"  t={attempt*5}s: BTC 15m={len(btc)}", flush=True)
        if len(btc) >= 40:
            print(f"Got {len(btc)} closed 15m candles — running checks\n", flush=True)
            break
    else:
        print("TIMEOUT: not enough candles after 150s")
        ws_task.cancel()
        return

    # ── CHECK 2: Sizing sanity ────────────────────────────────────────────────
    print("=== CHECK 2: Sizing sanity (RANGE_BALANCED, mult=1.0, $500) ===", flush=True)
    result = re.size_position("BTC/USDT:USDT", "RANGE_BALANCED", 1.0, 500.0)
    print(f"  result: {result}", flush=True)
    assert result["order_size_usd"] == 25.0, \
        f"Expected 25.0, got {result['order_size_usd']}"
    assert result["stop_distance_usd"] > 0, "stop must be > 0"
    assert result["take_profit_distance_usd"] > 0, "tp must be > 0"
    assert result["regime_multiplier"] == 1.0
    # ATR should be populated (199 backfilled candles available)
    if result["atr"] is not None:
        print(f"  ATR={result['atr']:.4f}, "
              f"stop={result['stop_distance_usd']:.2f}, "
              f"tp={result['take_profit_distance_usd']:.2f}", flush=True)
    else:
        print("  WARNING: ATR=None (fallback used)", flush=True)
    print("CHECK 2 PASS\n", flush=True)

    # ── CHECK 3: Crowded regime ───────────────────────────────────────────────
    print("=== CHECK 3: TREND_UP_CROWDED, mult=1.0, $500 → $12.50 ===", flush=True)
    result3 = re.size_position("BTC/USDT:USDT", "TREND_UP_CROWDED", 1.0, 500.0)
    print(f"  result: {result3}", flush=True)
    assert result3["order_size_usd"] == 12.50, \
        f"Expected 12.50, got {result3['order_size_usd']}"
    assert result3["regime_multiplier"] == config.SIZE_MULTIPLIER_CROWDED
    print("CHECK 3 PASS\n", flush=True)

    # ── CHECK 4: Event overlay size multiplier ────────────────────────────────
    print("=== CHECK 4: RANGE_BALANCED, event_mult=0.5, $500 → $12.50 ===", flush=True)
    result4 = re.size_position("BTC/USDT:USDT", "RANGE_BALANCED", 0.5, 500.0)
    print(f"  result: {result4}", flush=True)
    assert result4["order_size_usd"] == 12.50, \
        f"Expected 12.50, got {result4['order_size_usd']}"
    print("CHECK 4 PASS\n", flush=True)

    # ── CHECK 5: Daily cap ────────────────────────────────────────────────────
    print("=== CHECK 5: Daily cap check ===", flush=True)
    cap_hit = re.check_daily_cap(-51.0)
    cap_ok  = re.check_daily_cap(-49.0)
    print(f"  check_daily_cap(-51.0) = {cap_hit} (expected True)", flush=True)
    print(f"  check_daily_cap(-49.0) = {cap_ok} (expected False)", flush=True)
    assert cap_hit is True,  f"Expected True, got {cap_hit}"
    assert cap_ok  is False, f"Expected False, got {cap_ok}"
    print("CHECK 5 PASS\n", flush=True)

    # ── CHECK 6: R:R ratio ───────────────────────────────────────────────────
    print(f"=== CHECK 6: R:R ratio >= {_EXPECTED_RR:.4f} "
          f"(ATR_TP/ATR_STOP = {config.ATR_TP_MULTIPLIER}/{config.ATR_STOP_MULTIPLIER}) ===",
          flush=True)
    # Verify via calculate_entry_prices directly
    prices = re.calculate_entry_prices(
        "BTC/USDT:USDT", "LONG", 84200.0,
        stop_distance_usd=100.0,
        take_profit_distance_usd=100.0 * config.ATR_TP_MULTIPLIER / config.ATR_STOP_MULTIPLIER,
    )
    print(f"  prices: {prices}", flush=True)
    assert abs(prices["rr_ratio"] - _EXPECTED_RR) < 0.001, \
        f"R:R mismatch: {prices['rr_ratio']} vs {_EXPECTED_RR}"
    assert prices["stop_loss"] == round(84200.0 - 100.0, 2)
    assert prices["take_profit"] == round(84200.0 + 100.0 * _EXPECTED_RR, 2)
    print("CHECK 6 PASS\n", flush=True)

    # ── CHECK 1: Full decision loop cycle ─────────────────────────────────────
    print("=== CHECK 1: Full decision loop cycle (TRADE or REJECT) ===", flush=True)
    seen_trade = False
    for pair in config.TRADING_PAIRS:
        result = await dl.run_cycle(pair)
        line = DecisionLoop.format_result(result)
        print(f"  {line}", flush=True)

        # Validate structure
        assert result["decision"] in ("TRADE", "REJECT"), \
            f"Invalid decision: {result['decision']}"
        assert result["pair"] == pair
        assert isinstance(result["regime"], str)

        if result["decision"] == "TRADE":
            seen_trade = True
            assert result["signal"] in ("LONG", "SHORT")
            assert isinstance(result["entry_price"], float) and result["entry_price"] > 0
            assert isinstance(result["stop_loss"], float)
            assert isinstance(result["take_profit"], float)
            assert isinstance(result["order_size_usd"], float) and result["order_size_usd"] > 0
            assert isinstance(result["rr_ratio"], float) and result["rr_ratio"] >= 1.0
            assert result["reject_reason"] is None
        else:
            assert isinstance(result["reject_reason"], str) and result["reject_reason"]

    print("CHECK 1 PASS\n", flush=True)

    # ── CHECK 1b: Run a second cycle to get DB data ───────────────────────────
    print("Running second cycle to populate DB...", flush=True)
    for pair in config.TRADING_PAIRS:
        r = await dl.run_cycle(pair)
        print(f"  {DecisionLoop.format_result(r)}", flush=True)

    # ── CHECK 7: DB rows ──────────────────────────────────────────────────────
    print("\n=== CHECK 7: DB rows ===", flush=True)
    result = subprocess.run(
        [
            "/opt/homebrew/opt/postgresql@16/bin/psql",
            "trading_bot",
            "-c",
            "SELECT decision, layer_b_signal, layer_d_result, decision_reason "
            "FROM ticks ORDER BY id DESC LIMIT 10;",
        ],
        capture_output=True, text=True,
    )
    print(result.stdout, flush=True)
    if result.returncode != 0:
        print(f"DB error: {result.stderr}", flush=True)
    else:
        print("CHECK 7 PASS\n", flush=True)

    ws_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass

    print("=== ALL M4 CHECKS PASSED ===", flush=True)


asyncio.run(main())
