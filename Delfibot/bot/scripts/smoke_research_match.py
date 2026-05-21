"""
End-to-end smoke test: does the research bundle actually describe the
correct market?

This is the REAL test of the event_slug fix. We don't just check that
the keyword extractor saw the slug - we run the full research pipeline
and ask an LLM the same question the forecaster asks itself
(`same_event_verified`): does this evidence describe THIS specific
event, or a different edition / matchup / date?

For each of N recent markets:
  1. fetch_research(question, event_slug=...)  - full pipeline, with
     web search, Wikipedia, news, base rates.
  2. Build the prompt block the forecaster would see.
  3. Ask Claude (or fallback LLM): given the market question and the
     research bundle, is the research about the CORRECT event? Answer
     YES / PARTIAL / NO with a one-sentence reason.
  4. Per-market verdict:
       MATCH    - LLM says YES, research aligns with the market
       PARTIAL  - some on-event evidence, some off-event
       MISMATCH - research describes a DIFFERENT event (the user's
                  original complaint - Thunder/Spurs market with
                  Thunder/Timberwolves research)
       NO_RESEARCH - bundle was empty (research failed)

Exit non-zero if ANY sports/event market comes back MISMATCH.

Run from Delfibot/bot/:

    DELFI_DB_PATH=~/Library/Application\\ Support/com.delfi.desktop/delfi.db \\
    ../../.venv/bin/python scripts/smoke_research_match.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import text  # noqa: E402
from db.engine import get_engine  # noqa: E402
from research.fetcher import fetch_research  # noqa: E402
from engine.llm_client import get_llm  # noqa: E402


_VERIFY_PROMPT = (
    "You are auditing a research bundle for a prediction market. "
    "Your job is simple: does the research describe the SAME event the "
    "market is asking about? "
    "\n\n"
    "The market often has a date (explicit in the question or implicit in the "
    "event slug). The research may describe a DIFFERENT edition of the same "
    "recurring event - last year's tournament, a different game in a series, "
    "a different round, a different matchup. If most of the research is "
    "about a different event, that is a MISMATCH and the trade should be "
    "skipped (the bot already does this; we are testing whether the research "
    "fetcher is giving good data). "
    "\n\n"
    "Output STRICT JSON only, no prose:\n"
    "{\n"
    '  "verdict": "MATCH" | "PARTIAL" | "MISMATCH" | "NO_RESEARCH",\n'
    '  "reason": "<= 200 char one-sentence justification, cite which '
    "snippet(s) made you decide\"\n"
    "}\n"
    "\n"
    "MATCH    = >=70% of research clearly describes THIS event/edition/date.\n"
    "PARTIAL  = some on-event evidence but mixed with off-event noise.\n"
    "MISMATCH = research is mostly about a different game/date/edition - "
    "the trade would be a coin flip if Delfi traded on this.\n"
    "NO_RESEARCH = the bundle is essentially empty or only generic "
    "background with no event-specific facts.\n"
)


async def _verify_one(question: str, event_slug: str | None,
                      research_block: str) -> dict:
    llm = get_llm()
    user_prompt = (
        f"MARKET QUESTION: {question}\n"
        f"EVENT SLUG (parent event on Polymarket): {event_slug or '(none)'}\n"
        f"\n"
        f"--- RESEARCH BUNDLE START ---\n"
        f"{research_block}\n"
        f"--- RESEARCH BUNDLE END ---\n"
    )
    raw = await llm.call(
        system=_VERIFY_PROMPT,
        user=user_prompt,
        max_tokens=400,
        temperature=0.0,
    )
    if not raw:
        return {"verdict": "ERROR", "reason": "LLM returned no response"}
    raw = raw.strip()
    # Strip markdown code fence if Claude wrapped the JSON
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"verdict": "PARSE_ERROR",
                "reason": f"could not parse LLM output: {exc}; raw={raw[:200]!r}"}
    verdict = str(obj.get("verdict") or "").upper().strip()
    if verdict not in ("MATCH", "PARTIAL", "MISMATCH", "NO_RESEARCH"):
        return {"verdict": "PARSE_ERROR",
                "reason": f"unknown verdict {verdict!r}; raw={raw[:200]!r}"}
    return {"verdict": verdict,
            "reason": (str(obj.get("reason") or "")[:300])}


async def _eval_one(row: dict) -> dict:
    question = row["question"]
    event_slug = row.get("event_slug") or None
    market_id = row["market_id"]
    category = row.get("category")

    out = {
        "market_id": market_id,
        "question": question[:80],
        "event_slug": event_slug,
        "category": category,
    }

    # Faithful production reproduction: pass resolution_date so DDG
    # queries get pinned to the right month+year (the production
    # caller in pm_analyst does this via market.resolution_at_estimate).
    # We don't have the per-market resolution date in the DB row we
    # pulled, so use "today" - same fallback the fetcher uses when
    # resolution_date is None.
    resolution_date = datetime.now(timezone.utc)
    try:
        bundle = await asyncio.wait_for(
            fetch_research(
                question, category,
                event_slug=event_slug,
                resolution_date=resolution_date,
            ),
            timeout=60,
        )
    except Exception as exc:
        out["verdict"] = "RESEARCH_FAIL"
        out["reason"] = f"fetch_research raised: {exc!r}"
        return out

    research_block = bundle.to_prompt_block()
    out["bundle_chars"] = len(research_block)
    out["sources"] = bundle.sources

    if len(research_block.strip()) < 200:
        out["verdict"] = "NO_RESEARCH"
        out["reason"] = f"bundle too small ({len(research_block)} chars) - fetcher returned almost nothing"
        return out

    verdict_obj = await _verify_one(question, event_slug, research_block)
    out["verdict"] = verdict_obj["verdict"]
    out["reason"] = verdict_obj["reason"]
    return out


async def main() -> int:
    limit = int(os.environ.get("DELFI_RESEARCH_TEST_N", "20"))
    print(f"[smoke-research] fetching {limit} recent unique markets...")
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT market_id, question, category, event_slug
            FROM market_evaluations
            WHERE id IN (
                SELECT MAX(id) FROM market_evaluations
                GROUP BY question
            )
            ORDER BY evaluated_at DESC
            LIMIT :n
        """), {"n": limit}).mappings().all()
    markets = [dict(r) for r in rows]
    print(f"[smoke-research] got {len(markets)} markets to test\n")

    results: list[dict] = []
    for i, m in enumerate(markets, start=1):
        print(f"[{i}/{len(markets)}] {m['question'][:70]}")
        print(f"          slug: {m['event_slug']}")
        r = await _eval_one(m)
        results.append(r)
        bundle_chars = r.get("bundle_chars")
        if bundle_chars is not None:
            print(f"          bundle: {bundle_chars} chars, sources={r.get('sources')}")
        print(f"          => {r['verdict']}: {r['reason']}")
        print()

    # Summary
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for v in ("MATCH", "PARTIAL", "MISMATCH", "NO_RESEARCH",
              "RESEARCH_FAIL", "ERROR", "PARSE_ERROR"):
        n = counts.get(v, 0)
        if n:
            print(f"  {v:14s}  {n:3d}")
    print()

    mismatches = [r for r in results if r["verdict"] == "MISMATCH"]
    if mismatches:
        print(f"!!! {len(mismatches)} MISMATCH(es) - research about wrong event:")
        for r in mismatches:
            print(f"  - {r['question']}")
            print(f"    slug:   {r['event_slug']}")
            print(f"    reason: {r['reason']}")
            print()
        return 1

    partials = [r for r in results if r["verdict"] == "PARTIAL"]
    if partials:
        print(f"PARTIAL ({len(partials)}) - some off-event noise but on-event "
              "evidence present:")
        for r in partials:
            print(f"  - {r['question']}")
            print(f"    reason: {r['reason']}")
            print()
    print(f"PASS: zero MISMATCH out of {len(results)} markets tested.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
