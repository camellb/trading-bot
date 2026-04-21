"""
Polymarket evaluator — wraps Claude to produce a calibrated probability for
a single binary prediction market.

Design goals:
  * Claude sees the question + description + resolution horizon + market price.
  * Market price is shown as a prior that Claude should correct — the prompt
    asks Claude to evaluate whether the crowd price is too high, too low, or fair.
  * Output is strict JSON:
      {
        "probability_yes": 0..1,
        "confidence":      0..1,
        "category":        short tag,      # e.g. 'macro', 'geopolitics'
        "key_factors":     list of <6 strings,
        "reasoning":       <120 words,
      }
  * `probability_yes` is the calibrated probability Claude believes the
    YES outcome will resolve true.  This is the number we score against.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import anthropic

import config
from feeds.polymarket_feed import PolyMarket


def _system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"You are a calibrated forecaster evaluating a binary prediction market. "
        f"Today is {today}. "
        f"CRITICAL: Your training data is OUTDATED. Do NOT use your memory for "
        f"current prices, scores, standings, poll numbers, or recent events — "
        f"they WILL be wrong. Use ONLY the research context provided below. "
        f"If the research does not contain a key fact you need, say so in your "
        f"reasoning and set confidence LOW. "
        f"You are shown the current market price as a baseline — this is the "
        f"crowd's consensus probability. Your job is to evaluate whether the "
        f"market price is too high, too low, or approximately fair. "
        f"Start from the market price as a prior, then adjust up or down based "
        f"on the research context and your analysis. If you deviate significantly "
        f"from the market price (more than 10 percentage points), you MUST justify "
        f"why in your reasoning — what does the crowd have wrong? "
        f"IMPORTANT: When you lack research to form an independent view, "
        f"DEFAULT to the market price — do NOT default to 0.50. "
        f"A market at 0.80 with no contradicting research means probability_yes ≈ 0.80, "
        f"not 0.50. Only deviate when you have specific evidence. "
        f"Be MERCILESS about your uncertainty: a confident forecaster who is wrong "
        f"half the time loses money. It is better to report confidence 0.3 than to "
        f"pretend to 0.7. "
        f"If the resolution criteria are ambiguous or could be interpreted multiple "
        f"ways, say so in reasoning and cap confidence at 0.5. "
        f"LIVE MARKET DATA HANDLING: If the research context contains a section "
        f"labelled 'LIVE MARKET DATA (REAL-TIME)', that block is the primary input "
        f"for any short-horizon price-direction market. It reports the current spot "
        f"price, recent changes (15m/1h/24h), order-book imbalance, spread, funding "
        f"rate, and the last few candles — all fetched seconds ago from OKX (crypto) "
        f"or Yahoo Finance (equities). Use it to ground your probability in actual "
        f"price dynamics, not scraped news. Even with live data, continue to defer "
        f"to the market price as a strong prior — the crowd already incorporates "
        f"this information. Deviations beyond 10 percentage points from the market "
        f"price require a specific, articulable reason grounded in the live data or "
        f"research (e.g. 'spot is 4% above the strike with 40 minutes left, funding "
        f"turned negative, book is 3:1 bid-heavy'). If the LIVE MARKET DATA block is "
        f"missing, stale (fetched >60s ago for crypto / >120s for equity), or shows "
        f"a zero/obviously bad price, TREAT THIS AS A REASON TO LOWER CONFIDENCE and "
        f"stay close to the market price. Do NOT fabricate price levels from memory. "
        f"Output STRICT JSON only — no markdown fence, no prose before/after. "
        f"Schema: {{\"probability_yes\":0..1, \"confidence\":0..1, "
        f"\"category\":\"macro|geopolitics|politics|crypto|tech|sports|entertainment|science|other\", "
        f"\"key_factors\":[\"short factor\",...], "
        f"\"reasoning\":\"<=200 words prose\"}}"
    )


@dataclass
class MarketEvaluation:
    market_id:        str
    probability_yes:  float
    confidence:       float
    category:         str
    key_factors:      list[str]
    reasoning:        str
    raw:              str   # the raw model output for auditing


def _parse_json(raw: str) -> Optional[dict]:
    t = raw.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _clamp01(x, default=0.5):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


class PolymarketEvaluator:
    """
    Thin Claude wrapper — the bot already imports anthropic elsewhere,
    so we reuse the env-configured client (ANTHROPIC_API_KEY).
    """

    def __init__(self, client: Optional[anthropic.Anthropic] = None):
        self._client = client
        if self._client is None:
            try:
                self._client = anthropic.Anthropic()
            except Exception as exc:
                print(f"[polymarket_eval] client init failed: {exc}",
                      file=sys.stderr)
                self._client = None

    async def evaluate(
        self,
        market: PolyMarket,
        research_block: Optional[str] = None,
    ) -> Optional[MarketEvaluation]:
        if self._client is None:
            return None

        user = self._build_prompt(market, research_block)
        loop = asyncio.get_running_loop()
        raw = None
        for attempt in range(3):
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: self._client.messages.create(
                        model      = config.CLAUDE_MODEL,
                        max_tokens = 1000,
                        system     = _system_prompt(),
                        messages   = [{"role": "user", "content": user}],
                    ),
                )
                raw = response.content[0].text
                break
            except Exception as exc:
                if attempt == 2:
                    print(f"[polymarket_eval] Claude failed after 3 attempts "
                          f"on {market.id}: {exc}", file=sys.stderr)
                    return None
                await asyncio.sleep(2 ** attempt)
        if raw is None:
            return None

        obj = _parse_json(raw)
        if obj is None:
            print(f"[polymarket_eval] unparseable JSON for {market.id}: {raw[:200]}",
                  file=sys.stderr)
            return None

        return MarketEvaluation(
            market_id       = market.id,
            probability_yes = _clamp01(obj.get("probability_yes"), 0.5),
            confidence      = _clamp01(obj.get("confidence"),      0.5),
            category        = str(obj.get("category") or "other")[:40],
            key_factors     = [str(x)[:200] for x in (obj.get("key_factors") or [])][:6],
            reasoning       = str(obj.get("reasoning") or "")[:4000],
            raw             = raw,
        )

    @staticmethod
    def _build_prompt(market: PolyMarket,
                      research_block: Optional[str] = None) -> str:
        desc = market.description.strip()
        if len(desc) > 2500:
            desc = desc[:2500] + "…"
        research_section = ""
        if research_block:
            block = research_block.strip()
            if len(block) > 6000:
                block = block[:6000] + "…"
            research_section = f"\n\nResearch context:\n{block}\n"
        context_lines = [
            f"Days until resolution: {market.days_to_end:.0f}",
            f"24h trading volume: ${market.volume_24h_clob:,.0f}",
        ]
        if getattr(market, "neg_risk", False) and getattr(market, "group_item_title", None):
            context_lines.append(
                f"NOTE: This is ONE option in a multi-outcome group. "
                f"YES means specifically \"{market.group_item_title}\" — "
                f"not any other option. Estimate accordingly."
            )
        context_section = "\n".join(context_lines)

        no_price = 1.0 - market.yes_price
        market_price_section = (
            f"\n\nCurrent market price: YES = {market.yes_price:.2f}, "
            f"NO = {no_price:.2f}\n"
            f"This is the crowd's current estimate. "
            f"Your job is to determine if this price is wrong.\n"
        )

        return (
            f"Question: {market.question}\n\n"
            f"Resolution description:\n{desc or '(not provided)'}\n\n"
            f"Market will resolve on or before: {market.end_date_iso.isoformat()}\n"
            f"YES outcome label: {market.outcome_yes}\n"
            f"NO outcome label:  {market.outcome_no}\n\n"
            f"Market context:\n{context_section}"
            f"{market_price_section}"
            f"{research_section}\n"
            f"Evaluate whether the market price is correct. "
            f"Estimate the probability the YES outcome resolves true."
        )
