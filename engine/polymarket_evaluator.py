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
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import anthropic

import config
from feeds.polymarket_feed import PolyMarket


VALID_ARCHETYPES = {
    "price_threshold", "binary_event", "sports_match", "sports_prop",
    "geopolitical", "macro_release", "crypto", "entertainment",
    "scientific", "legal", "weather", "other",
}
VALID_RESOLUTION_STYLES = {"precise", "broad", "ambiguous", "multi_factor"}


def _system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"You are a calibrated forecaster evaluating a binary prediction market. "
        f"Today is {today}. "
        f"CRITICAL: Your training data is OUTDATED. Do NOT use your memory for "
        f"current prices, scores, standings, poll numbers, or recent events — "
        f"they WILL be wrong. Use ONLY the research context provided below. "
        f"You MAY use general knowledge about leagues, teams, players, historical "
        f"patterns, and base rates — just not current-season specifics. "
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
        f"If strategy memory from prior reviews is provided, treat it as a weak prior only. "
        f"Use it to notice recurring patterns or failure modes, but do not let it override "
        f"the current market's research, resolution rules, or price. "
        f"CONFIDENCE GUIDE — confidence reflects how likely your probability_yes is "
        f"within 5 percentage points of the true probability: "
        f"0.7-0.9 = strong research supports your estimate, clear evidence; "
        f"0.4-0.6 = some research available OR you're deferring to a well-traded "
        f"market price with no contradicting info (this IS a valid basis for confidence); "
        f"0.2-0.3 = very limited information AND unclear resolution criteria; "
        f"<0.2 = you cannot meaningfully estimate this market at all. "
        f"KEY: Agreeing with the market IS informative. If a liquid market is priced "
        f"at 0.75 and you see nothing to contradict it, confidence should be 0.4-0.5 "
        f"(you're fairly confident the market is right), NOT 0.15. "
        f"Reserve confidence below 0.25 for markets you truly cannot evaluate. "
        f"If the resolution criteria are ambiguous or could be interpreted multiple "
        f"ways, say so in reasoning and cap confidence at 0.5. "
        f"Related markets in the same event provide cross-references — use them "
        f"to identify opponents, related outcomes, and implied probabilities. "
        f"Output STRICT JSON only — no markdown fence, no prose before/after. "
        f"Schema: {{\"probability_yes\":0..1, \"confidence\":0..1, "
        f"\"category\":\"macro|geopolitics|politics|crypto|tech|sports|entertainment|science|other\", "
        f"\"market_archetype\":\"price_threshold|binary_event|sports_match|sports_prop|"
        f"geopolitical|macro_release|crypto|entertainment|scientific|legal|weather|other\", "
        f"\"resolution_style\":\"precise|broad|ambiguous|multi_factor\", "
        f"\"resolution_quality_score\":0..1, "
        f"\"scenarios\":[{{\"description\":\"str\",\"p_scenario\":0..1,\"p_conditional\":0..1}}], "
        f"\"key_factors\":[\"short factor\",...], "
        f"\"reasoning\":\"<=200 words prose\"}} "
        f"The scenarios array is OPTIONAL but encouraged — include 2-4 mutually exclusive "
        f"scenarios if you can. Each has p_scenario (probability it occurs) and p_conditional "
        f"(probability of YES given that scenario). They should roughly satisfy: "
        f"sum(p_scenario * p_conditional) ≈ probability_yes. "
        f"If you cannot decompose into scenarios, omit the field entirely. "
        f"market_archetype: classify the market type. "
        f"price_threshold = will price/metric reach X by date; "
        f"binary_event = will a specific event happen (election, policy); "
        f"sports_match = head-to-head team/player outcome; "
        f"sports_prop = totals, spreads, player stats; "
        f"geopolitical = conflict, diplomacy, international events; "
        f"macro_release = economic data (CPI, jobs, GDP); "
        f"crypto = cryptocurrency price/adoption; "
        f"entertainment = awards, TV, music; "
        f"scientific = discoveries, approvals; "
        f"legal = court, regulatory; "
        f"weather = weather/climate events. "
        f"resolution_style: precise = unambiguous binary check; "
        f"broad = generally clear but some room for interpretation; "
        f"ambiguous = multiple valid interpretations; "
        f"multi_factor = depends on multiple conditions. "
        f"resolution_quality_score: 0=impossible to verify, 1=perfectly clear resolution criteria."
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
    # Self-improvement fields
    market_archetype:         str   = "other"
    resolution_style:         str   = "broad"
    resolution_quality_score: float = 0.5
    # Ensemble fields
    model_disagreement:  float = 0.0   # logit std across models (0 = agreement)
    n_models:            int   = 1     # number of models in ensemble
    research_quality:    float = 0.0   # research bundle quality score


