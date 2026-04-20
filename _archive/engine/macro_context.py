"""
Macro Context Engine — multi-source weighted sentiment scoring system.

Replaces the single Claude headline-reading approach with 5 independent
data sources aggregated into a stable composite score:

  price_momentum:  25%  — EMA positions and volume confirmation per pair
  derivatives:     25%  — Funding rate contrarian signal
  fear_greed:      20%  — Alternative.me index (contrarian mapping)
  macro_regime:    20%  — BTC dominance proxy + upcoming event risk
  news_catalyst:   10%  — Rolling 24h log of scanner-detected events

Claude writes a 2-sentence plain-English summary only — it does not
determine the sentiment label.

Composite score → RISK_ON (> 0.25) / NEUTRAL / RISK_OFF (< -0.25)
"""

import asyncio
import json
import os
import sys
import urllib.request
import warnings
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

from sqlalchemy import create_engine, text as sa_text

import config

if TYPE_CHECKING:
    from feeds.news_feed import NewsFeed
    from feeds.macro_calendar import MacroCalendar
    from feeds.telegram_notifier import TelegramNotifier
    from feeds.okx_ws import OKXWebSocketManager

# ── Constants ─────────────────────────────────────────────────────────────────

_NEUTRAL_CONTEXT: dict = {
    "sentiment":              "NEUTRAL",
    "confidence":             0.5,
    "risk_multiplier":        1.0,
    "key_events":             [],
    "reasoning":              "Sentiment analysis unavailable.",
    "watch_for":              "Monitor feeds.",
    "suggested_cap_adjustment": None,
}

# Weighting per pair for cross-pair aggregation
_PAIR_WEIGHTS = {
    "BTC-USDT-SWAP": 0.50,
    "ETH-USDT-SWAP": 0.30,
    "SOL-USDT-SWAP": 0.20,
}

# Weighting per source for composite score
_SCORE_WEIGHTS = {
    "price_momentum": 0.25,
    "derivatives":    0.25,
    "fear_greed":     0.20,
    "macro_regime":   0.20,
    "news_catalyst":  0.10,
}

