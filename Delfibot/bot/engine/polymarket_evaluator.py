"""
Polymarket evaluator - wraps Claude to produce a calibrated probability for
a single binary prediction market.

Design goals:
  * Claude sees the question + description + resolution horizon + market price.
  * Market price is context only, not a prior. The prompt explicitly tells
    Claude to forecast what the evidence says regardless of the crowd price
    and never to anchor or hedge toward it. Side selection downstream is
    based on Claude's forecast alone (back-the-forecast doctrine).
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

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
from engine.llm_client import get_llm
from feeds.polymarket_feed import PolyMarket


def _system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"You are a forecaster. Your job is to estimate the true probability "
        f"that the YES outcome of this binary prediction market will resolve "
        f"true, based on the evidence available to you. "
        f"Today is {today}. "
        f"CRITICAL: Your training data is OUTDATED. Do NOT use your memory for "
        f"current prices, scores, standings, poll numbers, or recent events - "
        f"they WILL be wrong. Use ONLY the research context provided below. "
        f"If the research does not contain a key fact you need, say so in your "
        f"reasoning and set confidence LOW. "
        f"SAME-EVENT CHECK (do this FIRST, before forecasting): The user "
        f"prompt includes an EVENT CONTEXT block stating the market's exact "
        f"date and event. Many research snippets describe DIFFERENT editions "
        f"of the same recurring event (e.g. last year's tournament, prior "
        f"election cycles, earlier games in a series). Before you forecast, "
        f"verify that the research is about the SAME event the market asks "
        f"about. If most evidence describes a different edition / date / "
        f"matchup, set `same_event_verified` to 'no' and return a low "
        f"confidence skip - do NOT fabricate a forecast from off-event data. "
        f"SOURCE QUALITY: research snippets are tagged [tier-A: domain] or "
        f"[tier-B: domain]. Tier-A sources are league-official, "
        f"authoritative newsrooms (Reuters/AP/BBC), reference stats sites, "
        f"or first-party sources. Weight Tier-A heavily. Tier-B is "
        f"everything else; use it for corroboration but not as primary "
        f"evidence. Full-page extracts include a publish date "
        f"(e.g. '[reuters.com | 2026-05-17]'); evidence dated before the "
        f"market's event window is HISTORICAL CONTEXT ONLY and must not "
        f"drive a forecast about a future-edition event. "
        f"CITE SOURCES IN REASONING: when a specific fact drives your "
        f"estimate, name the source domain inline (e.g. 'per atptour.com, "
        f"Sinner is the top seed and faces Ruud in the semifinal'). This "
        f"is read by paying users; vague references like 'the research "
        f"shows' are not acceptable. "
        f"HOW TO FORECAST: Read the resolution criteria carefully. Read the "
        f"research. Think about what has to be true for YES to resolve, and "
        f"what has to be true for NO. Weigh the evidence. Produce an honest "
        f"probability estimate. "
        f"If the evidence strongly points to YES, output a high probability "
        f"(e.g. 0.80, 0.90). If it strongly points to NO, output a low "
        f"probability (e.g. 0.15, 0.05). If the evidence is mixed or "
        f"inconclusive, your probability should reflect that - and your "
        f"confidence should drop accordingly. "
        f"The market price is shown to you as context so you know what the bot "
        f"is trading against. It is NOT a prior. It does NOT constrain your "
        f"estimate. You may ignore it entirely when forecasting. If your "
        f"estimate happens to match the market, fine. If it differs "
        f"substantially, also fine - report what the evidence says. "
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
        f"honest about uncertainty - low confidence translates into a smaller "
        f"stake downstream. That is the correct way to handle thin evidence, "
        f"not by copying the market. "
        f"LIVE MARKET DATA: If the research context contains a section "
        f"labelled 'LIVE MARKET DATA (REAL-TIME)', that block is evidence for "
        f"short-horizon price-direction markets - current spot, recent "
        f"changes (15m/1h/24h), order-book imbalance, spread, funding rate, "
        f"recent candles - fetched seconds ago from OKX (crypto) or Yahoo "
        f"Finance (equities). Use it to ground your probability in actual "
        f"price dynamics. "
        f"If the LIVE MARKET DATA block is missing, stale (>60s for crypto / "
        f">120s for equity), or shows a zero/bad price, TREAT THIS AS MISSING "
        f"EVIDENCE and LOWER CONFIDENCE. Do not fabricate price levels from "
        f"memory. "
        f"VOICE: Your reasoning is read by a paying customer on a product "
        f"surface, not by another model and not in a lab notebook. Write like "
        f"a senior analyst briefing a principal. Concrete nouns, short "
        f"sentences, active voice. No hedging filler ('it seems', 'it "
        f"appears', 'arguably'). No meta commentary about the research "
        f"process ('the research shows', 'based on the context', 'according "
        f"to the data provided'). No apologies for uncertainty: state what "
        f"you know, state what you don't, state the forecast. Never mention "
        f"the model, the prompt, the research pipeline, or internal tooling. "
        f"Name the concrete facts driving the estimate. If a number matters, "
        f"cite it (e.g. 'trailing 30-day hashrate up 12%', not 'hashrate "
        f"trending up'). The reader should come away knowing exactly what "
        f"evidence drove the call. "
        f"REASONING_SHORT: In addition to the full reasoning, write a "
        f"standalone one-sentence summary of the call (max 140 characters). "
        f"It must stand on its own as a product caption: no ellipses, no "
        f"'because of the above', no references to the long reasoning. Lead "
        f"with the driver, then the direction. Example: 'Incumbent polls +6 "
        f"nationally with 3 weeks left and no debate scheduled; YES favored.' "
        f"DIRECTIONAL SELF-CHECK: After writing your reasoning, look at "
        f"it again as if a stranger had to call the trade. Which side "
        f"does the prose make a stronger case for? "
        f"Set `reasoning_direction` to that side ('YES' or 'NO'). "
        f"Then ensure your `probability_yes` matches: "
        f"reasoning_direction='YES' MUST mean probability_yes >= 0.50, "
        f"reasoning_direction='NO'  MUST mean probability_yes <= 0.50. "
        f"If your prose is genuinely balanced, set reasoning_direction to "
        f"the SAME side as your probability_yes (i.e. 'YES' if >=0.50, "
        f"else 'NO') and lower confidence to <=0.4. "
        f"WHY THIS MATTERS: a forecaster whose reasoning argues YES but "
        f"whose probability says NO is broken on that call. The bot will "
        f"reject inconsistent outputs entirely and skip the trade rather "
        f"than risk acting on incoherent reasoning. "
        f"Output STRICT JSON only - no markdown fence, no prose before/after. "
        f"Schema (output fields IN THIS ORDER; same_event_verified comes "
        f"FIRST so you commit to the evidence check before producing a "
        f"probability): "
        f"{{\"same_event_verified\":\"yes|partial|no\", "
        f"\"same_event_note\":\"<=120 char one-sentence justification of "
        f"the verification value, naming the specific evidence-vs-market "
        f"mismatch if any\", "
        f"\"probability_yes\":0..1, \"confidence\":0..1, "
        f"\"reasoning_direction\":\"YES|NO\", "
        f"\"category\":\"macro|geopolitics|politics|crypto|tech|sports|entertainment|science|other\", "
        f"\"key_factors\":[\"short factor with inline source domain\",...], "
        f"\"reasoning_short\":\"<=140 char one-sentence summary\", "
        f"\"reasoning\":\"<=200 words prose, citing source domains inline\"}}"
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
    reasoning_short:  str = ""


def _parse_json(raw: str) -> Optional[dict]:
    """Parse JSON leniently: strip fences, extract first {...} substring."""
    if not raw:
        return None
    t = raw.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(t[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _clamp01(x, default=0.5, *, field: str = ""):
    """Coerce to a probability in [0,1]. Warn on out-of-range numerics
    so a model returning `probability_yes: 1.5` (or -0.2) isn't
    silently treated as a perfectly confident YES (or NO). The
    earlier version clamped quietly; an OOR number is a sign the
    model mis-formatted, worth a stderr line so the operator can
    spot recurring patterns."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v < 0.0 or v > 1.0:
        import sys as _sys
        label = f"[{field}]" if field else ""
        print(
            f"[evaluator] _clamp01{label} out-of-range value {v!r}, "
            "clamping to [0,1]",
            file=_sys.stderr,
        )
    return max(0.0, min(1.0, v))