def _parse_json(raw: str) -> Optional[dict]:
    t = raw.strip()
    # Strip markdown code fences
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
    # Try direct parse first
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Fallback: extract first JSON object from text (Claude sometimes
    # generates analysis prose before/after the JSON)
    brace_start = raw.find("{")
    if brace_start == -1:
        return None
    # Find the matching closing brace
    depth = 0
    for i in range(brace_start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[brace_start:i + 1])
                    if isinstance(obj, dict) and "probability_yes" in obj:
                        return obj
                except Exception:
                    pass
                break
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
        strategy_block: Optional[str] = None,
    ) -> Optional[MarketEvaluation]:
        if self._client is None:
            return None

        user = self._build_prompt(
            market,
            research_block=research_block,
            strategy_block=strategy_block,
        )
        loop = asyncio.get_running_loop()
        raw = None
        for attempt in range(3):
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: self._client.messages.create(
                        model      = config.CLAUDE_MODEL,
                        max_tokens = 1200,
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

        # Validate scenario decomposition if present.
        probability_yes = _clamp01(obj.get("probability_yes"), 0.5)
        scenarios = obj.get("scenarios", [])
        if scenarios and isinstance(scenarios, list):
            weighted_sum = sum(
                float(s.get("p_scenario", 0)) * float(s.get("p_conditional", 0))
                for s in scenarios if isinstance(s, dict)
            )
            # If decomposition is inconsistent by >10pp, flag it
            if abs(weighted_sum - probability_yes) > 0.10:
                print(f"[polymarket_eval] scenario inconsistency for {market.id}: "
                      f"weighted_sum={weighted_sum:.3f} vs p_yes={probability_yes:.3f}",
                      file=sys.stderr)

        # Parse and validate archetype/resolution fields.
        raw_archetype = str(obj.get("market_archetype") or "other").lower().strip()
        if raw_archetype not in VALID_ARCHETYPES:
            raw_archetype = "other"
        raw_res_style = str(obj.get("resolution_style") or "broad").lower().strip()
        if raw_res_style not in VALID_RESOLUTION_STYLES:
            raw_res_style = "broad"
        raw_res_quality = _clamp01(obj.get("resolution_quality_score"), 0.5)

        return MarketEvaluation(
            market_id       = market.id,
            probability_yes = _clamp01(obj.get("probability_yes"), 0.5),
            confidence      = _clamp01(obj.get("confidence"),      0.5),
            category        = str(obj.get("category") or "other")[:40],
            key_factors     = [str(x)[:200] for x in (obj.get("key_factors") or [])][:6],
            reasoning       = str(obj.get("reasoning") or "")[:4000],
            raw             = raw,
            market_archetype         = raw_archetype,
            resolution_style         = raw_res_style,
            resolution_quality_score = raw_res_quality,
        )

    async def evaluate_ensemble(
        self,
        market: PolyMarket,
        research_block: Optional[str] = None,
        strategy_block: Optional[str] = None,
    ) -> Optional[MarketEvaluation]:
        """
        Multi-model ensemble: Claude Sonnet (primary) + Gemini Flash (secondary).
        Aggregates via logit-averaging. Model disagreement penalises confidence.
        """
        import config as cfg
        if not getattr(cfg, "ENSEMBLE_ENABLED", False):
            return await self.evaluate(market, research_block, strategy_block)

        # Run both models concurrently
        primary_task = self.evaluate(market, research_block, strategy_block)
        secondary_task = self._evaluate_gemini(market, research_block)

        results = await asyncio.gather(primary_task, secondary_task, return_exceptions=True)

        primary = results[0] if not isinstance(results[0], Exception) else None
        secondary = results[1] if not isinstance(results[1], Exception) else None

        if primary is None:
            return secondary  # fallback to Gemini if Claude fails
        if secondary is None:
            return primary  # single model, no ensemble benefit

        # Logit-average the probabilities
        def logit(p):
            p = max(0.001, min(0.999, p))
            return math.log(p / (1 - p))
        def inv_logit(x):
            return 1 / (1 + math.exp(-x))

        logits = [logit(primary.probability_yes), logit(secondary.probability_yes)]
        mean_logit = sum(logits) / len(logits)
        std_logit = (sum((l - mean_logit)**2 for l in logits) / len(logits)) ** 0.5

        p_ensemble = inv_logit(mean_logit)

        # Disagreement penalty on confidence
        threshold = float(getattr(cfg, "ENSEMBLE_DISAGREEMENT_THRESHOLD", 0.15))
        agreement_factor = math.exp(-max(0, std_logit - threshold * 0.5))
        confidence_adj = primary.confidence * min(1.0, agreement_factor)

        return MarketEvaluation(
            market_id=primary.market_id,
            probability_yes=p_ensemble,
            confidence=confidence_adj,
            category=primary.category,
            key_factors=primary.key_factors,
            reasoning=primary.reasoning,
            raw=primary.raw,
            market_archetype=primary.market_archetype,
            resolution_style=primary.resolution_style,
            resolution_quality_score=primary.resolution_quality_score,
            model_disagreement=std_logit,
            n_models=2,
        )

    async def _evaluate_gemini(
        self,
        market: PolyMarket,
        research_block: Optional[str] = None,
    ) -> Optional[MarketEvaluation]:
        """Call Gemini as secondary model for ensemble diversity."""
        import os
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if not gemini_key:
            return None

        try:
            from google import genai
            client = genai.Client(api_key=gemini_key)
        except Exception:
            return None

        import config as cfg
        user = self._build_prompt(market, research_block=research_block)
        system = _system_prompt()
        # Gemini uses combined prompt
        combined = f"{system}\n\n{user}"

        loop = asyncio.get_running_loop()
        try:
            def _call():
                resp = client.models.generate_content(
                    model=getattr(cfg, "ENSEMBLE_SECONDARY_MODEL", "gemini-2.5-flash"),
                    contents=combined,
                    config={"response_mime_type": "application/json",
                            "max_output_tokens": 1000, "temperature": 0.2},
                )
                if resp.candidates:
                    return resp.candidates[0].content.parts[0].text
                return ""

            raw = await loop.run_in_executor(None, _call)
        except Exception as exc:
            print(f"[polymarket_eval] Gemini failed for {market.id}: {exc}", file=sys.stderr)
            return None

        if not raw:
            return None

        obj = _parse_json(raw)
        if obj is None:
            return None

        return MarketEvaluation(
            market_id=market.id,
            probability_yes=_clamp01(obj.get("probability_yes"), 0.5),
            confidence=_clamp01(obj.get("confidence"), 0.5),
            category=str(obj.get("category") or "other")[:40],
            key_factors=[str(x)[:200] for x in (obj.get("key_factors") or [])][:6],
            reasoning=str(obj.get("reasoning") or "")[:4000],
            raw=raw,
            market_archetype=str(obj.get("market_archetype") or "other").lower().strip(),
            resolution_style=str(obj.get("resolution_style") or "broad").lower().strip(),
            resolution_quality_score=_clamp01(obj.get("resolution_quality_score"), 0.5),
        )

    async def justify_extreme_edge(
        self,
        market: PolyMarket,
        evaluation: MarketEvaluation,
        research_block: Optional[str] = None,
    ) -> Optional[dict]:
        """
        For markets with extreme edge (>PM_EXTREME_EDGE_JUSTIFICATION_BPS),
        ask Claude to cite specific verifiable evidence.

        Returns dict with keys:
          - action: "allow" | "revise" | "skip"
          - revised_probability: float (only if action="revise")
          - justification_quality: float 0..1
          - cited_evidence: str
          - raw: str

        Returns None on API/parse failure (caller should treat as "skip").
        """
        if self._client is None:
            return None

        edge_pp = abs(evaluation.probability_yes - market.yes_price) * 100
        mp_pct = market.yes_price * 100
        cp_pct = evaluation.probability_yes * 100

        research_section = ""
        if research_block:
            block = research_block.strip()
            if len(block) > 4000:
                block = block[:4000] + "..."
            research_section = f"\n\nResearch context:\n{block}\n"

        prompt = (
            f"You previously estimated p(YES) = {cp_pct:.1f}% for:\n"
            f"\"{market.question}\"\n\n"
            f"The market price is {mp_pct:.1f}%. "
            f"Your estimate disagrees by {edge_pp:.0f} percentage points.\n\n"
            f"Large disagreements with liquid prediction markets are usually "
            f"wrong — the crowd has aggregated many opinions with real money.\n\n"
            f"To justify maintaining your estimate, you MUST cite specific, "
            f"verifiable evidence from the research below. Not reasoning, not "
            f"logic — concrete data points with sources.\n"
            f"{research_section}\n"
            f"Instructions:\n"
            f"1. If you can cite 1-3 specific verifiable data points that "
            f"contradict the market price, respond with action='allow'.\n"
            f"2. If you have some evidence but are less certain, revise your "
            f"probability toward the market and respond with action='revise'.\n"
            f"3. If you cannot cite specific contradicting data, respond with "
            f"action='skip'.\n\n"
            f"Return STRICT JSON only:\n"
            f"{{\"action\": \"allow|revise|skip\", "
            f"\"revised_probability\": 0..1, "
            f"\"justification_quality\": 0..1, "
            f"\"cited_evidence\": \"specific data with source\"}}"
        )

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self._client.messages.create(
                    model=config.CLAUDE_MODEL,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                ),
            )
            raw = response.content[0].text
        except Exception as exc:
            print(f"[polymarket_eval] justification failed for {market.id}: {exc}",
                  file=sys.stderr)
            return None

        obj = _parse_json(raw)
        if obj is None:
            print(f"[polymarket_eval] justification unparseable for {market.id}: "
                  f"{raw[:200]}", file=sys.stderr)
            return None

        action = str(obj.get("action", "skip")).lower().strip()
        if action not in ("allow", "revise", "skip"):
            action = "skip"

        result = {
            "action": action,
            "revised_probability": _clamp01(obj.get("revised_probability"),
                                            evaluation.probability_yes),
            "justification_quality": _clamp01(obj.get("justification_quality"), 0.3),
            "cited_evidence": str(obj.get("cited_evidence") or "")[:500],
            "raw": raw,
        }

        # If quality is too low, force skip regardless of stated action
        if result["justification_quality"] < 0.4 and action != "skip":
            print(f"[polymarket_eval] justification quality {result['justification_quality']:.2f} "
                  f"too low for {market.id} — forcing skip", file=sys.stderr)
            result["action"] = "skip"

        return result

    @staticmethod
    def _build_prompt(market: PolyMarket,
                      research_block: Optional[str] = None,
                      strategy_block: Optional[str] = None) -> str:
        desc = market.description.strip()
        if len(desc) > 2500:
            desc = desc[:2500] + "…"
        research_section = ""
        if research_block:
            block = research_block.strip()
            if len(block) > 8000:
                block = block[:8000] + "…"
            research_section = f"\n\nResearch context:\n{block}\n"
        strategy_section = ""
        if strategy_block:
            block = strategy_block.strip()
            if len(block) > 1800:
                block = block[:1800] + "…"
            strategy_section = (
                "\n\nStrategy memory from prior reviews "
                "(weak prior, use only if relevant):\n"
                f"{block}\n"
            )
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
            f"{research_section}"
            f"{strategy_section}\n"
            f"Evaluate whether the market price is correct. "
            f"Estimate the probability the YES outcome resolves true."
        )
