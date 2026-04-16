"""
Strategist — the bot's brain, powered by Claude Sonnet.

Single entry point: make_decision(briefing).

The Scanner feeds it a rich briefing dict whenever it detects something
worth acting on. Claude reads the full context — prices, positions, news,
macro environment, IV, funding, and its own trade history — and returns a
structured decision: ENTER, EXIT, ADJUST, or WAIT.

Every decision is logged to the DB and Obsidian vault. Every trade entry
and exit triggers a Telegram notification with Claude's full reasoning.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

# Priority queue levels: lower number = processed first
# Critical news / urgent reviews should preempt routine candle signals.
_DECISION_PRIORITIES: dict[str, int] = {
    "NEWS_CRITICAL":         1,
    "URGENT_REVIEW":         1,
    "NEWS_URGENT":           2,
    "IV_SPIKE":              2,
    "MACRO_2H_WARNING":      2,
    "MACRO_15MIN_WARNING":   2,
    "THESIS_REVIEW":         5,
    "4H_ROUTINE_REVIEW":     5,
    "KEY_LEVEL":             5,
    "LARGE_CANDLE":          5,
    "VOLUME_SPIKE":          5,
    "FUNDING_EXTREME_LONG":  5,
    "FUNDING_EXTREME_SHORT": 5,
}

import anthropic

import config
from db import logger as db_logger
from db.models import daily_pnl as daily_pnl_table

if TYPE_CHECKING:
    from execution.order_manager import OrderManager
    from execution.position_monitor import PositionMonitor
    from engine.memory import MemoryManager
    from feeds.telegram_notifier import TelegramNotifier
    from engine.macro_context import MacroContextEngine


# ── System prompt (stable, sent with every call) ──────────────────────────────

_SYSTEM_PROMPT = """\
You are the trading brain of an autonomous crypto trading bot.
You manage a real portfolio of BTC, ETH, and SOL perpetual futures on OKX.
Your job is to make high-quality trading decisions that generate consistent
returns over time.

TRADING PHILOSOPHY:
- You trade like a professional fund manager, not a rule-following script
- You only trade when you have genuine conviction — it is fine to do nothing
- You think in terms of thesis: why will price move, and what invalidates it
- You hold positions for 1-7 days, not minutes
- You size positions based on conviction: high conviction = larger size
- You always set a logical stop loss based on market structure
- You protect capital first, make money second

HARD LIMITS (these cannot be overridden):
- Never deploy more than 50% of total capital simultaneously
- Every position must have a stop loss
- Never add to a losing position
- Maximum position size: $150 (at $500 capital)

DECISION FORMAT — respond with JSON only, no other text.

For entering a new trade:
{
  "action": "ENTER",
  "pair": "BTC-USDT-SWAP" | "ETH-USDT-SWAP" | "SOL-USDT-SWAP",
  "direction": "LONG" | "SHORT",
  "size_usd": number,
  "stop_loss": number,
  "take_profit": number,
  "confidence": 0.0-1.0,
  "playbook": "swing" | "momentum" | "mean_reversion" | "news_catalyst" | "macro",
  "time_horizon_days": number (expected hold in days),
  "catalyst": "the specific setup or event creating this opportunity",
  "invalidation": "the specific price level or event that proves you wrong",
  "primary_signal": "the single most important factor driving this decision",
  "risk_reward": number (calculate: (tp - entry) / (entry - sl) for long, (entry - tp) / (sl - entry) for short),
  "market_condition": "trending" | "ranging" | "volatile" | "low_volatility",
  "reasoning": "full explanation including all factors considered"
}

Playbook definitions:
  swing: multi-day trend trade, hold 2-7 days, technical setup driven
  momentum: breakout or continuation, hold hours to 2 days, volume driven
  mean_reversion: oversold/overbought bounce, hold hours to 1 day
  news_catalyst: event-driven, hold until catalyst resolves (hours to days)
  macro: macro theme trade, hold 3-14 days, fundamental driven

For exiting an existing position:
{
  "action": "EXIT",
  "position_id": 123,
  "exit_type": "thesis_broken" | "target_reached" | "time_decay" | "risk_management",
  "what_happened": "how the trade played out vs the original thesis",
  "reasoning": "why exiting now"
}

For doing nothing:
{
  "action": "WAIT",
  "reasoning": "why not acting and what would change your mind"
}