class PolymarketEvaluator:
    """
    Per-market forecaster. Delegates the actual LLM call to
    engine.llm_client (provider-routed, primary/backup failover,
    Anthropic prompt caching on the system block).

    No more self._client: state lives in the llm_client singleton.
    Credential hot-reload calls llm_client.reset_llm() so the next
    evaluate() picks up the new key without a daemon restart.
    """

    def __init__(self) -> None:
        # Kept as a no-op for back-compat with `PolymarketEvaluator()`
        # call sites. No per-instance state.
        pass

    async def evaluate(
        self,
        market: PolyMarket,
        research_block: Optional[str] = None,
    ) -> Optional[MarketEvaluation]:
        user = self._build_prompt(market, research_block)
        # cache_system=True is the prompt-caching win: the ~1500-token
        # system prompt is identical for every market in a scan, so
        # the first call writes the cache (1.25x once) and the next
        # 50+ calls within the 5-min ephemeral TTL hit it at 0.1x
        # input pricing. llm_client logs cache write/read tokens to
        # stderr when present so we can confirm the savings.
        raw = await get_llm().call(
            system       = _system_prompt(),
            user         = user,
            max_tokens   = 2000,
            cache_system = True,
        )
        if not raw:
            print(f"[polymarket_eval] no usable LLM response for "
                  f"{market.id} (every provider failed; see "
                  f"earlier [llm_client] lines)", file=sys.stderr)
            return None

        obj = _parse_json(raw)
        if obj is None:
            print(f"[polymarket_eval] unparseable JSON for {market.id} "
                  f"(raw_len={len(raw)}): {raw[:500]!r} ... tail={raw[-200:]!r}",
                  file=sys.stderr)
            return None

        reasoning_short_raw = str(obj.get("reasoning_short") or "").strip()
        if len(reasoning_short_raw) > 140:
            reasoning_short_raw = reasoning_short_raw[:137].rstrip() + "..."

        # ── Same-event verification gate ────────────────────────────────
        # The forecaster outputs `same_event_verified` BEFORE the
        # probability so the model commits to the evidence check
        # before producing a number. When the model reports the
        # research is about a DIFFERENT event/edition, force a low-
        # confidence skip — the JSON's `same_event_note` is surfaced
        # in the user-facing reasoning so the skip is intelligible.
        sev_raw = str(obj.get("same_event_verified") or "").strip().lower()
        if sev_raw == "no":
            note = str(obj.get("same_event_note") or "").strip()
            print(
                f"[polymarket_eval] same_event_verified=no on "
                f"{market.id} — skipping. note={note[:160]!r}",
                file=sys.stderr,
            )
            # Force claude_p to land on the OPPOSITE side of 0.50 from
            # the market favourite so the sizer's direction-agreement
            # gate actually trips a SKIP.
            #
            # Earlier wiring set `probability_yes = market.yes_price`
            # on the (incorrect) theory that "match the market = no
            # signal = no trade". But the V1 gate is
            # `(claude_p - 0.50) * (market_p - 0.50) < 0` —
            # "DIFFERENT sides of 0.50". Equality satisfies "SAME
            # side", so the gate didn't fire and the position opened
            # anyway. Real example 2026-05-18: Arsenal -2.5 spread
            # at 0.495 paired with research about a different match
            # (Champions League semifinal vs Atletico, not Premier
            # League vs Burnley). Evaluator emitted same_event=no,
            # set claude_p=0.495=market, sizer accepted the
            # direction match, bot opened a $2.52 NO position on a
            # market its own research didn't describe. User-reported
            # "It shows skipped but it's live on Polymarket".
            market_p_yes = float(market.yes_price)
            forced_p_yes = 0.49 if market_p_yes >= 0.50 else 0.51
            return MarketEvaluation(
                market_id       = market.id,
                probability_yes = forced_p_yes,
                confidence      = 0.10,
                category        = str(obj.get("category") or "other")[:40],
                key_factors     = ["evidence_off_event"],
                reasoning       = (
                    f"Skipped: the research available does not describe "
                    f"this specific event/edition. {note}"
                )[:4000],
                raw             = raw,
                reasoning_short = (
                    "Skipped: research is about a different edition/date "
                    "than this market."
                )[:140],
            )

        # ── Reasoning-vs-probability consistency gate ───────────────────
        # The forecaster has a documented failure mode where the prose
        # argues one side and the probability lands on the other. Real
        # case from 2026-05-02: BTC reasoning argued strongly for YES
        # ("price action strongly supports this possibility... could
        # easily push through $79,000") but probability_yes=0.25,
        # leading to a $-15.62 NO bet that lost when BTC reached $79k.
        #
        # Fix: require the model to output a `reasoning_direction`
        # field declaring which side the prose argues for. If that
        # disagrees with the probability_yes >= 0.50 rule, the
        # evaluation is rejected entirely - we'd rather skip than act
        # on incoherent reasoning. The downstream analyst already
        # treats `None` evaluations as SKIP_INVALID.
        prob_yes = _clamp01(obj.get("probability_yes"), 0.5)
        rd_raw = str(obj.get("reasoning_direction") or "").strip().upper()
        if rd_raw not in ("YES", "NO"):
            print(
                f"[polymarket_eval] missing/invalid reasoning_direction "
                f"on {market.id}: {rd_raw!r} - skipping",
                file=sys.stderr,
            )
            return None
        # 0.50 is the boundary; treat it as agreeing with EITHER side
        # to avoid a tied-probability false-rejection.
        delfi_implied = "YES" if prob_yes >= 0.50 else "NO"
        if rd_raw != delfi_implied and abs(prob_yes - 0.50) > 0.01:
            print(
                f"[polymarket_eval] reasoning/probability mismatch on "
                f"{market.id}: reasoning_direction={rd_raw} but "
                f"probability_yes={prob_yes:.3f} (implies {delfi_implied}). "
                f"Rejecting the evaluation - the forecaster contradicted "
                f"itself on this market.",
                file=sys.stderr,
            )
            return None

        return MarketEvaluation(
            market_id       = market.id,
            probability_yes = prob_yes,
            confidence      = _clamp01(obj.get("confidence"),      0.5),
            category        = str(obj.get("category") or "other")[:40],
            key_factors     = [str(x)[:200] for x in (obj.get("key_factors") or [])][:6],
            reasoning       = str(obj.get("reasoning") or "")[:4000],
            raw             = raw,
            reasoning_short = reasoning_short_raw,
        )

    @staticmethod
    def _build_prompt(market: PolyMarket,
                      research_block: Optional[str] = None) -> str:
        # Build an unmissable event-context block at the TOP of the
        # user prompt. The system prompt's date hint is a few hundred
        # tokens up from where the question lands; putting the exact
        # event window right next to the question + repeating the
        # "evidence from outside this window is HISTORICAL ONLY" rule
        # keeps the forecaster anchored even when DDG results are
        # noisy. This is the single highest-leverage prompt change
        # for preventing wrong-year confusion.
        now = datetime.now(timezone.utc)
        try:
            resolve_at = market.resolution_at_estimate
        except Exception:
            resolve_at = market.end_date_iso
        try:
            hours_until = (resolve_at - now).total_seconds() / 3600.0
        except Exception:
            hours_until = None
        event_window = (
            f"Today: {now.strftime('%Y-%m-%d %A')} (UTC). "
            f"Market resolves: {resolve_at.isoformat()} "
            f"({'in ' + format(hours_until, '.1f') + 'h' if hours_until is not None and hours_until >= 0 else 'past due'}). "
            f"Event year: {resolve_at.year}."
        )
        event_context = (
            "EVENT CONTEXT (do not ignore):\n"
            f"  {event_window}\n"
            "  Any evidence in the research below that describes events "
            "OUTSIDE this window (e.g. prior editions of the same "
            "tournament, prior election cycles, last year's stats) is "
            "HISTORICAL CONTEXT ONLY. It must NOT drive your forecast "
            "of how THIS market resolves. If the bulk of the research "
            "describes a different edition, set same_event_verified='no'.\n"
        )

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
                f"YES means specifically \"{market.group_item_title}\" - "
                f"not any other option. Estimate accordingly."
            )
        context_section = "\n".join(context_lines)

        no_price = 1.0 - market.yes_price
        market_price_section = (
            f"\n\nFor context - current market price: YES = {market.yes_price:.2f}, "
            f"NO = {no_price:.2f}. "
            f"This is what the crowd thinks. It is shown so you know what the "
            f"bot is trading against. It is context only - do NOT anchor your "
            f"probability estimate to it.\n"
        )

        return (
            f"{event_context}\n"
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
