"""
Scanner — the bot's always-on eyes and ears.

Replaces the 15-minute fixed-interval DecisionLoop with an event-driven model.
Gemini Flash monitors multiple data streams simultaneously and wakes Claude
(the Strategist) only when something significant happens.

Monitoring coroutines
---------------------
_monitor_price_action()     60s — 1H candles, key level breaks, large moves,
                                   volume spikes, routine 4H review
_monitor_news()             90s — Gemini triages headlines into 5 urgency tiers
_monitor_macro_calendar()    5m — pre-event briefings at 2H and 15min warnings
_monitor_funding_extremes() 10m — extreme funding rates
_monitor_iv_spikes()         5m — Deribit DVOL spike detection

When a trigger fires, _compile_and_send_briefing() gathers all available
context (price, regime, funding, IV, open positions, strategy memory, recent
news) and calls strategist.make_decision(briefing) if a Strategist is wired.

Strategist integration
----------------------
Pass strategist=None during construction; set scanner.strategist later once
the Strategist instance exists. The scanner guards every call with
`if self.strategist is not None` so startup order does not matter.
"""

import asyncio
import json
import os
import sys
import warnings
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

import config

if TYPE_CHECKING:
    from feeds.okx_ws import OKXWebSocketManager
    from feeds.news_feed import NewsFeed
    from feeds.macro_calendar import MacroCalendar
    from feeds.deribit_feed import DeribitFeed
    from feeds.feed_health_monitor import FeedHealthMonitor
    from engine.memory import MemoryManager

# ── Gemini news triage prompt ─────────────────────────────────────────────────

_TRIAGE_SYSTEM = (
    "You are a crypto trading signal monitor. Given a list of recent news "
    "headlines, classify the overall urgency for a BTC/ETH/SOL perpetual "
    "futures trader as one of exactly five labels:\n"
    "  ignore   — no market-moving relevance\n"
    "  routine  — background information, no immediate action needed\n"
    "  notable  — worth monitoring, mild market impact possible\n"
    "  urgent   — significant market event, review positions now\n"
    "  critical — severe market shock, immediate action required\n\n"
    "Also classify the overall price direction implied by the headlines:\n"
    "  bullish — positive for crypto prices\n"
    "  bearish — negative for crypto prices\n"
    "  neutral — mixed or unclear direction\n\n"
    "Return ONLY a JSON object with three keys:\n"
    '  {"urgency": "<label>", "reason": "<one sentence summary>", '
    '"direction": "<bullish|bearish|neutral>"}\n'
    "No markdown, no extra text."
)

# Number of headlines to include in each triage call
_TRIAGE_HEADLINE_COUNT = 10

# Minimum urgency level that triggers a Claude briefing
_BRIEFING_URGENCY_LEVELS = {"notable", "urgent", "critical"}

# Key level proximity — trigger if price within this % of a stored level
_KEY_LEVEL_PROXIMITY_PCT = 0.005   # 0.5%

# Large candle threshold — 1H close moves this % from open
_LARGE_CANDLE_PCT = 0.015          # 1.5%

# Volume spike — current bar volume >= this multiple of 20-bar average
_VOLUME_SPIKE_MULTIPLE = 2.5

# Routine 4H review interval (seconds)
_ROUTINE_REVIEW_INTERVAL_S = 4 * 3600

# Funding extreme thresholds (per 8h period, i.e. what the API returns)
_FUNDING_HIGH_THRESHOLD  =  0.001   # +0.10% — crowded long
_FUNDING_LOW_THRESHOLD   = -0.0005  # -0.05% — crowded short


def _ema_series(prices: list[float], period: int) -> list[float]:
    """Return the full EMA series for a list of prices."""
    if not prices:
        return []
    k = 2.0 / (period + 1)
    out = [prices[0]]
    for p in prices[1:]:
        out.append(p * k + out[-1] * (1.0 - k))
    return out


def calculate_ema(prices: list, period: int) -> float:
    """Final EMA value for a list of prices."""
    series = _ema_series([float(p) for p in prices if p is not None], period)
    return series[-1] if series else 0.0


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    """
    Wilder RSI over the last `period` price changes.
    Returns 50.0 if not enough data.
    """
    if len(closes) < period + 1:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
    gains  = [max(0.0, d) for d in diffs]
    losses = [max(0.0, -d) for d in diffs]
    avg_gain = sum(gains)  / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 1)


