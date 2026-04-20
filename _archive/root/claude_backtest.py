"""
Claude backtest harness.

Replays the Claude-driven strategy over historical OKX candles and logs every
decision to the `predictions` table with source='backtest'.  The point is to
answer the single most important question about the crypto layer: does it have
historical edge, or are we funding live experimentation?

Design:
  * Fetch 1H OHLCV from OKX for each pair (reuses backtester/data_fetcher.py —
    read-only, does not modify backtester/ logic per CLAUDE.md rule 18).
  * Detect a bounded set of realistic trigger events (breakouts, RSI extremes,
    large candles) — matches the spirit of the live Scanner.
  * At each trigger, build a compact technical briefing and ask Claude for a
    LONG/SHORT/WAIT decision with stop/TP/confidence/playbook.
  * Simulate walk-forward entry/stop/TP on subsequent candles with realistic
    fees; resolve each prediction row with the realized outcome.

What's INTENTIONALLY simplified vs live:
  * No news catalyst signal (can't replay historical Gemini-filtered feed).
  * No order book imbalance (not stored).
  * No macro sentiment context (regenerated daily, not stored per-tick).
  * No multi-pair correlation.  Each pair evaluated independently.

These are acceptable MVP gaps — if Claude can't show edge on technicals alone,
it probably can't carry the news/book signals through to positive EV either.
If it CAN, layer those back in later.

Usage:
  python claude_backtest.py --days 30 --pairs BTC-USDT-SWAP --max-triggers 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import anthropic
import requests

import config
import calibration

SOURCE     = "backtest"
FEE_PCT    = 0.0005        # 0.05% taker fee, round-trip modeled below
SLIP_PCT   = 0.0005        # 0.05% adverse slippage on entry
HORIZON_H  = 72            # max hold in hours before force-close
LOOKBACK_BARS = 120        # hours of history included in briefing
TRIGGER_COOLDOWN_H = 6     # min hours between triggers on the same pair


# ── Historical candle fetch ──────────────────────────────────────────────────
# Local pagination — backtester/data_fetcher.py exists but has an early-exit
# bug on filtered pages and per CLAUDE.md rule 18 we don't modify it.
_OKX_HIST = "https://www.okx.com/api/v5/market/history-candles"

def fetch_candles(pair: str, bar: str, days: int) -> list[dict]:
    """Fetch oldest-first closed candles for the last `days` days of `bar` data."""
    session = requests.Session()
    session.headers.update({"User-Agent": "trading-bot-backtest/1.0"})
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    after    = now_ms
    all_rows: list[dict] = []
    for _ in range(50):  # hard cap → max 5000 candles
        r = session.get(_OKX_HIST, timeout=15, params={
            "instId": pair, "bar": bar, "limit": "100", "after": str(after),
        })
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            break
        page = []
        for row in data:
            if row[8] != "1":                # skip in-progress
                continue
            t = int(row[0])
            if t < start_ms:                 # past our window
                continue
            page.append({
                "open_time": t,
                "open":  float(row[1]),
                "high":  float(row[2]),
                "low":   float(row[3]),
                "close": float(row[4]),
                "volume":float(row[6]),
            })
        all_rows.extend(page)
        oldest_ts = int(data[-1][0])
        if oldest_ts < start_ms or len(data) < 100:
            break
        after = oldest_ts - 1
        time.sleep(0.12)                     # rate-limit safety
    all_rows.sort(key=lambda r: r["open_time"])
    return all_rows


# ── Candle / indicator math ──────────────────────────────────────────────────
@dataclass
class Candle:
    t:     int    # open_time ms
    open:  float
    high:  float
    low:   float
    close: float
    volume: float

    @classmethod
    def from_raw(cls, r: dict) -> "Candle":
        return cls(
            t     = int(r["open_time"]),
            open  = float(r["open"]),
            high  = float(r["high"]),
            low   = float(r["low"]),
            close = float(r["close"]),
            volume= float(r.get("volume", 0) or 0),
        )

    @property
    def iso(self) -> str:
        return datetime.fromtimestamp(self.t / 1000, tz=timezone.utc).isoformat()


def ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(values: list[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d > 0: gains += d
        else:     losses -= d
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(0, d))  / period
        avg_l = (avg_l * (period - 1) + max(0, -d)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    a = sum(trs[:period]) / period
    for v in trs[period:]:
        a = (a * (period - 1) + v) / period
    return a


def macd(values: list[float]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < 35:
        return None, None, None
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)
    line_series, times = [], []
    for i, (a, b) in enumerate(zip(fast, slow)):
        if a is None or b is None: continue
        line_series.append(a - b)
        times.append(i)
    if len(line_series) < 10:
        return None, None, None
    sig = ema_series(line_series, 9)
    line_last = line_series[-1]
    sig_last  = sig[-1]
    if sig_last is None:
        return None, None, None
    return line_last, sig_last, line_last - sig_last


# ── Trigger detection ────────────────────────────────────────────────────────
@dataclass
class Trigger:
    pair:    str
    index:   int         # index into the candle series
    type:    str         # e.g. 'breakout_up', 'rsi_oversold'
    detail:  str


def detect_triggers(candles: list[Candle], pair: str) -> list[Trigger]:
    """
    Find meaningful trigger points in the historical series. Keeps a cooldown
    so we don't stack triggers within 6h of each other on the same pair.
    Returns triggers in chronological order.
    """
    out: list[Trigger] = []
    last_trigger_idx = -10**9
    cooldown_bars = TRIGGER_COOLDOWN_H  # 1H candles → cooldown in bars

    for i in range(LOOKBACK_BARS, len(candles) - 24):  # leave 24 bars for horizon
        if i - last_trigger_idx < cooldown_bars:
            continue
        window = candles[i - LOOKBACK_BARS:i + 1]
        closes = [c.close for c in window]
        highs  = [c.high  for c in window]
        lows   = [c.low   for c in window]

        current = candles[i]
        prior_high = max(h for h in highs[:-1])
        prior_low  = min(l for l in lows[:-1])

        # 1. Breakout — close above trailing 5-day high
        if current.close > prior_high:
            out.append(Trigger(pair, i, "breakout_up",
                f"close {current.close:.2f} > 5d high {prior_high:.2f}"))
            last_trigger_idx = i
            continue

        # 2. Breakdown
        if current.close < prior_low:
            out.append(Trigger(pair, i, "breakout_down",
                f"close {current.close:.2f} < 5d low {prior_low:.2f}"))
            last_trigger_idx = i
            continue

        # 3. RSI oversold / overbought flip
        r = rsi(closes, 14)
        if r is not None:
            if r < 30:
                out.append(Trigger(pair, i, "rsi_oversold",
                    f"RSI-14 {r:.1f} < 30"))
                last_trigger_idx = i
                continue
            if r > 70:
                out.append(Trigger(pair, i, "rsi_overbought",
                    f"RSI-14 {r:.1f} > 70"))
                last_trigger_idx = i
                continue

        # 4. Large candle — body ≥ 2× recent median range
        ranges = sorted(h - l for h, l in zip(highs[-24:-1], lows[-24:-1]))
        median_range = ranges[len(ranges) // 2] if ranges else 0
        body = abs(current.close - current.open)
        if median_range > 0 and body >= 2 * median_range:
            direction = "up" if current.close > current.open else "down"
            out.append(Trigger(pair, i, f"large_candle_{direction}",
                f"body {body:.2f} ≥ 2× median range {median_range:.2f}"))
            last_trigger_idx = i

    return out


# ── Briefing builder ─────────────────────────────────────────────────────────
def build_briefing(pair: str, candles: list[Candle], idx: int, trig: Trigger) -> dict:
    window = candles[max(0, idx - LOOKBACK_BARS):idx + 1]
    closes = [c.close for c in window]
    current = candles[idx]
    r = rsi(closes, 14)
    m_line, m_sig, m_hist = macd(closes)
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    a14 = atr(window, 14)
    recent20 = [round(c.close, 4) for c in window[-20:]]
    change_24h = ((current.close - window[-25].close) / window[-25].close * 100.0) if len(window) >= 25 else None
    return {
        "pair":          pair,
        "timestamp":     current.iso,
        "price":         current.close,
        "trigger":       trig.type,
        "trigger_detail":trig.detail,
        "change_24h_pct":round(change_24h, 2) if change_24h is not None else None,
        "rsi_14":        round(r, 1)  if r   is not None else None,
        "macd":          round(m_line, 2) if m_line is not None else None,
        "macd_signal":   round(m_sig, 2)  if m_sig  is not None else None,
        "macd_hist":     round(m_hist, 2) if m_hist is not None else None,
        "ema_20":        round(e20, 2) if e20 is not None else None,
        "ema_50":        round(e50, 2) if e50 is not None else None,
        "atr_14":        round(a14, 2) if a14 is not None else None,
        "recent_closes": recent20,
    }


# ── Claude wrapper ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are a disciplined crypto futures trader reviewing a technical setup "
    "on a historical bar. You do not have news, macro, or order-book context — "
    "only the technicals shown. Decide LONG, SHORT, or WAIT. "
    "Respect the trigger but do not blindly follow it; say WAIT if risk/reward "
    "is unclear. Output STRICT JSON only: "
    "{\"action\":\"LONG|SHORT|WAIT\", \"stop_loss\":number|null, "
    "\"take_profit\":number|null, \"time_horizon_hours\":number|null, "
    "\"confidence\":0..1, "
    "\"playbook\":\"momentum|mean_reversion|breakout|counter_trend|other\", "
    "\"reasoning\":\"<120 words\"}"
)


def _parse_json(raw: str) -> Optional[dict]:
    t = raw.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1: t = t[nl + 1:]
        if t.endswith("```"): t = t[:-3]
    try:
        o = json.loads(t)
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def evaluate(client: anthropic.Anthropic, briefing: dict) -> Optional[dict]:
    prompt = (
        f"Pair: {briefing['pair']}\n"
        f"Bar timestamp: {briefing['timestamp']}\n"
        f"Current price: {briefing['price']}\n"
        f"24h change: {briefing['change_24h_pct']}%\n\n"
        f"Trigger: {briefing['trigger']} — {briefing['trigger_detail']}\n\n"
        f"Indicators:\n"
        f"  RSI-14:      {briefing['rsi_14']}\n"
        f"  MACD:        {briefing['macd']}  signal={briefing['macd_signal']}  hist={briefing['macd_hist']}\n"
        f"  EMA-20:      {briefing['ema_20']}\n"
        f"  EMA-50:      {briefing['ema_50']}\n"
        f"  ATR-14:      {briefing['atr_14']}\n\n"
        f"Last 20 closes: {briefing['recent_closes']}\n\n"
        f"Decide the trade. If LONG/SHORT, set stop and TP in absolute price terms."
    )
    try:
        resp = client.messages.create(
            model      = config.CLAUDE_MODEL,
            max_tokens = 600,
            system     = SYSTEM_PROMPT,
            messages   = [{"role": "user", "content": prompt}],
        )
        return _parse_json(resp.content[0].text)
    except Exception as exc:
        print(f"[backtest] Claude error: {exc}", file=sys.stderr)
        return None


# ── Trade simulation ─────────────────────────────────────────────────────────
@dataclass
class SimResult:
    status:      str      # 'TP' | 'SL' | 'TIMEOUT' | 'INVALID'
    exit_price:  float
    entry_price: float
    pnl_usd:     float
    hours_held:  float
    note:        str = ""


def simulate_trade(
    action: dict,
    candles: list[Candle],
    entry_idx: int,
    size_usd: float = 100.0,
) -> SimResult:
    """
    Walk forward from entry_idx+1 until stop or TP hit or horizon elapsed.
    Fees modeled as a 0.1% round trip (entry + exit). Slippage 0.05% adverse
    on entry. Size_usd is notional.
    """
    direction = action["action"]
    stop = action.get("stop_loss")
    tp   = action.get("take_profit")
    if direction not in ("LONG", "SHORT") or stop is None or tp is None:
        return SimResult("INVALID", 0.0, 0.0, 0.0, 0.0, "missing stop/tp")

    entry_bar = candles[entry_idx]
    raw_entry = entry_bar.close
    if direction == "LONG":
        entry_price = raw_entry * (1 + SLIP_PCT)
        if not (stop < entry_price < tp):
            return SimResult("INVALID", 0.0, entry_price, 0.0, 0.0,
                             f"LONG stop<entry<tp violated: {stop}<{entry_price:.2f}<{tp}")
    else:
        entry_price = raw_entry * (1 - SLIP_PCT)
        if not (tp < entry_price < stop):
            return SimResult("INVALID", 0.0, entry_price, 0.0, 0.0,
                             f"SHORT tp<entry<stop violated: {tp}<{entry_price:.2f}<{stop}")

    max_bars = min(HORIZON_H, len(candles) - entry_idx - 1)
    for k in range(1, max_bars + 1):
        bar = candles[entry_idx + k]
        # Conservative: if both stop and TP touched in the same bar, assume
        # stop hit first (bearish for entrant). This biases results slightly
        # negative — which is the direction we want when in doubt.
        if direction == "LONG":
            if bar.low  <= stop:
                return _finalize("SL", stop, entry_price, direction, size_usd, k)
            if bar.high >= tp:
                return _finalize("TP", tp, entry_price, direction, size_usd, k)
        else:
            if bar.high >= stop:
                return _finalize("SL", stop, entry_price, direction, size_usd, k)
            if bar.low  <= tp:
                return _finalize("TP", tp, entry_price, direction, size_usd, k)

    # Horizon timeout — close at last-bar close
    last = candles[entry_idx + max_bars]
    return _finalize("TIMEOUT", last.close, entry_price, direction, size_usd, max_bars)


def _finalize(status, exit_price, entry_price, direction, size_usd, bars_held) -> SimResult:
    gross_pct = ((exit_price - entry_price) / entry_price) if direction == "LONG" \
                else ((entry_price - exit_price) / entry_price)
    pnl = gross_pct * size_usd - (2 * FEE_PCT * size_usd)  # round-trip fees
    return SimResult(status, exit_price, entry_price, pnl, float(bars_held))


# Exit v2 constants — tunable per-strategy.
BE_BUFFER_PCT   = 0.0015   # move stop to entry + 0.15% on breakeven (covers taker fee round-trip)
TRAIL_ATR_MULT  = 1.0      # trailing distance in ATR-14 multiples
DEFAULT_HORIZON_H = 72     # fallback if Claude omits time_horizon_hours


def simulate_trade_v2(
    action: dict,
    candles: list[Candle],
    entry_idx: int,
    size_usd: float = 100.0,
) -> SimResult:
    """
    Exit logic 2.0:
      * breakeven move once price has travelled 1R favorably (stop → entry + fee buffer)
      * trailing stop after 1R — stop follows `best_seen − TRAIL_ATR_MULT × ATR`
      * time stop at Claude-declared horizon (falls back to 72h)

    Same inputs and return shape as v1 so the two can be compared head-to-head
    on identical Claude decisions.
    """
    direction = action["action"]
    stop = action.get("stop_loss")
    tp   = action.get("take_profit")
    if direction not in ("LONG", "SHORT") or stop is None or tp is None:
        return SimResult("INVALID", 0.0, 0.0, 0.0, 0.0, "missing stop/tp")

    horizon_h = action.get("time_horizon_hours")
    try:
        horizon_h = int(horizon_h) if horizon_h else DEFAULT_HORIZON_H
    except (TypeError, ValueError):
        horizon_h = DEFAULT_HORIZON_H
    horizon_h = max(6, min(horizon_h, 168))   # clamp 6h..7d

    entry_bar = candles[entry_idx]
    raw_entry = entry_bar.close
    if direction == "LONG":
        entry_price = raw_entry * (1 + SLIP_PCT)
        if not (stop < entry_price < tp):
            return SimResult("INVALID", 0.0, entry_price, 0.0, 0.0,
                             f"LONG stop<entry<tp violated: {stop}<{entry_price:.2f}<{tp}")
    else:
        entry_price = raw_entry * (1 - SLIP_PCT)
        if not (tp < entry_price < stop):
            return SimResult("INVALID", 0.0, entry_price, 0.0, 0.0,
                             f"SHORT tp<entry<stop violated: {tp}<{entry_price:.2f}<{stop}")

    one_r = abs(entry_price - stop)
    breakeven_trigger = entry_price + one_r if direction == "LONG" \
                        else entry_price - one_r
    breakeven_price   = entry_price * (1 + BE_BUFFER_PCT) if direction == "LONG" \
                        else entry_price * (1 - BE_BUFFER_PCT)

    # Pre-entry ATR for trailing distance.  Fall back to 0.5R if unavailable.
    warmup = candles[max(0, entry_idx - 60):entry_idx + 1]
    atr_val = atr(warmup, 14) or (one_r * 0.5)
    trail_distance = atr_val * TRAIL_ATR_MULT

    max_bars = min(horizon_h, len(candles) - entry_idx - 1)
    current_stop = stop
    best_seen    = entry_price
    one_r_hit    = False

    for k in range(1, max_bars + 1):
        bar = candles[entry_idx + k]

        # 1. Update best-seen (peak for LONG, trough for SHORT).
        if direction == "LONG":
            if bar.high > best_seen:
                best_seen = bar.high
        else:
            if bar.low < best_seen:
                best_seen = bar.low

        # 2. If 1R reached, arm breakeven and trailing.
        if not one_r_hit:
            if (direction == "LONG" and best_seen >= breakeven_trigger) or \
               (direction == "SHORT" and best_seen <= breakeven_trigger):
                one_r_hit = True
                # Move stop to breakeven if it would tighten the existing stop.
                current_stop = max(current_stop, breakeven_price) if direction == "LONG" \
                               else min(current_stop, breakeven_price)

        if one_r_hit:
            # 3. Trailing: stop follows the peak minus ATR.
            trail_stop = (best_seen - trail_distance) if direction == "LONG" \
                         else (best_seen + trail_distance)
            current_stop = max(current_stop, trail_stop) if direction == "LONG" \
                           else min(current_stop, trail_stop)

        # 4. Exit checks — stop first for conservatism on tied-bar touches.
        if direction == "LONG":
            if bar.low  <= current_stop:
                status = ("BE_STOP"    if current_stop >= breakeven_price - 1e-9 and not (current_stop > breakeven_price)
                          else "TRAIL_STOP" if current_stop > breakeven_price
                          else "SL")
                return _finalize(status, current_stop, entry_price, "LONG", size_usd, k)
            if bar.high >= tp:
                return _finalize("TP", tp, entry_price, "LONG", size_usd, k)
        else:
            if bar.high >= current_stop:
                status = ("BE_STOP"    if current_stop <= breakeven_price + 1e-9 and not (current_stop < breakeven_price)
                          else "TRAIL_STOP" if current_stop < breakeven_price
                          else "SL")
                return _finalize(status, current_stop, entry_price, "SHORT", size_usd, k)
            if bar.low  <= tp:
                return _finalize("TP", tp, entry_price, "SHORT", size_usd, k)

    # Time stop
    last = candles[entry_idx + max_bars]
    return _finalize("TIME_STOP", last.close, entry_price, direction, size_usd, max_bars)


# ── Orchestrator ─────────────────────────────────────────────────────────────
def run_backtest(
    pairs:             list[str],
    days:              int,
    max_triggers_pair: int = 20,
    size_usd:          float = 100.0,
) -> dict:
    end = date.today()
    start = end - timedelta(days=days)

    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        print(f"[backtest] Anthropic init failed: {exc}", file=sys.stderr)
        return {"error": "anthropic_init_failed"}

    summary = {
        "pairs":       pairs,
        "start":       start.isoformat(),
        "end":         end.isoformat(),
        "pair_results": {},
        "total_predictions": 0,
        "total_trades":      0,
        "total_pnl_usd":     0.0,
        "v1_totals": {"trades": 0, "wins": 0, "pnl_usd": 0.0, "invalid": 0},
        "v2_totals": {"trades": 0, "wins": 0, "pnl_usd": 0.0, "invalid": 0,
                      "tp": 0, "be_stop": 0, "trail": 0, "sl": 0, "time": 0},
    }

    for pair in pairs:
        print(f"\n[backtest] === {pair} — {start} → {end} ===", flush=True)
        raw = fetch_candles(pair, "1H", days)
        if not raw:
            print(f"[backtest] no candles for {pair}", flush=True)
            continue
        candles = [Candle.from_raw(r) for r in raw]
        print(f"[backtest] {pair}: loaded {len(candles)} 1H candles", flush=True)

        triggers = detect_triggers(candles, pair)
        print(f"[backtest] {pair}: {len(triggers)} triggers detected", flush=True)
        if len(triggers) > max_triggers_pair:
            # Evenly stride to respect budget
            step = len(triggers) / max_triggers_pair
            triggers = [triggers[int(i * step)] for i in range(max_triggers_pair)]
            print(f"[backtest] {pair}: strided to {len(triggers)}", flush=True)

        pair_result = {"triggers": len(triggers), "trades": 0, "waits": 0,
                       "wins": 0, "losses": 0, "pnl_usd": 0.0,
                       "v2_wins": 0, "v2_pnl_usd": 0.0}

        for trig in triggers:
            briefing = build_briefing(pair, candles, trig.index, trig)
            action = evaluate(client, briefing)
            if action is None:
                continue

            act = action.get("action", "WAIT")
            if act == "WAIT":
                pair_result["waits"] += 1
                continue

            # Run BOTH exit strategies on the same Claude decision — apples-to-apples.
            sim_v1 = simulate_trade(action,    candles, trig.index, size_usd)
            sim_v2 = simulate_trade_v2(action, candles, trig.index, size_usd)
            if sim_v1.status == "INVALID":
                print(f"[backtest] {pair} {trig.index} INVALID(v1): {sim_v1.note}", flush=True)
                summary["v1_totals"]["invalid"] += 1
                summary["v2_totals"]["invalid"] += 1
                continue

            # v1 accounting (primary — what `backtest` source shows in the dashboard)
            pair_result["trades"]  += 1
            pair_result["pnl_usd"] += sim_v1.pnl_usd
            if sim_v1.pnl_usd > 0: pair_result["wins"]   += 1
            else:                  pair_result["losses"] += 1
            summary["v1_totals"]["trades"]  += 1
            if sim_v1.pnl_usd > 0: summary["v1_totals"]["wins"] += 1
            summary["v1_totals"]["pnl_usd"] += sim_v1.pnl_usd

            # v2 accounting
            pair_result["v2_pnl_usd"] += sim_v2.pnl_usd
            if sim_v2.pnl_usd > 0: pair_result["v2_wins"] += 1
            if sim_v2.status != "INVALID":
                summary["v2_totals"]["trades"]  += 1
                if sim_v2.pnl_usd > 0: summary["v2_totals"]["wins"] += 1
                summary["v2_totals"]["pnl_usd"] += sim_v2.pnl_usd
                k = {"TP":"tp", "BE_STOP":"be_stop", "TRAIL_STOP":"trail",
                     "SL":"sl", "TIME_STOP":"time"}.get(sim_v2.status)
                if k: summary["v2_totals"][k] += 1

            probability = calibration.conviction_to_probability(action.get("confidence"))
            base_meta = {
                "trigger":       trig.type,
                "trigger_detail":trig.detail,
                "briefing":      briefing,
                "action":        action,
            }

            # Log v1 (primary)
            pid1 = calibration.log_prediction(
                source        = SOURCE,
                subject_key   = f"backtest:{pair}:{candles[trig.index].iso}",
                probability   = probability,
                category      = action.get("playbook"),
                confidence    = action.get("confidence"),
                horizon_hours = sim_v1.hours_held,
                reasoning     = action.get("reasoning", "")[:4000],
                metadata      = {**base_meta, "simulation": {
                    "exit_version":"v1", "status": sim_v1.status,
                    "entry_price": sim_v1.entry_price, "exit_price": sim_v1.exit_price,
                    "hours_held": sim_v1.hours_held, "size_usd": size_usd,
                }},
            )
            if pid1 > 0:
                calibration.resolve_prediction_by_id(pid1,
                    outcome=1 if sim_v1.pnl_usd > 0 else 0,
                    pnl_usd=sim_v1.pnl_usd, note=sim_v1.status)

            # Log v2 — separate source so calibration diagrams stay per-strategy.
            if sim_v2.status != "INVALID":
                pid2 = calibration.log_prediction(
                    source        = "backtest_v2",
                    subject_key   = f"backtest_v2:{pair}:{candles[trig.index].iso}",
                    probability   = probability,
                    category      = action.get("playbook"),
                    confidence    = action.get("confidence"),
                    horizon_hours = sim_v2.hours_held,
                    reasoning     = action.get("reasoning", "")[:4000],
                    metadata      = {**base_meta, "simulation": {
                        "exit_version":"v2", "status": sim_v2.status,
                        "entry_price": sim_v2.entry_price, "exit_price": sim_v2.exit_price,
                        "hours_held": sim_v2.hours_held, "size_usd": size_usd,
                    }},
                )
                if pid2 > 0:
                    calibration.resolve_prediction_by_id(pid2,
                        outcome=1 if sim_v2.pnl_usd > 0 else 0,
                        pnl_usd=sim_v2.pnl_usd, note=sim_v2.status)

            summary["total_predictions"] += 1
            time.sleep(0.2)

        summary["pair_results"][pair] = pair_result
        summary["total_trades"] += pair_result["trades"]
        summary["total_pnl_usd"] += pair_result["pnl_usd"]
        print(f"[backtest] {pair} done: {pair_result}", flush=True)

    return summary


# ── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--days",  type=int, default=30)
    ap.add_argument("--pairs", type=str, default="BTC-USDT-SWAP",
                    help="comma-separated")
    ap.add_argument("--max-triggers", type=int, default=15)
    ap.add_argument("--size",  type=float, default=100.0)
    args = ap.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    start_ts = time.time()
    summary = run_backtest(
        pairs             = pairs,
        days              = args.days,
        max_triggers_pair = args.max_triggers,
        size_usd          = args.size,
    )
    elapsed = time.time() - start_ts

    print("\n" + "=" * 78)
    print(f"BACKTEST SUMMARY  ({elapsed:.1f}s)")
    print("=" * 78)
    print(f"  {'pair':20s} {'triggers':>8s} {'trades':>7s} {'waits':>6s}  "
          f"{'v1 win%':>8s} {'v1 pnl':>9s}   {'v2 win%':>8s} {'v2 pnl':>9s}  Δ")
    for pair, r in summary.get("pair_results", {}).items():
        t = r["trades"]
        v1wr = (r["wins"]    / t * 100) if t else 0.0
        v2wr = (r["v2_wins"] / t * 100) if t else 0.0
        delta = r["v2_pnl_usd"] - r["pnl_usd"]
        print(f"  {pair:20s} {r['triggers']:>8d} {t:>7d} {r['waits']:>6d}  "
              f"{v1wr:>7.1f}% ${r['pnl_usd']:>+8.2f}   "
              f"{v2wr:>7.1f}% ${r['v2_pnl_usd']:>+8.2f}  ${delta:>+6.2f}")

    v1 = summary["v1_totals"]; v2 = summary["v2_totals"]
    v1t, v2t = max(v1["trades"], 1), max(v2["trades"], 1)
    print("\n  " + "-" * 72)
    print(f"  {'TOTAL v1':20s} trades={v1['trades']:3d}  "
          f"win%={v1['wins']/v1t*100:5.1f}  "
          f"pnl=${v1['pnl_usd']:+7.2f}  "
          f"avg=${v1['pnl_usd']/v1t:+.2f}")
    print(f"  {'TOTAL v2':20s} trades={v2['trades']:3d}  "
          f"win%={v2['wins']/v2t*100:5.1f}  "
          f"pnl=${v2['pnl_usd']:+7.2f}  "
          f"avg=${v2['pnl_usd']/v2t:+.2f}")
    print(f"  {'Δ (v2 − v1)':20s} "
          f"pnl=${v2['pnl_usd'] - v1['pnl_usd']:+7.2f}  "
          f"win%={v2['wins']/v2t*100 - v1['wins']/v1t*100:+5.1f}")
    print(f"\n  v2 exit mix: TP={v2['tp']} BE={v2['be_stop']} "
          f"TRAIL={v2['trail']} SL={v2['sl']} TIME={v2['time']}")
    print(f"  (logged {summary['total_predictions']} Claude decisions, "
          f"{summary['total_predictions']*2 - v1['invalid']} prediction rows)")