You may return multiple actions in a JSON array if needed:
[{"action": "EXIT", ...}, {"action": "ENTER", ...}]"""


class Strategist:
    """
    Claude Sonnet decision engine.

    Receives market briefings from the Scanner and returns trading decisions.
    All decisions are executed, logged to the DB, and written to the Obsidian vault.

    position_monitor is optional but strongly recommended — pass it in main.py
    so EXIT actions can correctly update the in-memory position state.
    """

    def __init__(
        self,
        order_manager: "OrderManager",
        memory: "MemoryManager",
        notifier: "TelegramNotifier",
        macro_context: Optional["MacroContextEngine"],
        position_monitor: Optional["PositionMonitor"] = None,
        health_monitor=None,
    ) -> None:
        self.order_manager    = order_manager
        self._memory          = memory
        self.notifier         = notifier
        self.macro_context    = macro_context
        self._health_monitor  = health_monitor
        self._position_monitor = position_monitor

        self._client          = anthropic.Anthropic()
        # Priority queue replaces the old single Lock so concurrent triggers
        # are queued by priority rather than silently dropped.
        self._decision_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._enqueue_seq:    int = 0   # tie-breaker; avoids comparing briefing dicts
        self._worker_task:    Optional[asyncio.Task] = None

        print("[strategist] Claude Strategist initialised", flush=True)

    @property
    def memory(self) -> "MemoryManager":
        """Public accessor for MemoryManager (used by PositionMonitor)."""
        return self._memory

    # ── Main entry point ──────────────────────────────────────────────────────

    async def make_decision(self, briefing: dict) -> None:
        """
        Queue a briefing for decision processing.

        Concurrent callers (Scanner monitors, PositionMonitor reviews) all
        enqueue here.  The _decision_worker processes one at a time, highest
        priority first.  Priority is derived from briefing["trigger_type"].

        Fire-and-forget: callers do not receive a return value because the
        decision is processed asynchronously after the function returns.
        """
        trigger = str(briefing.get("trigger_type", "")).upper()
        priority = _DECISION_PRIORITIES.get(trigger, 5)

        # Start the background worker if it is not running
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.get_running_loop().create_task(
                self._decision_worker()
            )

        self._enqueue_seq += 1
        await self._decision_queue.put((priority, self._enqueue_seq, briefing))
        print(
            f"[strategist] Queued {trigger or 'UNKNOWN'} "
            f"(priority={priority}, queue_depth={self._decision_queue.qsize()})",
            flush=True,
        )

    async def _decision_worker(self) -> None:
        """
        Background coroutine: dequeues and processes decisions sequentially.
        Highest priority (lowest number) processed first.
        Runs until cancelled.
        """
        while True:
            priority, _seq, briefing = await self._decision_queue.get()
            try:
                await self._process_decision(briefing)
            except Exception as exc:
                print(f"[strategist] _decision_worker error: {exc}", file=sys.stderr)
            finally:
                self._decision_queue.task_done()

    async def _process_decision(self, briefing: dict) -> list[dict]:
        """
        Core decision processing.  Called only by _decision_worker.
        Never raises; catches all exceptions and returns a WAIT action.
        """
        raw_response = ""
        actions: list[dict] = []
        try:
            enriched             = await self._enrich_briefing(briefing)
            system_prompt, user_msg = self._build_prompt(enriched)
            raw_response         = await self._call_claude(system_prompt, user_msg)
            actions              = self._parse_response(raw_response)

            print(
                f"[strategist] Decision for {enriched.get('pair', 'PORTFOLIO')}: "
                f"{[a.get('action') for a in actions]}",
                flush=True,
            )

            await self._execute_decision(actions, enriched)
            await self._log_decision(actions, enriched, raw_response)
            return actions

        except Exception as exc:
            print(f"[strategist] _process_decision error: {exc}", file=sys.stderr)
            fallback = [{"action": "WAIT", "reasoning": f"Internal error: {exc}"}]
            try:
                await self._log_decision(fallback, briefing, raw_response)
            except Exception:
                pass
            return fallback

    # ── Briefing enrichment ───────────────────────────────────────────────────

    async def _enrich_briefing(self, briefing: dict) -> dict:
        """
        Add portfolio state, macro context, and normalise key names so that
        _build_prompt can rely on a consistent structure regardless of whether
        the briefing came from the live Scanner or a test mock.
        """
        enriched = dict(briefing)
        loop = asyncio.get_running_loop()

        # ── Portfolio state ───────────────────────────────────────────────────
        if "portfolio" not in enriched:
            portfolio_value = await loop.run_in_executor(
                None, self.order_manager.get_portfolio_value
            )
            open_positions = enriched.get("open_positions", [])
            deployed = sum(float(p.get("size_usd") or 0) for p in open_positions)
            today_pnl = await loop.run_in_executor(None, self._get_todays_pnl)
            enriched["portfolio"] = {
                "total_capital":    portfolio_value,
                "deployed_capital": deployed,
                "deployed_pct":     (deployed / portfolio_value * 100) if portfolio_value > 0 else 0.0,
                "available_capital": max(0.0, portfolio_value - deployed),
                "today_pnl":        today_pnl,
                "unrealised_pnl":   0.0,
            }

        # ── Macro context ─────────────────────────────────────────────────────
        if "macro" not in enriched:
            enriched["macro"] = self._get_macro_context()

        # ── Strategy memory ───────────────────────────────────────────────────
        if "memory" not in enriched:
            # Scanner uses "strategy_memory" key; normalise to "memory"
            enriched["memory"] = enriched.pop(
                "strategy_memory", self._memory.read_strategy_memory()
            )

        # ── Prices dict (scanner passes single "price" key; mock passes "prices") ──
        if "prices" not in enriched:
            prices: dict = {}
            pair = enriched.get("pair")
            price_info = enriched.get("price", {})
            if pair and price_info.get("mark_price"):
                prices[pair] = {
                    "price":      float(price_info["mark_price"]),
                    "change_24h": 0.0,
                }
            enriched["prices"] = prices

        # ── Funding dict ──────────────────────────────────────────────────────
        if "funding" not in enriched:
            funding: dict = {}
            pair = enriched.get("pair")
            price_info = enriched.get("price", {})
            if pair and price_info.get("funding_rate") is not None:
                funding[pair] = float(price_info["funding_rate"])
            enriched["funding"] = funding

        # ── News list (scanner uses "news_headlines"; mock uses "news") ───────
        if "news" not in enriched and "news_headlines" in enriched:
            enriched["news"] = [
                {"summary": h, "urgency": "notable"}
                for h in enriched["news_headlines"]
            ]

        # ── IV dict (scanner has a scalar per pair; mock has {"BTC": 52.0}) ──
        if "iv" in enriched and not isinstance(enriched["iv"], dict):
            iv_val = enriched["iv"]
            pair = enriched.get("pair", "")
            ccy = "BTC" if "BTC" in pair else "ETH" if "ETH" in pair else "BTC"
            enriched["iv"] = {ccy: iv_val} if iv_val is not None else {}

        # ── Normalise upcoming events ─────────────────────────────────────────
        if "upcoming_macro_events" in enriched and "macro" in enriched:
            if not enriched["macro"].get("upcoming_events"):
                enriched["macro"]["upcoming_events"] = enriched["upcoming_macro_events"]

        return enriched

    # ── Prompt building ───────────────────────────────────────────────────────

    def _build_prompt(self, briefing: dict) -> tuple[str, str]:
        """Return (system_prompt, user_message) built from the enriched briefing."""
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        trigger_type   = briefing.get("trigger_type", "unknown")
        trigger_detail = briefing.get("trigger_detail", "")

        portfolio  = briefing.get("portfolio", {})
        total_cap  = portfolio.get("total_capital",    config.STARTING_CAPITAL_USD)
        deployed   = portfolio.get("deployed_capital", 0.0)
        dep_pct    = portfolio.get("deployed_pct",     0.0)
        available  = portfolio.get("available_capital", total_cap)
        today_pnl  = portfolio.get("today_pnl",        0.0)
        macro      = briefing.get("macro", {})
        risk_mult  = macro.get("risk_multiplier", 1.0)

        prices    = briefing.get("prices",    {})
        technical = briefing.get("technical", {})
        funding   = briefing.get("funding",   {})
        iv_data   = briefing.get("iv",        {})

        # ── Helpers ───────────────────────────────────────────────────────────
        def _p(v) -> str:
            return f"${v:,.2f}" if v is not None else "N/A"

        def _pct(v) -> str:
            if v is None:
                return "N/A"
            sign = "+" if v >= 0 else ""
            return f"{sign}{v:.2f}%"

        def _f(v, fmt=".2f") -> str:
            return f"{v:{fmt}}" if v is not None else "N/A"

        # ── Open positions ────────────────────────────────────────────────────
        open_positions = briefing.get("open_positions", [])
        if open_positions:
            pos_lines = []
            for pos in open_positions:
                pid      = pos.get("id") or pos.get("trade_id", "?")
                pair_p   = pos.get("pair", "?")
                dirn_p   = pos.get("direction", "?")
                entry_p  = float(pos.get("entry_price") or 0)
                size_p   = float(pos.get("size_usd")    or 0)
                stop_p   = pos.get("stop_loss")
                tp_p     = pos.get("take_profit")
                thesis_p = (pos.get("thesis") or pos.get("trigger_event") or "").strip()
                opened   = pos.get("timestamp_open") or pos.get("opened_at")

                hours_held = "?"
                if opened:
                    try:
                        od = (
                            datetime.fromisoformat(str(opened))
                            if isinstance(opened, str)
                            else opened
                        )
                        if not od.tzinfo:
                            od = od.replace(tzinfo=timezone.utc)
                        hours_held = f"{(datetime.now(timezone.utc) - od).total_seconds() / 3600:.1f}"
                    except Exception:
                        pass

                curr_price = float(prices.get(pair_p, {}).get("price") or entry_p or 0)
                if entry_p > 0:
                    if dirn_p == "LONG":
                        unreal_usd = (curr_price - entry_p) / entry_p * size_p
                    else:
                        unreal_usd = (entry_p - curr_price) / entry_p * size_p
                    unreal_pct = unreal_usd / size_p * 100 if size_p > 0 else 0.0
                else:
                    unreal_usd = unreal_pct = 0.0

                stop_str = _p(float(stop_p)) if stop_p else "N/A"
                tp_str   = _p(float(tp_p))   if tp_p   else "N/A"
                pos_lines.append(
                    f"  [{pid}] {dirn_p} {pair_p} @ {_p(entry_p)} | Size: ${size_p:.2f}\n"
                    f"       Current: {_p(curr_price)} | "
                    f"Unrealised: {unreal_usd:+.2f} USD ({unreal_pct:+.1f}%)\n"
                    f"       Stop: {stop_str} | Target: {tp_str}\n"
                    f"       Thesis: {thesis_p[:200] if thesis_p else 'not recorded'}\n"
                    f"       Held: {hours_held}h"
                )
            positions_block = "\n".join(pos_lines)
        else:
            positions_block = "No open positions"

        # ── Prices summary ────────────────────────────────────────────────────
        price_lines = []
        for pair_k in config.TRADING_PAIRS:
            pdata = prices.get(pair_k, {})
            tdata = technical.get(pair_k, {})
            cp    = pdata.get("price")
            c24   = pdata.get("change_24h")
            c1h   = tdata.get("change_1h_pct")
            c4h   = tdata.get("change_4h_pct")
            price_lines.append(
                f"  {pair_k}: {_p(cp)} | "
                f"1H: {_pct(c1h)} | 4H: {_pct(c4h)} | 24H: {_pct(c24)}"
            )
        prices_block = "\n".join(price_lines) if price_lines else "  Prices unavailable"

        # ── Technical block (full suite per pair) ─────────────────────────────
        tech_lines = []
        for pair_k in config.TRADING_PAIRS:
            tdata = technical.get(pair_k)
            if not tdata:
                tech_lines.append(f"\n{pair_k}: [NO DATA — feed not yet available]")
                continue

            deg_note = (
                f"  \u26a0 FEED DEGRADED — {tdata.get('degraded_reason', '')}"
                if tdata.get("degraded")
                else ""
            )

            cp    = tdata.get("current_price")
            c1h   = tdata.get("change_1h_pct")
            c4h   = tdata.get("change_4h_pct")
            c24h  = tdata.get("change_24h_pct")
            e20   = tdata.get("ema_20")
            e50   = tdata.get("ema_50")
            vs20  = tdata.get("price_vs_ema20", "N/A")
            vs50  = tdata.get("price_vs_ema50", "N/A")
            trend = tdata.get("trend", "N/A")
            rsi   = tdata.get("rsi_14")
            ml    = tdata.get("macd_line")
            ms    = tdata.get("macd_signal")
            mh    = tdata.get("macd_histogram")
            atr   = tdata.get("atr_14")
            vr    = tdata.get("volume_ratio")
            h24   = tdata.get("high_24h")
            l24   = tdata.get("low_24h")
            h48   = tdata.get("high_48h")
            l48   = tdata.get("low_48h")
            c6    = tdata.get("last_6_closes", [])
            bars4h = tdata.get("last_6_4h_candles", [])

            mh_str = f"{'+' if mh >= 0 else ''}{_f(mh)}" if mh is not None else "N/A"

            lines = [
                f"\n{pair_k}:",
                f"  Price: {_p(cp)} | 1H: {_pct(c1h)} | 4H: {_pct(c4h)} | 24H: {_pct(c24h)}",
                f"  EMA20: {_p(e20)} ({vs20}) | EMA50: {_p(e50)} ({vs50})",
                f"  Trend: {trend} | RSI(14): {_f(rsi, '.1f')}",
                f"  MACD: {_f(ml)} / Signal: {_f(ms)} / Hist: {mh_str}",
                f"  ATR(14): {_p(atr)} | Volume ratio: {_f(vr, '.2f')}x",
                f"  24H Range: {_p(l24)} \u2013 {_p(h24)}",
                f"  48H Range: {_p(l48)} \u2013 {_p(h48)}",
            ]
            if c6:
                lines.append(
                    "  Last 6 closes (1H): [" +
                    ", ".join(f"{v:,.0f}" for v in c6) + "]"
                )
            if bars4h:
                lines.append("  Last 6 \u00d7 4H candles:")
                for bar in bars4h:
                    lines.append(
                        f"    {bar.get('timestamp', '?')} | "
                        f"O:{bar.get('open', 0):,.0f} H:{bar.get('high', 0):,.0f} "
                        f"L:{bar.get('low', 0):,.0f} C:{bar.get('close', 0):,.0f} "
                        f"V:{bar.get('volume', 0):,.0f}"
                    )
            if deg_note:
                lines.append(deg_note)
            tech_lines.extend(lines)

        tech_block = "\n".join(tech_lines) if tech_lines else "  Technical data not available"

        # ── Macro block ───────────────────────────────────────────────────────
        macro_sentiment  = macro.get("sentiment",  "NEUTRAL")
        macro_confidence = macro.get("confidence", 0.5) * 100
        macro_reasoning  = macro.get("reasoning",  "No macro brief available")
        macro_watch      = macro.get("watch_for",  "")
        macro_events     = macro.get("upcoming_events", [])

        event_lines = []
        for ev in macro_events[:5]:
            if isinstance(ev, dict):
                event_lines.append(
                    f"  - {ev.get('type', ev.get('description', str(ev)))} "
                    f"in {ev.get('days_away', ev.get('days', '?'))} days"
                )
            else:
                event_lines.append(f"  - {ev}")
        events_block = (
            "\n".join(event_lines) if event_lines
            else "  No scheduled events in next 3 days"
        )

        # ── News block ────────────────────────────────────────────────────────
        news = briefing.get("news", [])
        news_lines = []
        for item in news[:10]:
            if isinstance(item, dict):
                news_lines.append(
                    f"  - {item.get('summary', item.get('headline', str(item)))} "
                    f"[{item.get('urgency', 'notable')}]"
                )
            elif isinstance(item, str):
                news_lines.append(f"  - {item}")
        news_block = "\n".join(news_lines) if news_lines else "  No recent news"

        # ── IV regime ─────────────────────────────────────────────────────────
        def _iv_regime(v) -> str:
            if v is None:  return "N/A"
            if v < 40:     return "LOW"
            if v < 80:     return "NORMAL"
            if v < 100:    return "HIGH"
            return "EXTREME"

        btc_iv_val = iv_data.get("BTC") if isinstance(iv_data, dict) else None
        eth_iv_val = iv_data.get("ETH") if isinstance(iv_data, dict) else None
        btc_iv_str = (
            f"{btc_iv_val:.1f} [{_iv_regime(btc_iv_val)}]"
            if btc_iv_val is not None else "N/A"
        )
        eth_iv_str = (
            f"{eth_iv_val:.1f} [{_iv_regime(eth_iv_val)}]"
            if eth_iv_val is not None else "N/A"
        )
        iv_spike_note = " \u26a1 SPIKE" if trigger_type == "IV_SPIKE" else ""

        # ── Market conditions (IV + funding + order book per pair) ────────────
        cond_lines = [
            f"BTC IV (DVOL): {btc_iv_str}{iv_spike_note}",
            f"ETH IV (DVOL): {eth_iv_str}",
        ]
        for pair_k in config.TRADING_PAIRS:
            tdata = technical.get(pair_k, {})
            fr    = funding.get(pair_k)
            cond_lines.append("")
            if fr is not None:
                fr_f  = float(fr)
                sent  = tdata.get("funding_sentiment", "N/A")
                ann   = tdata.get("funding_annualised_pct")
                ann_s = (
                    f" | Ann: {'+' if ann >= 0 else ''}{ann:.1f}%"
                    if ann is not None else ""
                )
                cond_lines.append(
                    f"{pair_k} Funding: {'+' if fr_f >= 0 else ''}"
                    f"{fr_f * 100:.4f}%/8H ({sent}){ann_s}"
                )
            else:
                cond_lines.append(f"{pair_k} Funding: N/A")

            imb   = tdata.get("book_imbalance")
            bid_d = tdata.get("bid_depth_1pct")
            ask_d = tdata.get("ask_depth_1pct")
            spr   = tdata.get("book_spread_pct")
            if imb is not None:
                if imb > 0.6:
                    imb_note = "BID HEAVY \u2014 bullish pressure"
                elif imb < 0.4:
                    imb_note = "ASK HEAVY \u2014 bearish pressure"
                else:
                    imb_note = "BALANCED"
                spr_s   = f" | Spread: {spr:.4f}%" if spr is not None else ""
                depth_s = (
                    f" | Bid: {bid_d:.0f} / Ask: {ask_d:.0f} (within 1%)"
                    if bid_d is not None else ""
                )
                cond_lines.append(
                    f"  Book: {imb:.2f} imbalance ({imb_note}){spr_s}{depth_s}"
                )

        # ── Fear & Greed ──────────────────────────────────────────────────────
        fg = briefing.get("fear_greed", {})
        if fg.get("current_value") is not None:
            fg_val   = fg["current_value"]
            fg_label = fg.get("current_label", "")
            fg_trend = fg.get("trend", "")
            fg_line  = f"Fear & Greed: {fg_val} ({fg_label}) — {fg_trend}"
            if fg_val <= 20:
                fg_line += "  \u26a0\ufe0f EXTREME FEAR \u2014 historically good buying opportunity"
            elif fg_val >= 80:
                fg_line += "  \u26a0\ufe0f EXTREME GREED \u2014 market overleveraged, be cautious with longs"
            cond_lines.append("")
            cond_lines.append(fg_line)

        conditions_block = "\n".join(cond_lines)

        # ── Memory block ──────────────────────────────────────────────────────
        memory      = briefing.get("memory", {})
        what_works  = memory.get("what_works",       "No strategy memory yet \u2014 this is early days")
        what_doesnt = memory.get("what_doesnt_work", "No failure patterns recorded yet")
        curr_thesis = memory.get("current_thesis",   "No thesis recorded yet")

        # ── Assemble ──────────────────────────────────────────────────────────
        sections = [
            f"TRIGGER: {trigger_type} \u2014 {trigger_detail}",
            f"Time: {now_utc}",
            "",
            "=== PORTFOLIO STATE ===",
            f"Total capital: ${total_cap:,.2f}",
            f"Deployed: ${deployed:,.2f} ({dep_pct:.0f}% of capital)",
            f"Available: ${available:,.2f}",
            f"Today P&L: {today_pnl:+.2f} USD",
            f"Macro risk multiplier: {risk_mult:.1f}x",
            "",
            "=== OPEN POSITIONS ===",
            positions_block,
            "",
            "=== MARKET PRICES ===",
            prices_block,
            "",
            "=== TECHNICAL CONTEXT ===",
            tech_block,
            "",
            "=== MACRO & NEWS ===",
            f"Macro sentiment: {macro_sentiment} (confidence: {macro_confidence:.0f}%, "
            f"risk multiplier: {risk_mult:.1f}x)",
            f"Context: {macro_reasoning}",
            f"Watch today: {macro_watch}",
            "",
            "Upcoming events:",
            events_block,
            "",
            "Recent news (Gemini-filtered, notable+):",
            news_block,
            "",
            "=== MARKET CONDITIONS ===",
            conditions_block,
            "",
            "=== MEMORY (what you've learned) ===",
            "What works:",
            what_works,
            "",
            "What doesn't work:",
            what_doesnt,
            "",
            "Current market thesis:",
            curr_thesis,
            "",
            "=== YOUR DECISION ===",
        ]
        user_msg = "\n".join(sections)

        return _SYSTEM_PROMPT, user_msg

    # ── Claude API call ───────────────────────────────────────────────────────

    async def _call_claude(self, system: str, user: str) -> str:
        """Call Claude Sonnet. Runs in executor to avoid blocking the event loop."""
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self._client.messages.create(
                    model=config.CLAUDE_MODEL,
                    max_tokens=1500,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                ),
            )
            return response.content[0].text
        except Exception as exc:
            print(f"[strategist] Claude API error: {exc}", file=sys.stderr)
            return json.dumps({
                "action": "WAIT",
                "reasoning": f"API error: {exc}",
            })

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> list[dict]:
        """
        Parse Claude's JSON response. Handles:
          - Single action dict
          - Array of action dicts
          - Markdown code fences (```json ... ```)
        Returns a validated list of action dicts.
        """
        text = raw.strip()

        # Strip markdown fences
        if text.startswith("```"):
            first_newline = text.find("\n")
            last_fence    = text.rfind("```")
            if first_newline != -1 and last_fence > first_newline:
                text = text[first_newline + 1 : last_fence].strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"[strategist] JSON parse error: {exc}", file=sys.stderr)
            return [{"action": "WAIT", "reasoning": f"Parse error: {raw[:200]}"}]

        if isinstance(parsed, dict):
            actions = [parsed]
        elif isinstance(parsed, list):
            actions = parsed
        else:
            return [{"action": "WAIT", "reasoning": f"Unexpected response type: {type(parsed).__name__}"}]

        # Validate: keep only dicts that have an "action" field
        validated = [
            a for a in actions
            if isinstance(a, dict) and "action" in a
        ]
        return validated or [{"action": "WAIT", "reasoning": "No valid actions in response"}]

    # ── Decision execution ────────────────────────────────────────────────────

    async def _execute_decision(self, actions: list[dict], briefing: dict) -> None:
        """Execute each action in sequence."""
        for action in actions:
            action_type = str(action.get("action", "")).upper()
            if action_type == "ENTER":
                await self._execute_enter(action, briefing)
            elif action_type == "EXIT":
                await self._execute_exit(action, briefing)
            elif action_type == "ADJUST":
                await self._execute_adjust(action, briefing)
            elif action_type == "WAIT":
                await self._execute_wait(action, briefing)
            else:
                print(f"[strategist] Unknown action type: {action_type}", file=sys.stderr)

    async def _execute_enter(self, action: dict, briefing: dict) -> None:
        pair             = action.get("pair", briefing.get("pair", "BTC-USDT-SWAP"))
        direction        = str(action.get("direction", "LONG")).upper()
        size_usd         = float(action.get("size_usd", 0))
        stop_loss        = float(action.get("stop_loss", 0))
        take_profit      = float(action.get("take_profit", 0))
        reasoning        = action.get("reasoning", "")
        confidence       = float(action.get("confidence", 0.5))
        hold_duration    = action.get("hold_duration", "unknown")
        # New structured fields (optional — default to None for backward compat)
        playbook         = action.get("playbook")
        time_horizon_days = action.get("time_horizon_days")
        catalyst         = action.get("catalyst")
        invalidation     = action.get("invalidation")
        primary_signal   = action.get("primary_signal")
        risk_reward      = action.get("risk_reward")
        market_condition = action.get("market_condition")

        portfolio       = briefing.get("portfolio", {})
        total_capital   = portfolio.get("total_capital",    config.STARTING_CAPITAL_USD)
        available       = portfolio.get("available_capital", total_capital)
        deployed        = portfolio.get("deployed_capital", 0.0)

        # ── Basic parameter validation ────────────────────────────────────────
        if size_usd <= 0:
            print("[strategist] ENTER skipped: size_usd <= 0", file=sys.stderr)
            return
        if stop_loss <= 0:
            print("[strategist] ENTER skipped: no stop loss set", file=sys.stderr)
            return

        # ── Hard risk kernel — fresh state from DB ────────────────────────────
        # Briefing snapshot may be stale; re-fetch immediately before placing.
        loop = asyncio.get_running_loop()
        try:
            fresh_trades = await loop.run_in_executor(
                None, lambda: db_logger.load_open_trades(config.PAPER_MODE)
            )
        except Exception as exc:
            print(f"[strategist] ENTER skipped: DB unavailable for risk check: {exc}", file=sys.stderr)
            await self.notifier.send(
                "⚠️ <b>Trade blocked</b> — DB unavailable for fresh risk check"
            )
            return
        try:
            portfolio_value = await loop.run_in_executor(
                None, self.order_manager.get_portfolio_value
            )
        except RuntimeError as exc:
            print(f"[strategist] ENTER blocked: {exc}", file=sys.stderr)
            return

        allowed, reason = await self._enforce_risk_limits(action, fresh_trades, portfolio_value)
        if not allowed:
            print(f"[strategist] ENTER blocked by risk kernel: {reason}", file=sys.stderr)
            return

        # Re-read size_usd — _enforce_risk_limits may have capped it
        size_usd = float(action.get("size_usd", size_usd))

        # ── Resolve entry price ───────────────────────────────────────────────
        prices     = briefing.get("prices", {})
        entry_price = None
        if pair in prices:
            entry_price = float(prices[pair].get("price") or 0) or None
        if not entry_price:
            price_info  = briefing.get("price", {})
            mark_price  = price_info.get("mark_price")
            entry_price = float(mark_price) if mark_price else None
        if not entry_price:
            print(f"[strategist] ENTER skipped: no entry price available for {pair}", file=sys.stderr)
            return

        # ── Validate stop and TP are on the correct side of entry ────────────
        if direction == "LONG":
            if stop_loss >= entry_price:
                msg = (
                    f"ENTER skipped: LONG stop_loss ({stop_loss:.2f}) >= "
                    f"entry_price ({entry_price:.2f})"
                )
                print(f"[strategist] {msg}", file=sys.stderr)
                await self.notifier.send(f"⚠️ <b>Trade blocked — invalid levels</b>\n{msg}")
                return
            if take_profit <= entry_price:
                msg = (
                    f"ENTER skipped: LONG take_profit ({take_profit:.2f}) <= "
                    f"entry_price ({entry_price:.2f})"
                )
                print(f"[strategist] {msg}", file=sys.stderr)
                await self.notifier.send(f"⚠️ <b>Trade blocked — invalid levels</b>\n{msg}")
                return
        else:  # SHORT
            if stop_loss <= entry_price:
                msg = (
                    f"ENTER skipped: SHORT stop_loss ({stop_loss:.2f}) <= "
                    f"entry_price ({entry_price:.2f})"
                )
                print(f"[strategist] {msg}", file=sys.stderr)
                await self.notifier.send(f"⚠️ <b>Trade blocked — invalid levels</b>\n{msg}")
                return
            if take_profit >= entry_price:
                msg = (
                    f"ENTER skipped: SHORT take_profit ({take_profit:.2f}) >= "
                    f"entry_price ({entry_price:.2f})"
                )
                print(f"[strategist] {msg}", file=sys.stderr)
                await self.notifier.send(f"⚠️ <b>Trade blocked — invalid levels</b>\n{msg}")
                return

        # ── Re-check feed health at execution time ────────────────────────────
        # Briefing may have been queued while feeds were healthy; recheck now.
        if self._health_monitor is not None:
            if not self._health_monitor.are_core_feeds_healthy():
                print(
                    f"[strategist] ENTER blocked — core feeds unhealthy at execution time",
                    file=sys.stderr,
                )
                await self.notifier.send(
                    f"⚠️ <b>Trade blocked at execution</b> — core feeds unhealthy\n"
                    f"{pair} {direction}"
                )
                return

        # ── Build order dict compatible with OrderManager.place_order() ───────
        cycle_result = {
            "pair":          pair,
            "signal":        direction,
            "entry_price":   entry_price,
            "order_size_usd": size_usd,
            "stop_loss":     stop_loss,
            "take_profit":   take_profit,
            "regime":        "CLAUDE_STRATEGIST",
        }

        try:
            fill_result = await loop.run_in_executor(
                None,
                lambda: self.order_manager.place_order(cycle_result),
            )
        except Exception as exc:
            print(f"[strategist] place_order error: {exc}", file=sys.stderr)
            return

        if fill_result.get("status") != "filled":
            error = fill_result.get("error", "unknown")
            # CRITICAL FIX 1: specific Telegram alerts per error type
            if error == "db_logging_failed":
                await self.notifier.send(
                    f"🚨 <b>CRITICAL: DB log failed after fill</b>\n"
                    f"{pair} {direction} — position emergency-closed. Verify OKX account."
                )
            elif error == "order_state_ambiguous":
                await self.notifier.send(
                    f"🚨 <b>CRITICAL: Order state ambiguous</b>\n"
                    f"{pair} {direction} — trading halted. "
                    f"Verify OKX position, then /resume."
                )
            elif error == "trading_halted_pending_reconciliation":
                await self.notifier.send(
                    f"⚠️ <b>Trade blocked — bot halted</b>\n"
                    f"Use /resume after verifying OKX state."
                )
            print(f"[strategist] Order not filled: {error}", file=sys.stderr)
            return

        trade_id = fill_result.get("trade_id", -1)

        # Use actual post-fill values — lot-step rounding and fill-price movement
        # may have changed qty, size, and stop/TP from the Claude-intended values.
        actual_entry    = fill_result.get("filled_price", entry_price)
        actual_size_usd = fill_result.get("filled_size_usd", size_usd)
        actual_stop     = fill_result.get("stop_loss", stop_loss)
        actual_tp       = fill_result.get("take_profit", take_profit)

        # ── Write thesis + structured fields to DB ────────────────────────────
        if trade_id > 0:
            await loop.run_in_executor(
                None,
                lambda: self._update_trade_metadata(
                    trade_id,
                    reasoning,
                    briefing.get("trigger_detail", ""),
                    playbook=playbook,
                    time_horizon_days=time_horizon_days,
                    catalyst=catalyst,
                    invalidation=invalidation,
                    primary_signal=primary_signal,
                    risk_reward=risk_reward,
                    market_condition=market_condition,
                ),
            )

        # ── Register with PositionMonitor for immediate monitoring ───────────
        if self._position_monitor is not None and trade_id > 0:
            self._position_monitor.register_position({
                "id":               trade_id,
                "pair":             pair,
                "direction":        direction,
                "entry_price":      actual_entry,
                "size_usd":         actual_size_usd,
                "stop_loss":        actual_stop,
                "take_profit":      actual_tp,
                "timestamp_open":   datetime.now(timezone.utc),
                "paper":            config.PAPER_MODE,
                "filled_qty":       fill_result.get("filled_qty"),
                "client_order_id":  fill_result.get("client_order_id"),
            })

        # ── Obsidian entry note ───────────────────────────────────────────────
        self._memory.write_trade_entry(
            {
                "id":               trade_id,
                "pair":             pair,
                "direction":        direction,
                "entry_price":      actual_entry,
                "size_usd":         actual_size_usd,
                "stop_loss":        actual_stop,
                "take_profit":      actual_tp,
                "trigger_event":    briefing.get("trigger_detail", ""),
                "playbook":         playbook,
                "time_horizon_days": time_horizon_days,
                "catalyst":         catalyst,
                "invalidation":     invalidation,
                "primary_signal":   primary_signal,
                "risk_reward":      risk_reward,
                "market_condition": market_condition,
            },
            reasoning,
        )

        # ── Telegram ──────────────────────────────────────────────────────────
        mode = "PAPER" if config.PAPER_MODE else "LIVE"
        playbook_str  = f" | Playbook: {playbook}" if playbook else ""
        horizon_str   = f" | Horizon: {time_horizon_days}d" if time_horizon_days else f" | Hold: {hold_duration}"
        rr_str        = f" | R/R: {risk_reward:.1f}" if risk_reward is not None else ""
        catalyst_str  = f"\nCatalyst: {catalyst}" if catalyst else ""
        invalid_str   = f"\nInvalidation: {invalidation}" if invalidation else ""
        size_note     = f" (intended ${size_usd:.2f})" if abs(actual_size_usd - size_usd) > 0.01 else ""
        msg = (
            f"🟢 <b>TRADE ENTERED</b> [{mode}]\n"
            f"<b>{direction} {pair}</b> @ ${actual_entry:,.2f}\n"
            f"Size: ${actual_size_usd:.2f}{size_note} | Stop: ${actual_stop:,.2f} | TP: ${actual_tp:,.2f}\n"
            f"Confidence: {confidence*100:.0f}%{playbook_str}{horizon_str}{rr_str}"
            f"{catalyst_str}{invalid_str}\n\n"
            f"💭 <i>{reasoning[:500]}</i>"
        )
        await self.notifier.send(msg)
        print(
            f"[strategist] ENTER: {direction} {pair} @ {actual_entry:.2f} "
            f"size=${actual_size_usd:.2f} stop={actual_stop:.2f} tp={actual_tp:.2f}",
            flush=True,
        )

    async def _execute_exit(self, action: dict, briefing: dict) -> None:
        position_id   = action.get("position_id")
        reasoning     = action.get("reasoning", "")
        exit_type     = action.get("exit_type")       # new structured field
        what_happened = action.get("what_happened")   # new structured field

        if position_id is None:
            print("[strategist] EXIT action missing position_id", file=sys.stderr)
            return

        # ── Find position ─────────────────────────────────────────────────────
        open_positions = briefing.get("open_positions", [])
        position = next(
            (
                p for p in open_positions
                if p.get("id") == position_id or p.get("trade_id") == position_id
            ),
            None,
        )
        if not position:
            print(f"[strategist] EXIT: position_id={position_id} not found", file=sys.stderr)
            return

        pair        = position.get("pair", "")
        direction   = position.get("direction", "LONG")
        entry_price = float(position.get("entry_price") or 0)
        size_usd    = float(position.get("size_usd") or 0)
        paper       = position.get("paper", config.PAPER_MODE)
        opened_at   = position.get("timestamp_open") or position.get("opened_at")

        # ── Resolve exit price from briefing (best estimate before exchange call) ─
        prices     = briefing.get("prices", {})
        if pair in prices:
            estimated_exit = float(prices[pair].get("price") or entry_price)
        else:
            price_info     = briefing.get("price", {})
            estimated_exit = float(price_info.get("mark_price") or entry_price) or entry_price

        close_reason = f"CLAUDE: {reasoning[:200]}"
        loop         = asyncio.get_running_loop()

        # ── Delegate to position_monitor (updates in-memory state too) ────────
        # _close_position is async, returns a result dict.  Only proceed with
        # vault note and Telegram after confirming the close succeeded.
        if self._position_monitor is not None:
            close_result = await self._position_monitor._close_position(
                position_id, estimated_exit, close_reason
            )
            if not close_result.get("success"):
                reason = close_result.get("reason", "unknown")
                print(
                    f"[strategist] EXIT failed for position_id={position_id}: {reason}",
                    file=sys.stderr,
                )
                await self.notifier.send(
                    f"⚠️ <b>Exit attempted but failed</b>\n"
                    f"trade_id={position_id} {pair} {direction}\n"
                    f"Reason: {reason}"
                )
                return  # No vault note, no success Telegram
            # Use actual values returned by close_position
            exit_price = close_result.get("exit_price", estimated_exit)
            pnl        = close_result.get("pnl", 0.0)
        else:
            # Fallback: write to DB directly (no position_monitor available)
            exit_price = estimated_exit
            if direction == "LONG":
                pnl = (exit_price - entry_price) / entry_price * size_usd if entry_price else 0
            else:
                pnl = (entry_price - exit_price) / entry_price * size_usd if entry_price else 0

            await loop.run_in_executor(
                None,
                lambda: db_logger.log_trade_close(
                    position_id, exit_price, pnl, close_reason
                ),
            )
            today = datetime.now(timezone.utc).date()
            await loop.run_in_executor(
                None,
                lambda: db_logger.upsert_daily_pnl(today, pnl, paper),
            )

        # ── Calculate hold duration ───────────────────────────────────────────
        duration = "unknown"
        if opened_at:
            try:
                if isinstance(opened_at, str):
                    opened_dt = datetime.fromisoformat(opened_at)
                else:
                    opened_dt = opened_at
                if not opened_dt.tzinfo:
                    opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                delta    = datetime.now(timezone.utc) - opened_dt
                hours    = int(delta.total_seconds() // 3600)
                mins     = int((delta.total_seconds() % 3600) // 60)
                duration = f"{hours}h {mins}m"
            except Exception:
                pass

        # ── Persist exit_type and what_happened to DB ────────────────────────
        if exit_type or what_happened:
            await loop.run_in_executor(
                None,
                lambda: self._update_trade_exit_metadata(
                    position_id,
                    exit_type=exit_type,
                    what_happened=what_happened,
                ),
            )

        # ── Obsidian post-mortem (only on confirmed success) ──────────────────
        self._memory.write_trade_exit(
            {
                "id":          position_id,
                "pair":        pair,
                "direction":   direction,
                "entry_price": entry_price,
                "exit_price":  exit_price,
                "pnl_usd":     pnl,
                "opened_at":   opened_at,
                "closed_at":   datetime.now(timezone.utc),
            },
            reasoning,
            reasoning,
        )

        # ── Telegram (only on confirmed success) ──────────────────────────────
        emoji = "🟢" if pnl >= 0 else "🔴"
        mode  = "PAPER" if config.PAPER_MODE else "LIVE"
        exit_type_str    = f" | Type: {exit_type}" if exit_type else ""
        what_happened_str = f"\n📋 {what_happened}" if what_happened else ""
        msg = (
            f"{emoji} <b>TRADE EXITED</b> [{mode}]\n"
            f"<b>{direction} {pair}</b>\n"
            f"Entry: ${entry_price:,.2f} → Exit: ${exit_price:,.2f}\n"
            f"P&L: {pnl:+.2f} USD | Held: {duration}{exit_type_str}"
            f"{what_happened_str}\n\n"
            f"💭 <i>{reasoning[:500]}</i>"
        )
        await self.notifier.send(msg)
        print(
            f"[strategist] EXIT: {pair} {direction} "
            f"entry={entry_price:.2f} exit={exit_price:.2f} pnl={pnl:+.2f}",
            flush=True,
        )

    async def _execute_adjust(self, action: dict, briefing: dict) -> None:
        """
        ADJUST is not supported — live exchange SL/TP orders cannot be amended
        atomically without cancelling and re-placing, which creates a window
        with no stop on the exchange.  Treat as WAIT with a Telegram warning.
        """
        position_id    = action.get("position_id")
        reasoning      = action.get("reasoning", "")
        open_positions = briefing.get("open_positions", [])
        position       = next(
            (p for p in open_positions
             if p.get("id") == position_id or p.get("trade_id") == position_id),
            {},
        )
        pair = position.get("pair", f"position_id={position_id}")
        msg = (
            f"⚠️ <b>ADJUST not executed</b>\n"
            f"{pair} — ADJUST is not supported until exchange SL/TP amendment "
            f"is implemented.\n<i>{reasoning[:300]}</i>"
        )
        await self.notifier.send(msg)
        print(
            f"[strategist] ADJUST requested for position_id={position_id} — "
            f"not supported, treating as WAIT",
            file=sys.stderr,
        )

    async def _enforce_risk_limits(
        self,
        action: dict,
        fresh_trades: list,
        portfolio_value: float,
    ) -> tuple:
        """
        Hard risk kernel called immediately before every ENTER order.

        Checks (in order):
          1. Daily loss cap
          2. Max simultaneous positions
          3. One position per symbol
          4. Min trade size
          5. Max trade size (caps action["size_usd"] rather than blocking)
          6. 50% total capital deployment

        Returns (True, "ok") if all limits pass.
        Returns (False, reason_str) if any limit blocks the trade.

        May mutate action["size_usd"] downward to the configured cap.
        """
        loop = asyncio.get_running_loop()

        # 0. DB state load failed at startup — block ENTERs until reconciled
        if (
            self._position_monitor is not None
            and getattr(self._position_monitor, "_db_load_failed", False)
        ):
            return False, "DB state load failed at startup — new ENTERs blocked until reconciled"

        # 1. Daily loss cap
        today_pnl = await loop.run_in_executor(None, self._get_todays_pnl)
        if today_pnl <= -config.DAILY_LOSS_CAP_USD:
            return False, f"Daily loss cap hit ({today_pnl:.2f} USD)"

        # 2. Max simultaneous positions
        if len(fresh_trades) >= config.MAX_SIMULTANEOUS_POSITIONS:
            return (
                False,
                f"Max positions reached ({len(fresh_trades)}/{config.MAX_SIMULTANEOUS_POSITIONS})",
            )

        # 3. One position per symbol
        if any(t["pair"] == action.get("pair") for t in fresh_trades):
            return False, f"Already have position in {action.get('pair')}"

        # 4. Min trade size
        size = float(action.get("size_usd", 0))
        if size < config.PORTFOLIO_MIN_TRADE_USD:
            return (
                False,
                f"Size ${size:.2f} below minimum ${config.PORTFOLIO_MIN_TRADE_USD:.2f}",
            )

        # 5. Max trade size cap (warn + adjust rather than block)
        if size > config.PORTFOLIO_MAX_TRADE_USD:
            print(
                f"[strategist] Size capped: ${size:.2f} → "
                f"${config.PORTFOLIO_MAX_TRADE_USD:.2f}",
                flush=True,
            )
            action["size_usd"] = config.PORTFOLIO_MAX_TRADE_USD
            size = config.PORTFOLIO_MAX_TRADE_USD

        # 6. 50% capital deployment cap (using potentially-capped size)
        fresh_deployed = sum(float(t.get("size_usd") or 0) for t in fresh_trades)
        if fresh_deployed + size > portfolio_value * 0.50:
            return (
                False,
                f"Would exceed 50% capital cap "
                f"(deployed=${fresh_deployed:.2f}, size=${size:.2f}, "
                f"cap=${portfolio_value:.2f})",
            )

        return True, "ok"

    async def _execute_wait(self, action: dict, briefing: dict) -> None:
        reasoning    = action.get("reasoning", "")
        trigger_type = briefing.get("trigger_type", "")

        # Only send Telegram for critical triggers
        if "critical" in trigger_type.lower() or "CRITICAL" in trigger_type:
            msg = (
                f"👁 <b>WATCHING — no action taken</b>\n"
                f"Trigger: {briefing.get('trigger_detail', '')}\n"
                f"<i>{reasoning[:300]}</i>"
            )
            await self.notifier.send(msg)

        print(f"[strategist] WAIT: {reasoning[:120]}", flush=True)

    # ── Decision logging ──────────────────────────────────────────────────────

    async def _log_decision(
        self,
        actions: list[dict],
        briefing: dict,
        raw_response: str,
    ) -> None:
        """Write to ticks table and event_log for debugging."""
        pair = briefing.get("pair", "PORTFOLIO")
        if briefing.get("pairs_affected"):
            pair = briefing["pairs_affected"][0]

        first_action = actions[0] if actions else {}

        loop = asyncio.get_running_loop()

        # Write to ticks table
        await loop.run_in_executor(
            None,
            lambda: db_logger.log_tick({
                "timestamp":       datetime.now(timezone.utc),
                "pair":            pair,
                "regime":          "CLAUDE_STRATEGIST",
                "decision":        first_action.get("action", "WAIT"),
                "decision_reason": str(first_action.get("reasoning", ""))[:500],
                "conviction_score": first_action.get("confidence"),
                "conviction_label": "claude",
            }),
        )

        # Write full raw response to event_log for debugging
        summary = f"actions={[a.get('action') for a in actions]} | trigger={briefing.get('trigger_type')}"
        await loop.run_in_executor(
            None,
            lambda: db_logger.log_event(
                event_type="strategist_decision",
                severity=1,
                description=f"{summary}\n\n{raw_response[:2000]}",
                source="strategist",
            ),
        )

    # ── Strategy memory update (called by self_improvement.py weekly) ────────

    async def update_strategy_memory(self) -> None:
        """
        Reviews the last 14 days of trades and asks Claude to update the
        strategy memory files in the Obsidian vault.
        Called weekly by self_improvement.py.
        """
        recent_trades = self._memory.get_recent_trades(days=14)
        if not recent_trades:
            print("[strategist] update_strategy_memory: no recent trades", flush=True)
            return

        trade_text = "\n\n---\n\n".join(recent_trades[:10])
        prompt = (
            "Review these recent trades and update the strategy memory.\n"
            "Return JSON (no markdown fences):\n"
            "{\n"
            '  "what_works": "patterns that led to winning trades",\n'
            '  "what_doesnt_work": "patterns that led to losing trades",\n'
            '  "current_thesis": "your current specific view on BTC/ETH/SOL for the '
            'next 7 days based on these trades and market conditions. Be specific: '
            'what direction, what price levels matter, what catalyst would change '
            'your view. This will be shown to you before every trade decision '
            'this week — make it actionable, not generic."\n'
            "}\n\n"
            f"Recent trades:\n{trade_text}"
        )

        raw = await self._call_claude(
            system="You are a crypto trading performance analyst reviewing trade history.",
            user=prompt,
        )

        try:
            text = raw.strip()
            if text.startswith("```"):
                text = text[text.find("\n") + 1 : text.rfind("```")].strip()
            parsed = json.loads(text)
            what_works     = parsed.get("what_works", "")
            what_doesnt    = parsed.get("what_doesnt_work", "")
            current_thesis = parsed.get("current_thesis", "")

            self._memory.update_strategy_memory(
                what_works     = what_works,
                what_doesnt    = what_doesnt,
                current_thesis = current_thesis,
            )

            # Write weekly thesis to macro_context_log so the dashboard
            # AI Brain tab can display it alongside daily macro briefs.
            database_url = os.environ.get("DATABASE_URL")
            if database_url:
                try:
                    from sqlalchemy import create_engine as _ce
                    from db.models import macro_context_log
                    _engine = _ce(database_url)
                    with _engine.begin() as conn:
                        conn.execute(
                            macro_context_log.insert().values(
                                date=datetime.now(timezone.utc).date(),
                                sentiment="WEEKLY_REVIEW",
                                confidence=0.8,
                                risk_multiplier=1.0,
                                reasoning=what_works[:2000],
                                watch_for=current_thesis[:200],
                            )
                        )
                    print(
                        "[strategist] Weekly thesis written to macro_context_log",
                        flush=True,
                    )
                except Exception as db_exc:
                    print(
                        f"[strategist] macro_context_log insert error: {db_exc}",
                        file=sys.stderr,
                    )

            await self.notifier.send("🧠 Strategy memory updated")
            print("[strategist] Strategy memory updated", flush=True)
        except Exception as exc:
            print(
                f"[strategist] update_strategy_memory parse error: {exc}",
                file=sys.stderr,
            )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_todays_pnl(self) -> float:
        """
        Query today's P&L.

        Priority:
          1. daily_pnl table (fast aggregate, updated by upsert_daily_pnl)
          2. trades table direct sum (fallback when daily_pnl unavailable)
          3. -DAILY_LOSS_CAP_USD * 2  (fail-safe: forces kill switch when
             both tables are unreachable — never silently allow new ENTERs
             with an unknown loss balance)
        """
        from sqlalchemy import create_engine, select, text as sa_text
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            print("[strategist] _get_todays_pnl: DATABASE_URL not set", file=sys.stderr)
            return -config.DAILY_LOSS_CAP_USD * 2

        today = datetime.now(timezone.utc).date()

        # Run BOTH queries independently so we always use the most conservative value.
        # If daily_pnl was written but trades SUM is larger (e.g. a race), we use
        # whichever is more negative.  Neither query short-circuits the other.
        aggregate: float | None = None
        computed:  float | None = None

        # ── Query 1: daily_pnl aggregate ──────────────────────────────────────
        try:
            engine = create_engine(database_url)
            with engine.connect() as conn:
                row = conn.execute(
                    select(daily_pnl_table.c.pnl_usd).where(
                        (daily_pnl_table.c.date == today) &
                        (daily_pnl_table.c.paper == config.PAPER_MODE)
                    )
                ).fetchone()
                aggregate = float(row[0]) if row else 0.0
        except Exception as exc:
            print(f"[strategist] _get_todays_pnl: daily_pnl unavailable — {exc}", file=sys.stderr)

        # ── Query 2: recompute from trades table ──────────────────────────────
        try:
            engine = create_engine(database_url)
            with engine.connect() as conn:
                row = conn.execute(
                    sa_text(
                        """
                        SELECT COALESCE(SUM(pnl_usd), 0)
                        FROM trades
                        WHERE paper = :paper
                          AND DATE(timestamp_close AT TIME ZONE 'UTC') = :today
                          AND timestamp_close IS NOT NULL
                          AND close_reason NOT IN (
                                'test_cleanup', 'manual_cleanup', 'pre_m7_cleanup'
                              )
                          AND NOT (
                                close_reason LIKE 'price_recon_pending:%'
                                AND reconciliation_pending = TRUE
                              )
                        """
                    ),
                    {"paper": config.PAPER_MODE, "today": today},
                ).fetchone()
                computed = float(row[0]) if row else 0.0
        except Exception as exc2:
            print(f"[strategist] _get_todays_pnl: trades query failed — {exc2}", file=sys.stderr)

        # ── Return most conservative (most negative) of the two ───────────────
        if aggregate is not None and computed is not None:
            return min(aggregate, computed)
        if aggregate is not None:
            return aggregate
        if computed is not None:
            return computed

        # ── Both failed — block all new ENTERs ───────────────────────────────
        print(
            "[strategist] _get_todays_pnl: both queries failed — blocking new ENTERs (fail-safe).",
            file=sys.stderr,
        )
        return -config.DAILY_LOSS_CAP_USD * 2

    def _get_macro_context(self) -> dict:
        """Return today's macro context from MacroContextEngine, or neutral defaults."""
        neutral = {
            "sentiment":       "NEUTRAL",
            "confidence":      0.5,
            "reasoning":       "No macro brief generated yet",
            "watch_for":       "",
            "upcoming_events": [],
        }
        if self.macro_context is None:
            return neutral
        ctx = getattr(self.macro_context, "_todays_context", None)
        if not ctx:
            return neutral
        return {
            "sentiment":       ctx.get("sentiment",  "NEUTRAL"),
            "confidence":      ctx.get("confidence", 0.5),
            "reasoning":       ctx.get("reasoning",  ""),
            "watch_for":       ctx.get("watch_for",  ""),
            "upcoming_events": ctx.get("key_events", []),
        }

    def _update_trade_metadata(
        self,
        trade_id: int,
        thesis: str,
        trigger_event: str,
        *,
        playbook: "str | None" = None,
        time_horizon_days: "float | None" = None,
        catalyst: "str | None" = None,
        invalidation: "str | None" = None,
        primary_signal: "str | None" = None,
        risk_reward: "float | None" = None,
        market_condition: "str | None" = None,
    ) -> None:
        """Write thesis, trigger_event, and structured decision fields to the trades row."""
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            return
        try:
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(database_url)
            params: dict = {
                "thesis":   thesis[:4000],
                "trigger":  trigger_event[:500],
                "id":       trade_id,
            }
            set_clauses = ["thesis = :thesis", "trigger_event = :trigger"]
            if playbook is not None:
                params["playbook"] = playbook[:30]
                set_clauses.append("playbook = :playbook")
            if time_horizon_days is not None:
                params["time_horizon_days"] = float(time_horizon_days)
                set_clauses.append("time_horizon_days = :time_horizon_days")
            if catalyst is not None:
                params["catalyst"] = str(catalyst)[:2000]
                set_clauses.append("catalyst = :catalyst")
            if invalidation is not None:
                params["invalidation"] = str(invalidation)[:2000]
                set_clauses.append("invalidation = :invalidation")
            if primary_signal is not None:
                params["primary_signal"] = str(primary_signal)[:2000]
                set_clauses.append("primary_signal = :primary_signal")
            if risk_reward is not None:
                params["risk_reward"] = float(risk_reward)
                set_clauses.append("risk_reward = :risk_reward")
            if market_condition is not None:
                params["market_condition"] = market_condition[:30]
                set_clauses.append("market_condition = :market_condition")
            with engine.begin() as conn:
                conn.execute(
                    sa_text(
                        f"UPDATE trades SET {', '.join(set_clauses)} WHERE id = :id"
                    ),
                    params,
                )
        except Exception as exc:
            print(f"[strategist] _update_trade_metadata error: {exc}", file=sys.stderr)

    def _update_trade_exit_metadata(
        self,
        trade_id: int,
        *,
        exit_type: "str | None" = None,
        what_happened: "str | None" = None,
    ) -> None:
        """Write exit_type and what_happened to the trades row after a confirmed close."""
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            return
        params: dict = {"id": trade_id}
        set_clauses: list[str] = []
        if exit_type is not None:
            params["exit_type"] = exit_type[:30]
            set_clauses.append("exit_type = :exit_type")
        if what_happened is not None:
            params["what_happened"] = str(what_happened)[:4000]
            set_clauses.append("what_happened = :what_happened")
        if not set_clauses:
            return
        try:
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(database_url)
            with engine.begin() as conn:
                conn.execute(
                    sa_text(
                        f"UPDATE trades SET {', '.join(set_clauses)} WHERE id = :id"
                    ),
                    params,
                )
        except Exception as exc:
            print(f"[strategist] _update_trade_exit_metadata error: {exc}", file=sys.stderr)

    def _update_position_levels(self, trade_id: int, updates: dict) -> None:
        """Update stop_loss / take_profit on an open trade row."""
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            return
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(database_url)
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            with engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE trades SET {set_clause} WHERE id = :id"),
                    {**updates, "id": trade_id},
                )
        except Exception as exc:
            print(f"[strategist] _update_position_levels error: {exc}", file=sys.stderr)