def calculate_atr(candles: list[dict], period: int = 14) -> float:
    """Average True Range over the last `period` bars."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(len(candles) - period, len(candles)):
        h  = float(candles[i].get("high")  or 0)
        lo = float(candles[i].get("low")   or 0)
        pc = float(candles[i - 1].get("close") or 0)
        if h > 0 and lo > 0 and pc > 0:
            trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return round(sum(trs) / len(trs), 2) if trs else 0.0


def calculate_macd(
    closes: list[float],
) -> tuple[float, float, float]:
    """
    Standard MACD (12, 26, 9).
    Returns (macd_line, signal_line, histogram).
    Returns (0, 0, 0) if not enough data.
    """
    if len(closes) < 26:
        return 0.0, 0.0, 0.0
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_s = [a - b for a, b in zip(ema12, ema26)]
    macd_line = macd_s[-1]
    signal_input = macd_s[-20:] if len(macd_s) >= 20 else macd_s
    signal = _ema_series(signal_input, 9)[-1] if signal_input else 0.0
    return round(macd_line, 2), round(signal, 2), round(macd_line - signal, 2)


class Scanner:
    """
    Always-on market scanner powered by Gemini Flash.

    Wakes the Strategist (Claude Sonnet) only when something meaningful occurs,
    replacing the fixed 15-minute polling loop.
    """

    def __init__(
        self,
        ws_manager: "OKXWebSocketManager",
        news_feed: "NewsFeed",
        macro_calendar: "MacroCalendar",
        deribit_feed: "DeribitFeed",
        health_monitor: "FeedHealthMonitor",
        memory: "MemoryManager",
        strategist=None,
        position_monitor=None,
    ) -> None:
        self._ws              = ws_manager
        self._news            = news_feed
        self._calendar        = macro_calendar
        self._deribit         = deribit_feed
        self._monitor         = health_monitor
        self._memory          = memory
        self.strategist       = strategist        # set after construction if needed
        self.position_monitor = position_monitor  # set after construction if needed
        self.macro_context    = None              # set via main.py after construction

        # Gemini client for news triage (initialised lazily)
        self._gemini = None
        self._init_gemini()

        # Track last-seen values to detect changes
        self._last_macro_alerts: dict[str, datetime] = {}   # event_id → last alerted
        self._last_routine_ts:   datetime = datetime.min.replace(tzinfo=timezone.utc)

        # Key levels loaded from Obsidian vault at startup
        self._key_levels: dict[str, list[float]] = {}       # pair → sorted price levels

        # Candle tracking for volume spike detection
        self._vol_history:  dict[str, list[float]] = {}     # pair → recent 1H volumes
        self._last_candle_close: dict[str, int] = {}        # pair → last seen close_time

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init_gemini(self) -> None:
        """Initialise Gemini Flash for news triage. Silent on missing key."""
        gemini_key = os.getenv("GEMINI_API_KEY", "")
        if not gemini_key:
            print(
                "[scanner] GEMINI_API_KEY not set — news triage will skip Gemini",
                file=sys.stderr,
            )
            return
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                import google.generativeai as genai
                genai.configure(api_key=gemini_key)
                self._gemini = genai.GenerativeModel(
                    config.GEMINI_MODEL,
                    system_instruction=_TRIAGE_SYSTEM,
                )
            print(f"[scanner] Gemini triage ready: model={config.GEMINI_MODEL}", flush=True)
        except Exception as exc:
            print(f"[scanner] Gemini init error: {exc}", file=sys.stderr)

    def _load_key_levels(self) -> None:
        """
        Load stored price levels from the Obsidian vault patterns/ directory.
        Each file in patterns/ may contain lines like: "BTC-USDT-SWAP: 84000, 85500"
        Falls back silently if no files found.
        """
        try:
            patterns_dir = self._memory.vault_path / "patterns"
            for fpath in patterns_dir.glob("*.md"):
                try:
                    for line in fpath.read_text(encoding="utf-8").splitlines():
                        for pair in config.TRADING_PAIRS:
                            prefix = f"{pair}:"
                            if line.strip().startswith(prefix):
                                levels_str = line.strip()[len(prefix):].strip()
                                levels = [
                                    float(v.strip())
                                    for v in levels_str.split(",")
                                    if v.strip()
                                ]
                                existing = self._key_levels.get(pair, [])
                                self._key_levels[pair] = sorted(
                                    set(existing + levels)
                                )
                except Exception:
                    pass
            if self._key_levels:
                print(f"[scanner] Key levels loaded: {self._key_levels}", flush=True)
        except Exception as exc:
            print(f"[scanner] Key level load error: {exc}", file=sys.stderr)

    # ── Main entry point ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start all monitoring coroutines concurrently.
        Waits until core feeds are healthy (checks every 5s, max 60s).
        """
        for _ in range(12):
            await asyncio.sleep(5)
            if (
                self._monitor is not None
                and self._monitor.are_core_feeds_healthy()
            ):
                break
        self._load_key_levels()
        print("[scanner] Starting monitoring coroutines...", flush=True)

        await asyncio.gather(
            self._monitor_price_action(),
            self._monitor_news(),
            self._monitor_macro_calendar(),
            self._monitor_funding_extremes(),
            self._monitor_iv_spikes(),
        )

    # ── Price action monitor ──────────────────────────────────────────────────

    async def _monitor_price_action(self) -> None:
        """
        Poll 1H closed candles every 60s.
        Triggers on:
          - Key level break (price within 0.5% of a stored level)
          - Large candle (1H move >= 1.5%)
          - Volume spike (current bar volume >= 2.5× 20-bar average)
          - Routine 4H review
        """
        while True:
            try:
                for pair in config.TRADING_PAIRS:
                    candles_1h = self._ws.get_closed_candles(pair, "1H")
                    if len(candles_1h) < 2:
                        continue

                    latest = candles_1h[-1]
                    ct = latest.get("close_time", 0)

                    # Skip if we've already processed this bar
                    if ct == self._last_candle_close.get(pair, 0):
                        continue
                    self._last_candle_close[pair] = ct

                    o_price = float(latest.get("open", 0))
                    c_price = float(latest.get("close", 0))
                    volume  = float(latest.get("volume", 0))

                    if c_price <= 0:
                        continue

                    # Update rolling volume history (keep last 20 bars)
                    hist = self._vol_history.get(pair, [])
                    hist.append(volume)
                    if len(hist) > 20:
                        hist = hist[-20:]
                    self._vol_history[pair] = hist

                    # ── Key level break ───────────────────────────────────────
                    levels = self._key_levels.get(pair, [])
                    for level in levels:
                        if abs(c_price - level) / level <= _KEY_LEVEL_PROXIMITY_PCT:
                            await self._compile_and_send_briefing(
                                pair,
                                trigger_type="KEY_LEVEL",
                                trigger_detail=(
                                    f"Price {c_price:.2f} within 0.5% of key level "
                                    f"{level:.2f}"
                                ),
                            )
                            break   # one briefing per bar per pair

                    # ── Large candle ──────────────────────────────────────────
                    if o_price > 0:
                        candle_move = abs(c_price - o_price) / o_price
                        if candle_move >= _LARGE_CANDLE_PCT:
                            direction = "UP" if c_price > o_price else "DOWN"
                            await self._compile_and_send_briefing(
                                pair,
                                trigger_type="LARGE_CANDLE",
                                trigger_detail=(
                                    f"1H candle moved {candle_move*100:.1f}% "
                                    f"{direction} (open={o_price:.2f}, "
                                    f"close={c_price:.2f})"
                                ),
                            )
                            continue   # large candle takes priority, skip vol check

                    # ── Volume spike ──────────────────────────────────────────
                    if len(hist) >= 5:
                        avg_vol = sum(hist[:-1]) / len(hist[:-1])
                        if avg_vol > 0 and volume >= avg_vol * _VOLUME_SPIKE_MULTIPLE:
                            await self._compile_and_send_briefing(
                                pair,
                                trigger_type="VOLUME_SPIKE",
                                trigger_detail=(
                                    f"1H volume {volume:.0f} is "
                                    f"{volume/avg_vol:.1f}× the 20-bar average "
                                    f"({avg_vol:.0f})"
                                ),
                            )

                # ── Routine 4H review ─────────────────────────────────────────
                now = datetime.now(timezone.utc)
                if (now - self._last_routine_ts).total_seconds() >= _ROUTINE_REVIEW_INTERVAL_S:
                    self._last_routine_ts = now
                    for pair in config.TRADING_PAIRS:
                        await self._compile_and_send_briefing(
                            pair,
                            trigger_type="ROUTINE_4H",
                            trigger_detail="Scheduled 4-hour market review",
                        )

                # ── Large loss check ──────────────────────────────────────────
                # Trigger an urgent Claude review if any open position is down
                # more than 8% of its position size (not a stop-loss event but
                # a meaningful drawdown that may invalidate the thesis).
                if self.position_monitor is not None:
                    for pos in self.position_monitor.get_open_positions():
                        size_usd = float(pos.get("size_usd") or 0)
                        if size_usd <= 0:
                            continue
                        loss_pct = float(pos.get("unrealised_pnl") or 0) / size_usd
                        if loss_pct <= -0.08:
                            trade_id = pos.get("trade_id")
                            if trade_id:
                                await self.position_monitor.trigger_urgent_review(
                                    trade_id,
                                    f"{pos.get('direction')} {pos.get('pair')} "
                                    f"position down {loss_pct*100:.1f}% "
                                    f"— large loss detected, reviewing thesis",
                                )

            except Exception as exc:
                print(f"[scanner] _monitor_price_action error: {exc}", file=sys.stderr)

            await asyncio.sleep(60)

    # ── News monitor ──────────────────────────────────────────────────────────

    async def _monitor_news(self) -> None:
        """
        Fetch latest headlines every 90s, triage with Gemini.
        Triggers a briefing for 'notable', 'urgent', or 'critical' events.
        'ignore' and 'routine' are suppressed.
        Skips the Gemini call entirely when headlines haven't changed since last poll.
        """
        _last_triggered_reason: str = ""
        _last_headlines_hash: int = 0

        while True:
            try:
                headlines = self._news.get_latest_headlines(count=_TRIAGE_HEADLINE_COUNT)
                if headlines:
                    headlines_hash = hash(tuple(headlines))
                    if headlines_hash != _last_headlines_hash:
                        _last_headlines_hash = headlines_hash
                        urgency, reason, direction = await self._triage_headlines(
                            headlines
                        )
                        if (
                            urgency in _BRIEFING_URGENCY_LEVELS
                            and reason != _last_triggered_reason
                        ):
                            _last_triggered_reason = reason

                            # Feed the news catalyst scorer with this event
                            if self.macro_context is not None:
                                try:
                                    await self.macro_context.log_news_event(
                                        headline=reason,
                                        urgency=urgency,
                                        direction=direction,
                                    )
                                except Exception as _mc_exc:
                                    print(
                                        f"[scanner] log_news_event error: {_mc_exc}",
                                        file=sys.stderr,
                                    )

                            for pair in config.TRADING_PAIRS:
                                await self._compile_and_send_briefing(
                                    pair,
                                    trigger_type=f"NEWS_{urgency.upper()}",
                                    trigger_detail=reason,
                                )
                            # Critical news: also trigger urgent review on every
                            # open position so Claude can decide whether to exit
                            # immediately rather than wait for the next briefing.
                            if urgency == "critical" and self.position_monitor is not None:
                                for pos in self.position_monitor.get_open_positions():
                                    trade_id = pos.get("trade_id")
                                    if trade_id:
                                        await self.position_monitor.trigger_urgent_review(
                                            trade_id,
                                            f"Critical news: {reason}",
                                        )
            except Exception as exc:
                print(f"[scanner] _monitor_news error: {exc}", file=sys.stderr)

            await asyncio.sleep(90)

    async def _triage_headlines(
        self, headlines: list[str]
    ) -> tuple[str, str, str]:
        """
        Use Gemini Flash to classify headline urgency and direction.
        Returns (urgency_label, reason_string, direction).
        Falls back to ('routine', message, 'neutral') if Gemini unavailable.
        """
        if self._gemini is None:
            return "routine", "Gemini unavailable — cannot triage headlines", "neutral"

        user_content = "\n".join(f"- {h}" for h in headlines)
        try:
            loop = asyncio.get_running_loop()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                import google.generativeai as genai
                response = await loop.run_in_executor(
                    None,
                    lambda: self._gemini.generate_content(
                        user_content,
                        generation_config=genai.types.GenerationConfig(
                            response_mime_type="application/json"
                        ),
                    ),
                )
            parsed    = json.loads(response.text)
            urgency   = str(parsed.get("urgency",   "routine")).lower()
            reason    = str(parsed.get("reason",    ""))
            direction = str(parsed.get("direction", "neutral")).lower()
            if urgency not in {"ignore", "routine", "notable", "urgent", "critical"}:
                urgency = "routine"
            if direction not in {"bullish", "bearish", "neutral"}:
                direction = "neutral"
            return urgency, reason, direction
        except Exception as exc:
            print(f"[scanner] Gemini triage error: {exc}", file=sys.stderr)
            return "routine", f"Triage error: {exc}", "neutral"

    # ── Macro calendar monitor ────────────────────────────────────────────────

    async def _monitor_macro_calendar(self) -> None:
        """
        Check for upcoming macro events every 5 minutes.
        Sends pre-event briefings at 2H window and 15min warning.
        """
        while True:
            try:
                now = datetime.now(timezone.utc)
                events = self._calendar.get_upcoming_events(days=1)
                for event in events:
                    event_date = event["date"]
                    # get_upcoming_events() always returns a date object; use noon UTC
                    event_dt = datetime(
                        event_date.year, event_date.month, event_date.day,
                        12, 0, 0, tzinfo=timezone.utc
                    )

                    mins_away = (event_dt - now).total_seconds() / 60
                    event_id  = f"{event['type']}_{event_date}"

                    last_alerted = self._last_macro_alerts.get(event_id)

                    # 2-hour warning (between 130min and 120min away)
                    if 120 <= mins_away <= 130:
                        if last_alerted is None or (now - last_alerted).total_seconds() > 3600:
                            self._last_macro_alerts[event_id] = now
                            detail = (
                                f"Macro event in ~2 hours: {event['type']} — "
                                f"{event['description']}"
                            )
                            for pair in config.TRADING_PAIRS:
                                await self._compile_and_send_briefing(
                                    pair,
                                    trigger_type="MACRO_2H_WARNING",
                                    trigger_detail=detail,
                                )

                    # 15-minute warning (between 20min and 15min away)
                    elif 15 <= mins_away <= 20:
                        alert_key = f"{event_id}_15min"
                        last_15 = self._last_macro_alerts.get(alert_key)
                        if last_15 is None or (now - last_15).total_seconds() > 1800:
                            self._last_macro_alerts[alert_key] = now
                            detail = (
                                f"Macro event in ~15 minutes: {event['type']} — "
                                f"{event['description']}"
                            )
                            for pair in config.TRADING_PAIRS:
                                await self._compile_and_send_briefing(
                                    pair,
                                    trigger_type="MACRO_15MIN_WARNING",
                                    trigger_detail=detail,
                                )

            except Exception as exc:
                print(f"[scanner] _monitor_macro_calendar error: {exc}", file=sys.stderr)

            await asyncio.sleep(300)

    # ── Funding extreme monitor ───────────────────────────────────────────────

    async def _monitor_funding_extremes(self) -> None:
        """
        Check funding rates every 10 minutes.
        Triggers when funding > 0.10% (crowded long) or < -0.05% (crowded short).
        """
        _last_funding_alert: dict[str, datetime] = {}

        while True:
            try:
                for pair in config.TRADING_PAIRS:
                    ticker = self._ws.get_latest_ticker(pair)
                    if not ticker:
                        continue
                    funding = ticker.get("funding_rate")
                    if funding is None:
                        continue
                    try:
                        funding_f = float(funding)
                    except (TypeError, ValueError):
                        continue

                    last_alert = _last_funding_alert.get(pair)
                    now = datetime.now(timezone.utc)

                    # Deduplicate: at most one alert per pair per hour
                    if last_alert and (now - last_alert).total_seconds() < 3600:
                        continue

                    if funding_f >= _FUNDING_HIGH_THRESHOLD:
                        _last_funding_alert[pair] = now
                        await self._compile_and_send_briefing(
                            pair,
                            trigger_type="FUNDING_EXTREME_LONG",
                            trigger_detail=(
                                f"Funding rate {funding_f*100:.4f}% is extremely "
                                f"positive — crowded long, potential mean-reversion"
                            ),
                        )
                    elif funding_f <= _FUNDING_LOW_THRESHOLD:
                        _last_funding_alert[pair] = now
                        await self._compile_and_send_briefing(
                            pair,
                            trigger_type="FUNDING_EXTREME_SHORT",
                            trigger_detail=(
                                f"Funding rate {funding_f*100:.4f}% is extremely "
                                f"negative — crowded short, potential squeeze"
                            ),
                        )
            except Exception as exc:
                print(f"[scanner] _monitor_funding_extremes error: {exc}", file=sys.stderr)

            await asyncio.sleep(600)

    # ── IV spike monitor ──────────────────────────────────────────────────────

    async def _monitor_iv_spikes(self) -> None:
        """
        Check Deribit DVOL for spikes every 5 minutes.
        Triggers when the latest 1H DVOL close is >= 20% above the prior close.
        """
        _last_iv_alert: dict[str, datetime] = {}

        while True:
            try:
                for pair in config.TRADING_PAIRS:
                    spiked = await self._deribit.detect_iv_spike(pair)
                    if not spiked:
                        continue

                    last_alert = _last_iv_alert.get(pair)
                    now = datetime.now(timezone.utc)

                    # Deduplicate: at most one alert per pair per 2 hours
                    if last_alert and (now - last_alert).total_seconds() < 7200:
                        continue

                    _last_iv_alert[pair] = now
                    iv = await self._deribit.get_iv_for_pair(pair)
                    iv_str = f"{iv:.1f}" if iv is not None else "N/A"
                    await self._compile_and_send_briefing(
                        pair,
                        trigger_type="IV_SPIKE",
                        trigger_detail=(
                            f"DVOL spike detected for {pair} — "
                            f"current IV={iv_str}, "
                            f"increase >= {config.DERIBIT_IV_SPIKE_PCT*100:.0f}% "
                            "in the last hour"
                        ),
                    )
            except Exception as exc:
                print(f"[scanner] _monitor_iv_spikes error: {exc}", file=sys.stderr)

            await asyncio.sleep(300)

    # ── Briefing compiler ─────────────────────────────────────────────────────

    async def _compile_and_send_briefing(
        self,
        pair: str,
        trigger_type: str,
        trigger_detail: str,
    ) -> None:
        """
        Compile a comprehensive briefing from all available data sources and
        hand it to the Strategist for a decision.

        Per pair:
          - 100 closed 1H candles → EMA-20/50, RSI-14, MACD, ATR-14, volume ratio
          - 1H/4H/24H price changes
          - 4H candles synthesised from 1H data
          - Order book depth and imbalance
          - Funding rate, sentiment, annualised %
        Global:
          - BTC and ETH DVOL concurrently
          - Open positions (all DB columns incl. thesis/stop/tp)
          - 15 Gemini-filtered news headlines
          - Upcoming macro events (next 3 days)
          - Obsidian strategy memory + recent trades
        """
        if self.strategist is None:
            return

        # Skip briefing if core feeds are degraded — stale data leads to bad decisions
        if self._monitor is not None and not self._monitor.are_core_feeds_healthy():
            print(
                f"[scanner] Briefing skipped (core feeds degraded): "
                f"trigger={trigger_type}",
                file=sys.stderr,
            )
            return

        now = datetime.now(timezone.utc)
        print(
            f"[scanner] Briefing → {pair} | trigger={trigger_type} | "
            f"{trigger_detail[:80]}",
            flush=True,
        )

        # ── Per-pair technical, price and book data ───────────────────────────
        technical:    dict = {}
        prices_dict:  dict = {}
        funding_dict: dict = {}

        for scan_pair in config.TRADING_PAIRS:

            # Ticker
            ticker        = self._ws.get_latest_ticker(scan_pair)
            current_price: Optional[float] = None
            funding_rate:  Optional[float] = None
            bid: Optional[float] = None
            ask: Optional[float] = None
            if ticker:
                current_price = ticker.get("mark_price") or ticker.get("last")
                funding_rate  = ticker.get("funding_rate")
                bid           = ticker.get("bid")
                ask           = ticker.get("ask")

            # 100 closed 1H candles
            all_candles = self._ws.get_closed_candles(scan_pair, "1H")
            candles     = all_candles[-100:]
            n           = len(candles)
            degraded    = n < 10

            # OHLCV float lists
            closes  = [float(c["close"])  for c in candles if c.get("close")]
            highs   = [float(c["high"])   for c in candles if c.get("high")]
            lows    = [float(c["low"])    for c in candles if c.get("low")]
            volumes = [float(c["volume"]) for c in candles if c.get("volume") is not None]

            # Price changes vs closed bars
            def _chg(bars_ago: int) -> Optional[float]:
                if current_price and len(closes) >= bars_ago and closes[-bars_ago]:
                    return round((current_price - closes[-bars_ago]) / closes[-bars_ago] * 100, 2)
                return None

            change_1h_pct  = _chg(1)
            change_4h_pct  = _chg(4)
            change_24h_pct = _chg(24)

            # EMAs
            ema_20 = calculate_ema(closes, 20) if len(closes) >= 2 else None
            ema_50 = calculate_ema(closes, 50) if len(closes) >= 2 else None

            # RSI-14
            rsi_14 = calculate_rsi(closes, 14) if len(closes) >= 15 else None

            # MACD
            if len(closes) >= 26:
                macd_line, macd_signal, macd_hist = calculate_macd(closes)
            else:
                macd_line = macd_signal = macd_hist = None

            # ATR-14
            atr_14 = calculate_atr(candles, 14) if len(candles) >= 15 else None

            # Volume SMA-20 and ratio
            volume_sma_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else None
            volume_ratio  = (
                round(volumes[-1] / volume_sma_20, 2)
                if volume_sma_20 and volume_sma_20 > 0 and volumes
                else None
            )

            # Trend labels
            if current_price and ema_20 and ema_50:
                trend        = "UP"    if ema_20 > ema_50       else "DOWN"
                vs_ema20     = "ABOVE" if current_price > ema_20 else "BELOW"
                vs_ema50     = "ABOVE" if current_price > ema_50 else "BELOW"
            else:
                trend = vs_ema20 = vs_ema50 = "N/A"

            # Key levels
            high_48h = max(highs[-48:]) if len(highs) >= 2 else None
            low_48h  = min(lows[-48:])  if len(lows)  >= 2 else None
            high_24h = max(highs[-24:]) if len(highs) >= 2 else None
            low_24h  = min(lows[-24:])  if len(lows)  >= 2 else None
            last_6_closes = [round(c, 2) for c in closes[-6:]] if closes else []

            # Synthesise 4H candles from last 48 1H bars (complete groups only)
            h1_slice = candles[-48:]
            groups   = [h1_slice[i:i + 4] for i in range(0, len(h1_slice), 4)]
            complete = [g for g in groups if len(g) == 4][-6:]
            last_6_4h: list[dict] = []
            for g in complete:
                g_highs   = [float(c.get("high")   or 0) for c in g]
                g_lows    = [float(c.get("low")    or 0) for c in g]
                g_closes  = [float(c.get("close")  or 0) for c in g]
                g_volumes = [float(c.get("volume") or 0) for c in g]
                ts = g[0].get("open_time", 0)
                ts_str = (
                    datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                    .strftime("%m-%d %H:%M")
                    if ts else "?"
                )
                last_6_4h.append({
                    "timestamp": ts_str,
                    "open":   round(float(g[0].get("open") or 0), 2),
                    "high":   round(max(g_highs),  2),
                    "low":    round(min(g_lows),   2),
                    "close":  round(g_closes[-1],  2),
                    "volume": round(sum(g_volumes), 0),
                })

            # Order book depth + imbalance
            book_spread_pct = bid_depth_1pct = ask_depth_1pct = book_imbalance = None
            ob = self._ws.get_orderbook(scan_pair)
            if ob and bid and ask and bid > 0:
                mid = (bid + ask) / 2
                book_spread_pct = round((ask - bid) / bid * 100, 4)
                bid_depth = sum(qty for px, qty in ob["bids"].items() if px >= mid * 0.99)
                ask_depth = sum(qty for px, qty in ob["asks"].items() if px <= mid * 1.01)
                bid_depth_1pct = round(bid_depth, 2)
                ask_depth_1pct = round(ask_depth, 2)
                total = bid_depth + ask_depth
                book_imbalance = round(bid_depth / total, 3) if total > 0 else 0.5

            # Funding enrichment
            if funding_rate is not None:
                fr = float(funding_rate)
                funding_dict[scan_pair]  = fr
                funding_sentiment        = "LONGS_PAYING"  if fr >= 0 else "SHORTS_PAYING"
                funding_annualised_pct   = round(fr * 3 * 365 * 100, 1)
            else:
                funding_sentiment      = "N/A"
                funding_annualised_pct = None

            technical[scan_pair] = {
                # Rich indicators
                "current_price":         current_price,
                "change_1h_pct":         change_1h_pct,
                "change_4h_pct":         change_4h_pct,
                "change_24h_pct":        change_24h_pct,
                "ema_20":                round(ema_20, 2) if ema_20 else None,
                "ema_50":                round(ema_50, 2) if ema_50 else None,
                "rsi_14":                rsi_14,
                "macd_line":             macd_line,
                "macd_signal":           macd_signal,
                "macd_histogram":        macd_hist,
                "atr_14":                atr_14,
                "volume_sma_20":         round(volume_sma_20, 2) if volume_sma_20 else None,
                "volume_ratio":          volume_ratio,
                "trend":                 trend,
                "price_vs_ema20":        vs_ema20,
                "price_vs_ema50":        vs_ema50,
                "high_48h":              high_48h,
                "low_48h":               low_48h,
                "high_24h":              high_24h,
                "low_24h":              low_24h,
                "last_6_closes":         last_6_closes,
                "last_6_4h_candles":     last_6_4h,
                "funding_sentiment":     funding_sentiment,
                "funding_annualised_pct": funding_annualised_pct,
                "book_spread_pct":       book_spread_pct,
                "bid_depth_1pct":        bid_depth_1pct,
                "ask_depth_1pct":        ask_depth_1pct,
                "book_imbalance":        book_imbalance,
                "candles_available":     n,
                "degraded":              degraded,
                "degraded_reason":       f"Only {n} 1H bars available" if degraded else "",
                # Backward-compat keys used by older prompt paths
                "closes":   last_6_closes,
                "ema20":    round(ema_20, 2) if ema_20 else None,
                "high48":   high_48h,
                "low48":    low_48h,
            }

            prices_dict[scan_pair] = {
                "price":      current_price,        # key _execute_enter expects
                "change_24h": change_24h_pct or 0.0,
                "funding_rate": funding_rate,
            }

        # ── IV — fetch BTC and ETH concurrently ───────────────────────────────
        async def _fetch_iv(iv_pair: str) -> Optional[float]:
            try:
                return await self._deribit.get_iv_for_pair(iv_pair)
            except Exception:
                return None

        async def _fetch_fear_greed() -> dict:
            try:
                import urllib.request as _ur
                loop = asyncio.get_event_loop()
                def _get():
                    with _ur.urlopen(
                        "https://api.alternative.me/fng/?limit=2", timeout=5
                    ) as resp:
                        import json as _json
                        return _json.load(resp)
                data = await loop.run_in_executor(None, _get)
                rows = data.get("data", [])
                if not rows:
                    return {"current_value": None, "current_label": "Unknown"}
                cur  = rows[0]
                prev = rows[1] if len(rows) > 1 else None
                cur_val  = int(cur["value"])
                cur_lab  = cur["value_classification"]
                prev_val = int(prev["value"]) if prev else None
                if prev_val is not None:
                    trend = (
                        "improving" if cur_val > prev_val
                        else "worsening" if cur_val < prev_val
                        else "stable"
                    )
                else:
                    trend = "stable"
                return {
                    "current_value":    cur_val,
                    "current_label":    cur_lab,
                    "yesterday_value":  prev_val,
                    "trend":            trend,
                }
            except Exception as exc:
                print(f"[scanner] _fetch_fear_greed error: {exc}", file=sys.stderr)
                return {"current_value": None, "current_label": "Unknown"}

        btc_iv, eth_iv, fear_greed = await asyncio.gather(
            _fetch_iv("BTC-USDT-SWAP"),
            _fetch_iv("ETH-USDT-SWAP"),
            _fetch_fear_greed(),
        )
        iv_dict: dict = {}
        if btc_iv is not None:
            iv_dict["BTC"] = round(btc_iv, 1)
        if eth_iv is not None:
            iv_dict["ETH"] = round(eth_iv, 1)

        # ── Supporting context ────────────────────────────────────────────────
        open_positions  = self._get_open_positions()
        headlines       = self._news.get_latest_headlines(count=15)
        upcoming_events = self._calendar.get_upcoming_events(days=3)
        strategy_memory = self._memory.read_strategy_memory()
        recent_trades   = self._memory.get_recent_trades(days=14)[:5]

        trigger_pair_price = prices_dict.get(pair, {}).get("price")
        trigger_pair_fr    = prices_dict.get(pair, {}).get("funding_rate")

        # ── Assemble briefing ─────────────────────────────────────────────────
        briefing = {
            "timestamp":      now.isoformat(),
            "pair":           pair,
            "trigger_type":   trigger_type,
            "trigger_detail": trigger_detail,
            "triggered_at":   now.isoformat(),
            # Single-pair snapshot (backward compat for _enrich_briefing)
            "price": {
                "mark_price":   trigger_pair_price,
                "funding_rate": trigger_pair_fr,
            },
            # All-pairs rich data — these are the primary fields used by _build_prompt
            "prices":    prices_dict,
            "technical": technical,
            "funding":   funding_dict,          # {pair: float} — format strategist uses
            "iv":        iv_dict,               # {"BTC": float, "ETH": float}
            "fear_greed": fear_greed,          # {current_value, current_label, trend}
            # Positions with all DB columns (thesis, stop_loss, take_profit, etc.)
            "open_positions": open_positions,
            # News and calendar
            "news_headlines": headlines,
            "upcoming_macro_events": [
                {
                    "date":        str(e["date"]),
                    "type":        e["type"],
                    "description": e["description"],
                    "days_away":   e["days_away"],
                }
                for e in upcoming_events[:5]
            ],
            # Memory
            "strategy_memory": {
                "what_works":       strategy_memory.get("what_works", ""),
                "what_doesnt_work": strategy_memory.get("what_doesnt_work", ""),
                "current_thesis":   strategy_memory.get("current_thesis", ""),
            },
            "recent_trades": recent_trades,
        }

        try:
            await self.strategist.make_decision(briefing)
        except Exception as exc:
            print(
                f"[scanner] strategist.make_decision error for {pair}: {exc}",
                file=sys.stderr,
            )

    # ── DB helper ─────────────────────────────────────────────────────────────

    def _get_open_positions(self) -> list[dict]:
        """
        Return all open trades (timestamp_close IS NULL) with every DB column.
        The full row includes: id, pair, direction, entry_price, size_usd,
        stop_loss, take_profit, thesis, trigger_event, timestamp_open, paper.
        """
        try:
            from db.logger import load_open_trades
            rows = load_open_trades(paper=config.PAPER_MODE)
            return [dict(row) for row in rows]
        except Exception as exc:
            print(f"[scanner] _get_open_positions error: {exc}", file=sys.stderr)
            return []
