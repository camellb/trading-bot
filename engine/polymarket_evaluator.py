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
        f"You are a forecaster. Your job is to estimate the true probability "
        f"that the YES outcome of this binary prediction market will resolve "
        f"true, based on the evidence available to you. "
        f"Today is {today}. "
        f"CRITICAL: Your training data is OUTDATED. Do NOT use your memory for "
        f"current prices, scores, standings, poll numbers, or recent events — "
        f"they WILL be wrong. Use ONLY the research context provided below. "
        f"If the research does not contain a key fact you need, say so in your "
        f"reasoning and set confidence LOW. "
        f"HOW TO FORECAST: Read the resolution criteria carefully. Read the "
        f"research. Think about what has to be true for YES to resolve, and "
        f"what has to be true for NO. Weigh the evidence. Produce an honest "
        f"probability estimate. "
        f"If the evidence strongly points to YES, output a high probability "
        f"(e.g. 0.80, 0.90). If it strongly points to NO, output a low "
        f"probability (e.g. 0.15, 0.05). If the evidence is mixed or "
        f"inconclusive, your probability should reflect that — and your "
        f"confidence should drop accordingly. "
        f"The market price is shown to you as context so you know what the bot "
        f"is trading against. It is NOT a prior. It does NOT constrain your "
        f"estimate. You may ignore it entirely when forecasting. If your "
        f"estimate happens to match the market, fine. If it differs "
        f"substantially, also fine — report what the evidence says. "
        f"Do not hedge toward the market price to avoid looking wrong. Do not "
        f"invent contrarian estimates to look smart. Forecast what you "
        f"actually believe the probability is. "
        f"CONFIDENCE: Confidence is how sure you are of your own estimate, "
        f"based on the quality and specificity of the evidence you have. It "
        f"is NOT the distance between your estimate and the market price. "
        f"Rich, specific, current evidence that directly addresses the "
        f"resolution criteria → confidence 0.7–0.9. Partial evidence with "
        f"some directly relevant facts and gaps in others → confidence "
        f"0.4–0.6. Thin evidence, ambiguous resolution criteria, or key "
        f"facts missing → confidence 0.1–0.3. "
        f"If resolution criteria are ambiguous or could be interpreted "
        f"multiple ways, say so in reasoning and cap confidence at 0.4. "
        f"A confident forecaster who is wrong half the time loses money. Be "
        f"honest about uncertainty — low confidence translates into a smaller "
        f"stake downstream. That is the correct way to handle thin evidence, "
        f"not by copying the market. "
        f"LIVE MARKET DATA: If the research context contains a section "
        f"labelled 'LIVE MARKET DATA (REAL-TIME)', that block is evidence for "
        f"short-horizon price-direction markets — current spot, recent "
        f"changes (15m/1h/24h), order-book imbalance, spread, funding rate, "
        f"recent candles — fetched seconds ago from OKX (crypto) or Yahoo "
        f"Finance (equities). Use it to ground your probability in actual "
        f"price dynamics. "
        f"If the LIVE MARKET DATA block is missing, stale (>60s for crypto / "
        f">120s for equity), or shows a zero/bad price, TREAT THIS AS MISSING "
        f"EVIDENCE and LOWER CONFIDENCE. Do not fabricate price levels from "
        f"memory. "
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
            f"\n\nFor context — current market price: YES = {market.yes_price:.2f}, "
            f"NO = {no_price:.2f}. "
            f"This is what the crowd thinks. It is shown so you know what the "
            f"bot is trading against. It is context only — do NOT anchor your "
            f"probability estimate to it.\n"
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
            f"Forecast the probability that the YES outcome will resolve true, "
            f"based on the evidence above."
        )