# Catalyst score lookup: (urgency, direction) → score
_CATALYST_SCORES: dict[tuple[str, str], float] = {
    ("critical", "bullish"):  +1.0,
    ("urgent",   "bullish"):  +0.6,
    ("notable",  "bullish"):  +0.3,
    ("critical", "neutral"):   0.0,
    ("urgent",   "neutral"):   0.0,
    ("notable",  "neutral"):   0.0,
    ("notable",  "bearish"):  -0.3,
    ("urgent",   "bearish"):  -0.6,
    ("critical", "bearish"):  -1.0,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _ema_series(prices: list[float], period: int) -> list[float]:
    """Full EMA series. Works with any length >= 1."""
    if not prices:
        return []
    k = 2.0 / (period + 1)
    out = [prices[0]]
    for p in prices[1:]:
        out.append(p * k + out[-1] * (1.0 - k))
    return out


def _weighted_composite(pair_scores: dict[str, float]) -> float:
    """Weighted average of per-pair scores using _PAIR_WEIGHTS."""
    total = 0.0
    weight_sum = 0.0
    for pair in config.TRADING_PAIRS:
        w = _PAIR_WEIGHTS.get(pair, 0.0)
        total += pair_scores.get(pair, 0.0) * w
        weight_sum += w
    return _clamp(total / weight_sum) if weight_sum else 0.0


# ── Main class ────────────────────────────────────────────────────────────────

class MacroContextEngine:
    """
    Multi-source weighted sentiment engine.

    generate_daily_brief() — called by APScheduler at 00:30 UTC.
    get_risk_multiplier()  — called by Strategist every decision cycle.
    log_news_event()       — called by Scanner whenever a notable headline fires.
    set_ws_manager()       — wired from main.py after ws_manager is ready.
    """

    def __init__(
        self,
        news_feed: "NewsFeed",
        macro_calendar: "MacroCalendar",
        notifier: "TelegramNotifier",
    ) -> None:
        self._news_feed      = news_feed
        self._macro_calendar = macro_calendar
        self._notifier       = notifier
        self._ws_manager     = None          # set via set_ws_manager()

        self._current_sentiment: Optional[dict] = None
        self._last_scored_at:    Optional[datetime] = None
        self._news_event_buffer: list[dict] = []   # rolling 24h window

        # Backwards-compat cache for get_todays_context()
        self._todays_context: Optional[dict] = None
        self._context_date:   Optional[object] = None   # datetime.date

        self._gemini = None
        self._init_gemini()

    def set_ws_manager(self, ws) -> None:
        """Wire the live ws_manager in after construction."""
        self._ws_manager = ws

    def _init_gemini(self) -> None:
        """Initialise Gemini Flash. Silent on missing key."""
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            return
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                import google.generativeai as genai
                genai.configure(api_key=key)
                self._gemini = genai.GenerativeModel(config.GEMINI_MODEL)
        except Exception as exc:
            print(f"[macro_context] Gemini init error: {exc}", file=sys.stderr)

    # ── Individual scorers ────────────────────────────────────────────────────

    def score_price_momentum(self, ws_manager) -> dict:
        """
        Score momentum from EMA positions and volume on 1H candles.
        Returns score -1.0 to +1.0.
        Gracefully returns neutral when ws_manager is None.
        """
        if ws_manager is None:
            return {
                "score": 0.0,
                "label": "NEUTRAL",
                "detail": {"note": "No ws_manager — graceful degradation"},
            }

        pair_scores: dict[str, float] = {}
        detail: dict[str, dict] = {}

        for pair in config.TRADING_PAIRS:
            try:
                candles = ws_manager.get_closed_candles(pair, "1H")
                if len(candles) < 25:
                    pair_scores[pair] = 0.0
                    detail[pair] = {"note": "insufficient data"}
                    continue

                candles = candles[-50:]
                closes  = [float(c["close"]) for c in candles]
                volumes = [float(c.get("volume", 0) or 0) for c in candles]

                ema20 = _ema_series(closes, 20)[-1]
                ema50 = _ema_series(closes, min(50, len(closes)))[-1]
                price = closes[-1]

                price_vs_ema20 = +0.3 if price > ema20 else -0.3
                price_vs_ema50 = +0.3 if price > ema50 else -0.3
                ema20_vs_ema50 = +0.2 if ema20 > ema50 else -0.2

                vol_signal = 0.0
                if len(volumes) >= 25:
                    recent_vol = sum(volumes[-5:]) / 5
                    base_vol   = sum(volumes[-25:-5]) / 20
                    if base_vol > 0 and recent_vol > base_vol * 1.3:
                        vol_signal = +0.2 if closes[-1] > closes[-6] else -0.2

                raw_score = price_vs_ema20 + price_vs_ema50 + ema20_vs_ema50 + vol_signal
                pair_scores[pair] = _clamp(raw_score)
                detail[pair] = {
                    "price":         round(price, 2),
                    "ema20":         round(ema20, 2),
                    "ema50":         round(ema50, 2),
                    "price_vs_ema20": price_vs_ema20,
                    "price_vs_ema50": price_vs_ema50,
                    "ema20_vs_ema50": ema20_vs_ema50,
                    "vol_signal":    vol_signal,
                    "pair_score":    round(_clamp(raw_score), 3),
                }
            except Exception as exc:
                pair_scores[pair] = 0.0
                detail[pair] = {"error": str(exc)}

        composite = _weighted_composite(pair_scores)
        label = "BULLISH" if composite > 0.3 else "BEARISH" if composite < -0.3 else "NEUTRAL"
        return {"score": composite, "label": label, "detail": detail}

    def score_derivatives(self, ws_manager) -> dict:
        """
        Score funding rates as a contrarian sentiment indicator.
        High positive funding = longs crowded = bearish contrarian signal.
        High negative funding = shorts crowded = bullish contrarian signal.
        """
        if ws_manager is None:
            return {
                "score": 0.0,
                "label": "NEUTRAL",
                "detail": {"note": "No ws_manager — graceful degradation"},
            }

        pair_scores: dict[str, float] = {}
        detail: dict[str, dict] = {}

        for pair in config.TRADING_PAIRS:
            try:
                ticker = ws_manager.get_latest_ticker(pair)
                funding_rate = 0.0
                if ticker:
                    funding_rate = float(
                        ticker.get("funding_rate") or
                        ticker.get("fundingRate") or
                        ticker.get("lastFundingRate") or 0
                    )

                # Contrarian scoring (per 8H period)
                if funding_rate > 0.001:       # > 0.1%/8H: extreme long crowding
                    fs = -1.0
                    signal = "extreme_long_crowding"
                elif funding_rate > 0.0005:    # > 0.05%/8H: longs crowded
                    fs = -0.5
                    signal = "long_crowded"
                elif funding_rate < -0.0005:   # < -0.05%/8H: extreme short crowding
                    fs = +1.0
                    signal = "extreme_short_crowding"
                elif funding_rate < -0.0002:   # < -0.02%/8H: shorts paying
                    fs = +0.5
                    signal = "short_crowded"
                else:
                    fs = 0.0
                    signal = "neutral"

                pair_scores[pair] = fs
                detail[pair] = {
                    "funding_rate":           funding_rate,
                    "funding_annualised_pct": round(funding_rate * 3 * 365 * 100, 2),
                    "signal":                 signal,
                    "score":                  fs,
                }
            except Exception as exc:
                pair_scores[pair] = 0.0
                detail[pair] = {"error": str(exc)}

        composite = _weighted_composite(pair_scores)
        label = "BULLISH" if composite > 0.3 else "BEARISH" if composite < -0.3 else "NEUTRAL"
        return {"score": composite, "label": label, "detail": detail}

    async def score_fear_greed(self) -> dict:
        """
        Fetch Fear & Greed index (last 3 days) and score it contrarian.
        Extreme fear = historically good buying opportunity (+0.8).
        Extreme greed = market overleveraged (-0.8).
        API error → score=0.0, label='Unknown'.
        """
        try:
            loop = asyncio.get_event_loop()

            def _fetch():
                with urllib.request.urlopen(
                    "https://api.alternative.me/fng/?limit=3", timeout=5
                ) as resp:
                    return json.loads(resp.read())

            data = await loop.run_in_executor(None, _fetch)
            rows = data.get("data", [])
            if not rows:
                return {"score": 0.0, "label": "Unknown", "value": None,
                        "trend": "stable", "detail": {}}

            cur_val   = int(rows[0]["value"])
            cur_lab   = rows[0]["value_classification"]
            prev_val  = int(rows[1]["value"]) if len(rows) > 1 else cur_val
            prev2_val = int(rows[2]["value"]) if len(rows) > 2 else prev_val

            # Contrarian mapping
            if cur_val <= 24:
                score = +0.8
            elif cur_val <= 44:
                score = +0.3
            elif cur_val <= 55:
                score = 0.0
            elif cur_val <= 74:
                score = -0.3
            else:
                score = -0.8

            # Trend adjustment (±0.1)
            if cur_val > prev_val > prev2_val:
                score += 0.1
                trend = "improving"
            elif cur_val < prev_val < prev2_val:
                score -= 0.1
                trend = "worsening"
            else:
                trend = "stable"

            return {
                "score": _clamp(score),
                "label": cur_lab,
                "value": cur_val,
                "trend": trend,
                "detail": {
                    "current":        cur_val,
                    "yesterday":      prev_val,
                    "two_days_ago":   prev2_val,
                    "classification": cur_lab,
                    "trend":          trend,
                },
            }
        except Exception as exc:
            print(f"[macro_context] score_fear_greed error: {exc}", file=sys.stderr)
            return {
                "score": 0.0, "label": "Unknown", "value": None,
                "trend": "stable", "detail": {"error": str(exc)},
            }

    def score_macro_regime(self, ws_manager) -> dict:
        """
        Score the broad macro environment using objective, slow-moving data.

        1. BTC vs ETH dominance proxy (relative performance over available history)
           BTC outperforming alts → capital-safety flow → risk-off signal
           ETH/alts outperforming → speculation mode → risk-on signal

        2. Upcoming high-impact event risk (FOMC, CPI, PPI)
           Major events within 2 days → uncertainty → negative signal
           No events → low uncertainty → small positive signal
        """
        signals: dict[str, float] = {}
        detail:  dict[str, object] = {}

        # ── 1. BTC dominance proxy ────────────────────────────────────────────
        btc_dom_signal = 0.0
        if ws_manager is not None:
            try:
                btc_c = ws_manager.get_closed_candles("BTC-USDT-SWAP", "1H")
                eth_c = ws_manager.get_closed_candles("ETH-USDT-SWAP", "1H")
                if len(btc_c) >= 10 and len(eth_c) >= 10:
                    n = min(len(btc_c), len(eth_c), 72)  # up to 3-day window
                    btc_chg = float(btc_c[-1]["close"]) / float(btc_c[-n]["close"]) - 1
                    eth_chg = float(eth_c[-1]["close"]) / float(eth_c[-n]["close"]) - 1
                    outperf = btc_chg - eth_chg
                    if outperf > 0.05:
                        btc_dom_signal = -0.3    # BTC >> ETH: risk-off
                    elif outperf > 0.02:
                        btc_dom_signal = -0.15
                    elif outperf < -0.05:
                        btc_dom_signal = +0.3    # ETH >> BTC: risk-on
                    elif outperf < -0.02:
                        btc_dom_signal = +0.15
                    detail["btc_dominance"] = {
                        "btc_change_pct":  round(btc_chg * 100, 2),
                        "eth_change_pct":  round(eth_chg * 100, 2),
                        "outperformance":  round(outperf * 100, 2),
                        "signal":          btc_dom_signal,
                        "periods":         n,
                    }
            except Exception as exc:
                detail["btc_dominance"] = {"error": str(exc)}
        signals["btc_dominance"] = btc_dom_signal

        # ── 2. Upcoming event risk ────────────────────────────────────────────
        event_risk = 0.0
        upcoming: list[dict] = []
        if self._macro_calendar is not None:
            try:
                upcoming = self._macro_calendar.get_upcoming_events(days=2)
                for ev in upcoming:
                    ev_type   = str(ev.get("type", "")).upper()
                    days_away = int(ev.get("days_away", 7))
                    if "FOMC" in ev_type and days_away <= 2:
                        event_risk -= 0.3
                    elif "CPI" in ev_type and days_away <= 1:
                        event_risk -= 0.2
                    elif "PPI" in ev_type and days_away <= 1:
                        event_risk -= 0.15
                if not upcoming:
                    event_risk = +0.1   # low uncertainty bonus
            except Exception as exc:
                detail["event_risk_err"] = str(exc)
        signals["event_risk"] = _clamp(event_risk)
        detail["event_risk"] = {
            "score":  event_risk,
            "events": [e.get("description", "") for e in upcoming[:5]],
        }

        composite = _clamp(
            signals["btc_dominance"] * 0.60 + signals["event_risk"] * 0.40
        )
        label = "RISK_ON" if composite > 0.2 else "RISK_OFF" if composite < -0.2 else "NEUTRAL"
        return {"score": composite, "label": label, "detail": detail}

    def score_news_catalyst(self) -> dict:
        """
        Score active news catalysts from the rolling 24h event buffer.
        Events in the last 2 hours are weighted 3x more than older events.
        Returns score=0.0, label='NO_CATALYST' when buffer is empty.
        """
        now = datetime.now(timezone.utc)

        # Prune expired
        self._news_event_buffer = [
            e for e in self._news_event_buffer if e["expires_at"] > now
        ]

        if not self._news_event_buffer:
            return {
                "score": 0.0,
                "label": "NO_CATALYST",
                "active_catalysts": [],
                "detail": {"event_count": 0},
            }

        cutoff_2h = now - timedelta(hours=2)
        weighted_sum = 0.0
        total_weight = 0.0
        active_catalysts: list[str] = []

        for ev in self._news_event_buffer:
            weight = 3.0 if ev["logged_at"] >= cutoff_2h else 1.0
            weighted_sum += ev["catalyst_score"] * weight
            total_weight += weight
            if abs(ev["catalyst_score"]) >= 0.3:
                active_catalysts.append(ev["headline"][:100])

        score = _clamp(weighted_sum / total_weight if total_weight > 0 else 0.0)

        if score >= 0.3:
            label = "CATALYST_BULLISH"
        elif score <= -0.3:
            label = "CATALYST_BEARISH"
        else:
            label = "NO_CATALYST"

        return {
            "score": score,
            "label": label,
            "active_catalysts": active_catalysts[:5],
            "detail": {
                "event_count":   len(self._news_event_buffer),
                "recent_events": len([
                    e for e in self._news_event_buffer
                    if e["logged_at"] >= cutoff_2h
                ]),
            },
        }

    # ── News event logging ────────────────────────────────────────────────────

    async def log_news_event(
        self, headline: str, urgency: str, direction: str
    ) -> None:
        """
        Called by Scanner when a notable/urgent/critical headline fires.
        Scores the event, adds it to the rolling 24h buffer, persists to DB.
        Critical events trigger an immediate full rescore.
        """
        now = datetime.now(timezone.utc)
        catalyst_score = _CATALYST_SCORES.get(
            (urgency.lower(), direction.lower()), 0.0
        )

        event: dict = {
            "headline":       headline[:500],
            "urgency":        urgency,
            "direction":      direction,
            "catalyst_score": catalyst_score,
            "logged_at":      now,
            "expires_at":     now + timedelta(hours=24),
            "source":         "scanner",
        }
        self._news_event_buffer.append(event)

        # Prune expired while we're here
        self._news_event_buffer = [
            e for e in self._news_event_buffer if e["expires_at"] > now
        ]

        await self._write_news_event_to_db(event)

        if urgency.lower() == "critical":
            print(
                f"[macro_context] Critical event — triggering rescore: "
                f"{headline[:80]}",
                flush=True,
            )
            try:
                await self.generate_sentiment_score(self._ws_manager)
            except Exception as exc:
                print(f"[macro_context] Rescore error: {exc}", file=sys.stderr)

    async def _write_news_event_to_db(self, event: dict) -> None:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return
        try:
            engine = create_engine(database_url)
            with engine.begin() as conn:
                conn.execute(sa_text("""
                    INSERT INTO news_event_log
                      (headline, urgency, direction, catalyst_score, expires_at, source)
                    VALUES
                      (:headline, :urgency, :direction, :catalyst_score,
                       :expires_at, :source)
                """), {
                    "headline":       event["headline"],
                    "urgency":        event["urgency"],
                    "direction":      event["direction"],
                    "catalyst_score": event["catalyst_score"],
                    "expires_at":     event["expires_at"],
                    "source":         event["source"],
                })
        except Exception as exc:
            print(f"[macro_context] _write_news_event_to_db error: {exc}",
                  file=sys.stderr)

    # ── Composite scoring ─────────────────────────────────────────────────────

    async def generate_sentiment_score(self, ws_manager) -> dict:
        """
        Run all 5 scorers and combine into a weighted composite.
        Claude writes a 2-sentence summary. Result written to DB and cached.
        """
        # Sync scorers (fast in-memory reads, no I/O)
        momentum    = self.score_price_momentum(ws_manager)
        derivatives = self.score_derivatives(ws_manager)
        macro       = self.score_macro_regime(ws_manager)
        catalyst    = self.score_news_catalyst()
        # Network call — run last
        fear_greed  = await self.score_fear_greed()

        raw = (
            momentum["score"]    * _SCORE_WEIGHTS["price_momentum"] +
            derivatives["score"] * _SCORE_WEIGHTS["derivatives"]    +
            fear_greed["score"]  * _SCORE_WEIGHTS["fear_greed"]     +
            macro["score"]       * _SCORE_WEIGHTS["macro_regime"]   +
            catalyst["score"]    * _SCORE_WEIGHTS["news_catalyst"]
        )

        if raw > 0.25:
            composite_label = "RISK_ON"
        elif raw < -0.25:
            composite_label = "RISK_OFF"
        else:
            composite_label = "NEUTRAL"

        # Confidence: fraction of sources that agree with composite direction
        scores = [momentum["score"], derivatives["score"], fear_greed["score"],
                  macro["score"], catalyst["score"]]
        all_positive = sum(1 for s in scores if s > 0)
        all_negative = sum(1 for s in scores if s < 0)
        agreement    = max(all_positive, all_negative) / 5
        confidence   = 0.5 + (agreement * 0.5)

        score_breakdown = {
            "price_momentum": momentum,
            "derivatives":    derivatives,
            "fear_greed":     fear_greed,
            "macro_regime":   macro,
            "news_catalyst":  catalyst,
        }

        claude_summary = await self._get_claude_summary(
            momentum=momentum, derivatives=derivatives, fear_greed=fear_greed,
            macro=macro, catalyst=catalyst,
            composite_label=composite_label, confidence=confidence,
        )

        result = {
            "composite_score": round(raw, 4),
            "composite_label": composite_label,
            "confidence":      round(confidence, 3),
            "score_breakdown": score_breakdown,
            "claude_summary":  claude_summary,
            "scored_at":       datetime.now(timezone.utc).isoformat(),
        }

        self._current_sentiment = result
        self._last_scored_at    = datetime.now(timezone.utc)

        await self._write_sentiment_score_to_db(result)
        return result

    async def _get_claude_summary(
        self, *, momentum, derivatives, fear_greed, macro, catalyst,
        composite_label, confidence,
    ) -> str:
        """Ask Claude for a 2-sentence factual summary. Falls back to a template."""
        try:
            import anthropic

            fg_val = fear_greed.get("value")
            fg_str = (
                f"{fg_val} ({fear_greed.get('label')}, {fear_greed.get('trend')})"
                if fg_val is not None else "unavailable"
            )

            prompt = (
                "Write a 2-sentence market summary for a crypto trader.\n"
                "Data:\n"
                f"- Price momentum: {momentum['label']} ({momentum['score']:+.2f})\n"
                f"- Derivatives/funding: {derivatives['label']} "
                f"({derivatives['score']:+.2f})\n"
                f"- Fear & Greed: {fg_str}\n"
                f"- Macro regime: {macro['label']} ({macro['score']:+.2f})\n"
                f"- Active news catalysts: {catalyst['label']}\n"
                f"- Composite: {composite_label} at "
                f"{confidence * 100:.0f}% confidence\n\n"
                "Keep it factual and specific. No fluff."
            )

            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            print(f"[macro_context] Claude summary error: {exc}", file=sys.stderr)
            fg_val = fear_greed.get("value")
            fg_part = f" Fear & Greed at {fg_val}." if fg_val is not None else ""
            return (
                f"Market composite is {composite_label} "
                f"({confidence * 100:.0f}% confidence).{fg_part} "
                f"Price momentum {momentum['label'].lower()}, "
                f"derivatives {derivatives['label'].lower()}."
            )

    async def _write_sentiment_score_to_db(self, result: dict) -> None:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return
        try:
            sb = result["score_breakdown"]
            engine = create_engine(database_url)
            with engine.begin() as conn:
                conn.execute(sa_text("""
                    INSERT INTO sentiment_scores (
                        price_momentum_score, derivatives_score, fear_greed_score,
                        macro_regime_score, news_catalyst_score,
                        composite_score, composite_label, confidence,
                        price_momentum_detail, derivatives_detail, fear_greed_detail,
                        macro_regime_detail, news_catalyst_detail, claude_summary
                    ) VALUES (
                        :pm_score, :d_score, :fg_score, :mr_score, :nc_score,
                        :composite, :label, :confidence,
                        :pm_detail, :d_detail, :fg_detail, :mr_detail, :nc_detail,
                        :summary
                    )
                """), {
                    "pm_score":   sb["price_momentum"]["score"],
                    "d_score":    sb["derivatives"]["score"],
                    "fg_score":   sb["fear_greed"]["score"],
                    "mr_score":   sb["macro_regime"]["score"],
                    "nc_score":   sb["news_catalyst"]["score"],
                    "composite":  result["composite_score"],
                    "label":      result["composite_label"],
                    "confidence": result["confidence"],
                    "pm_detail":  json.dumps(sb["price_momentum"].get("detail", {})),
                    "d_detail":   json.dumps(sb["derivatives"].get("detail", {})),
                    "fg_detail":  json.dumps(sb["fear_greed"].get("detail", {})),
                    "mr_detail":  json.dumps(sb["macro_regime"].get("detail", {})),
                    "nc_detail":  json.dumps(sb["news_catalyst"].get("detail", {})),
                    "summary":    result["claude_summary"],
                })
        except Exception as exc:
            print(f"[macro_context] _write_sentiment_score_to_db error: {exc}",
                  file=sys.stderr)

    # ── Daily brief ───────────────────────────────────────────────────────────

    async def generate_daily_brief(self) -> dict:
        """
        Full pipeline: score → build brief dict → write to DB → send Telegram.
        Called by APScheduler at 00:30 UTC and 45s after startup.
        Never raises — all errors fall back to neutral context.
        """
        try:
            print("[macro_context] Generating daily sentiment brief…", flush=True)

            result = await self.generate_sentiment_score(self._ws_manager)
            raw    = result["composite_score"]

            # Risk multiplier from composite score
            if raw > 0.25:
                risk_multiplier = 1.0
            elif raw > -0.25:
                risk_multiplier = 0.85
            elif raw > -0.5:
                risk_multiplier = 0.65
            else:
                risk_multiplier = 0.4

            watch_for = self._derive_watch_for(result)

            key_events: list[str] = []
            if self._macro_calendar is not None:
                try:
                    evs = self._macro_calendar.get_upcoming_events(days=7)
                    key_events = [
                        f"{e.get('type', '')} — {e.get('description', '')} "
                        f"(in {e.get('days_away', '?')} day(s))"
                        for e in evs[:5]
                    ]
                except Exception:
                    pass

            # Map composite label to legacy sentiment for callers expecting
            # BULLISH/NEUTRAL/BEARISH (e.g., dashboard)
            label_to_sentiment = {
                "RISK_ON":  "BULLISH",
                "NEUTRAL":  "NEUTRAL",
                "RISK_OFF": "BEARISH",
            }
            sentiment = label_to_sentiment.get(result["composite_label"], "NEUTRAL")

            context: dict = {
                "sentiment":              sentiment,
                "confidence":             result["confidence"],
                "risk_multiplier":        risk_multiplier,
                "key_events":             key_events,
                "reasoning":              result["claude_summary"],
                "watch_for":              watch_for,
                "suggested_cap_adjustment": None,
                "score_breakdown":        result["score_breakdown"],
                "composite_score":        result["composite_score"],
                "composite_label":        result["composite_label"],
                "generated_at":           datetime.now(timezone.utc).isoformat(),
            }

            self._todays_context = context
            self._context_date   = datetime.now(timezone.utc).date()

            await self._write_to_db(context)
            await self._send_telegram_brief(context, result)

            print(
                f"[macro_context] Brief complete — {result['composite_label']}, "
                f"confidence={result['confidence']:.2f}, "
                f"risk_multiplier={risk_multiplier}",
                flush=True,
            )
            return context

        except Exception as exc:
            print(f"[macro_context] generate_daily_brief error: {exc}",
                  file=sys.stderr)
            return dict(_NEUTRAL_CONTEXT)

    def _derive_watch_for(self, result: dict) -> str:
        """Derive a watch_for string from the dominant signal."""
        sb       = result.get("score_breakdown", {})
        catalyst = sb.get("news_catalyst", {})
        momentum = sb.get("price_momentum", {})
        deriv    = sb.get("derivatives", {})
        fg       = sb.get("fear_greed", {})

        if catalyst.get("label") not in ("NO_CATALYST", None):
            active = catalyst.get("active_catalysts", [])
            if active:
                return f"Active catalyst: {active[0][:120]}"
            return "Active news catalyst — monitor for follow-through"

        if momentum.get("score", 0) < -0.3 and deriv.get("score", 0) < -0.3:
            return ("Both momentum and derivatives bearish — "
                    "confirm breakdown below support")

        fg_val = fg.get("value")
        if fg_val is not None:
            if fg_val <= 24:
                return "Extreme fear reading — watch for reversal / capitulation bottom"
            if fg_val >= 75:
                return ("Extreme greed reading — market overleveraged, "
                        "watch for flush")

        # Highest absolute-score signal
        candidates = [
            ("price momentum", abs(sb.get("price_momentum", {}).get("score", 0)),
             sb.get("price_momentum", {}).get("label", "")),
            ("derivatives",    abs(sb.get("derivatives",    {}).get("score", 0)),
             sb.get("derivatives",    {}).get("label", "")),
            ("Fear & Greed",   abs(sb.get("fear_greed",     {}).get("score", 0)),
             sb.get("fear_greed",     {}).get("label", "")),
            ("macro regime",   abs(sb.get("macro_regime",   {}).get("score", 0)),
             sb.get("macro_regime",   {}).get("label", "")),
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)
        name, _, label = candidates[0]
        return f"Dominant signal: {name} is {label}"

    # ── DB persistence ────────────────────────────────────────────────────────

    async def _write_to_db(self, context: dict) -> None:
        """Persist the daily context to macro_context_log (existing table)."""
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return
        try:
            from db.models import macro_context_log
            engine = create_engine(database_url)
            key_events_str = json.dumps(context.get("key_events", []))
            with engine.begin() as conn:
                conn.execute(
                    macro_context_log.insert().values(
                        date=datetime.now(timezone.utc).date(),
                        sentiment=context.get("sentiment"),
                        confidence=context.get("confidence"),
                        risk_multiplier=context.get("risk_multiplier"),
                        key_events=key_events_str,
                        reasoning=context.get("reasoning"),
                        watch_for=context.get("watch_for"),
                        suggested_cap_adjustment=None,
                    )
                )
        except Exception as exc:
            print(f"[macro_context] _write_to_db error: {exc}", file=sys.stderr)

    # ── Telegram brief ────────────────────────────────────────────────────────

    async def _send_telegram_brief(self, context: dict, result: dict) -> None:
        """Send a structured sentiment brief with score breakdown to Telegram."""
        if self._notifier is None or not self._notifier.enabled:
            return

        utc_now = datetime.now(timezone.utc)
        myt_now = utc_now + timedelta(hours=8)
        date_str = myt_now.strftime("%Y-%m-%d")
        time_str = myt_now.strftime("%H:%M MYT")

        label  = result["composite_label"]
        emoji  = {"RISK_ON": "🟢", "RISK_OFF": "🔴", "NEUTRAL": "⚪"}.get(label, "⚪")
        conf   = result["confidence"] * 100
        rm     = context["risk_multiplier"]

        sb       = result.get("score_breakdown", {})
        momentum = sb.get("price_momentum", {})
        deriv    = sb.get("derivatives",    {})
        fg       = sb.get("fear_greed",     {})
        macro    = sb.get("macro_regime",   {})
        catalyst = sb.get("news_catalyst",  {})

        fg_val = fg.get("value")
        fg_line = (
            f"{fg_val} — {fg.get('label')} ({fg.get('trend')})"
            if fg_val is not None else "unavailable"
        )

        msg = (
            f"🌍 <b>MARKET SENTIMENT BRIEF</b>\n"
            f"{date_str} | {time_str}\n"
            f"\n"
            f"<b>Composite: {emoji} {label}</b> ({conf:.0f}% confidence)\n"
            f"{context['reasoning']}\n"
            f"\n"
            f"<b>📊 Score breakdown:</b>\n"
            f"Price momentum:  {momentum.get('label', '—'):<10}  "
            f"({momentum.get('score', 0):+.2f})\n"
            f"Derivatives:     {deriv.get('label', '—'):<10}  "
            f"({deriv.get('score', 0):+.2f})\n"
            f"Fear &amp; Greed:    {fg_line}\n"
            f"Macro regime:    {macro.get('label', '—'):<10}  "
            f"({macro.get('score', 0):+.2f})\n"
            f"News catalyst:   {catalyst.get('label', '—')}\n"
            f"\n"
            f"<b>⚙️ Risk adjustment:</b> {rm * 100:.0f}% of normal size"
        )
        await self._notifier.send(msg)

    # ── Public accessors ──────────────────────────────────────────────────────

    def get_todays_context(self) -> dict:
        """
        Return today's cached context dict.
        Falls back to in-memory sentiment if a rescore happened today,
        then to neutral defaults.
        """
        today = datetime.now(timezone.utc).date()
        if self._context_date == today and self._todays_context is not None:
            return self._todays_context

        if self._current_sentiment is not None:
            raw = self._current_sentiment.get("composite_score", 0.0)
            return {
                "sentiment":        self._current_sentiment.get(
                                        "composite_label", "NEUTRAL"),
                "confidence":       self._current_sentiment.get("confidence", 0.5),
                "risk_multiplier":  self._context_risk_multiplier(raw),
                "key_events":       [],
                "reasoning":        self._current_sentiment.get(
                                        "claude_summary", ""),
                "watch_for":        "See latest sentiment brief.",
                "suggested_cap_adjustment": None,
            }
        return dict(_NEUTRAL_CONTEXT)

    def _context_risk_multiplier(self, raw: float = 0.0) -> float:
        if raw > 0.25:
            return 1.0
        elif raw > -0.25:
            return 0.85
        elif raw > -0.5:
            return 0.65
        return 0.4

    def get_risk_multiplier(self) -> float:
        """Return today's macro risk multiplier. Defaults to 1.0."""
        return float(self.get_todays_context().get("risk_multiplier", 1.0))

    def apply_context_to_risk(self, base_size_usd: float) -> float:
        """Apply today's macro risk multiplier to a position size."""
        return base_size_usd * self.get_risk_multiplier()
