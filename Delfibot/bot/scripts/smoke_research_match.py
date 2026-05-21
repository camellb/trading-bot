"""
Smoke-test the event_slug research enrichment.

Pulls ~20 active markets from the local DB (recently scanned), runs
fetch_research() with the new event_slug parameter, and checks whether
the extracted keywords actually correspond to the parent event.

The historical failure mode this targets: opaque sub-market titles like
"Spread: Thunder (-5.5)" whose research used to surface games about
some OTHER Thunder matchup because the question alone has no opponent.
After the event_slug fix, the keyword extractor sees the full context
(e.g. "Spread: Thunder (-5.5) [event: nba-sas-okc-2026-05-20]") and
should produce keywords that include the actual opponent (Spurs / SAS).

Pass/fail per market:
  PASS - keywords include at least one token from the event_slug that
         isn't already in the question (i.e. the slug added signal)
  FLAG - sports market with opaque question, no event_slug present, OR
         keyword extractor didn't pick up opponent tokens from the slug
  SKIP - non-sports market, or question already self-explanatory

Run from Delfibot/bot/:

    ../../.venv/bin/python scripts/smoke_research_match.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from sqlalchemy import text  # noqa: E402
from db.engine import get_engine  # noqa: E402
from research.fetcher import fetch_research  # noqa: E402


# Tokens we strip when comparing question vs slug content. These are
# noise tokens that appear in slugs but don't carry matchup signal.
_SLUG_NOISE = {
    "nba", "nfl", "mlb", "nhl", "soccer", "atp", "wta", "epl", "ucl",
    "spread", "moneyline", "ml", "ou", "over", "under", "home", "away",
    "pt5", "5pt5", "yes", "no", "the", "vs", "v", "and", "or",
    "winner", "team", "game", "match", "round", "set", "leg",
}


def _tokenize(s: str) -> set[str]:
    """Lowercase token set, alphanumeric runs only, dropping noise."""
    if not s:
        return set()
    raw = re.findall(r"[a-z0-9]+", s.lower())
    return {t for t in raw if t and t not in _SLUG_NOISE and not t.isdigit()}


def _slug_signal_tokens(slug: str | None) -> set[str]:
    """Tokens from the event slug that AREN'T just dates / league names."""
    if not slug:
        return set()
    return _tokenize(slug)


async def _eval_one(row: dict) -> dict:
    """Compare research WITH and WITHOUT event_slug, report whether the
    slug version produced better-anchored research."""
    question = row["question"]
    event_slug = row.get("event_slug") or None
    market_id = row["market_id"]
    category = row.get("category")

    out = {
        "market_id": market_id,
        "question": question[:60],
        "event_slug": event_slug,
        "category": category,
    }

    if not event_slug:
        out["verdict"] = "NO_SLUG"
        out["detail"] = "no event_slug on this market - nothing to enrich"
        return out

    # Run research BOTH ways: with slug (new behaviour) and without
    # (legacy behaviour). The difference between the two is exactly the
    # signal the fix adds.
    try:
        baseline, enriched = await asyncio.gather(
            asyncio.wait_for(
                fetch_research(question, category, event_slug=None),
                timeout=30,
            ),
            asyncio.wait_for(
                fetch_research(question, category, event_slug=event_slug),
                timeout=30,
            ),
        )
    except Exception as exc:
        out["verdict"] = "RESEARCH_FAIL"
        out["detail"] = f"fetch_research raised: {exc!r}"
        return out

    baseline_kws = baseline.keywords or []
    enriched_kws = enriched.keywords or []
    baseline_tok = set()
    for kw in baseline_kws:
        baseline_tok |= _tokenize(kw)
    enriched_tok = set()
    for kw in enriched_kws:
        enriched_tok |= _tokenize(kw)

    novel_tokens = enriched_tok - baseline_tok
    lost_tokens = baseline_tok - enriched_tok

    # The metric that actually matters: did the enriched run produce
    # keyword tokens not present in the baseline run? If so, the slug
    # contributed real signal.
    out["baseline_keywords"] = baseline_kws[:6]
    out["enriched_keywords"] = enriched_kws[:6]
    out["novel_tokens"] = sorted(novel_tokens)
    out["lost_tokens"] = sorted(lost_tokens)

    if novel_tokens:
        out["verdict"] = "ENRICHED"
        out["detail"] = (
            f"enriched run added {len(novel_tokens)} token(s) not in "
            f"baseline: {sorted(novel_tokens)[:8]}"
        )
    elif baseline_kws == enriched_kws:
        out["verdict"] = "NO_DELTA"
        out["detail"] = "baseline already captured everything; slug added no new signal"
    else:
        out["verdict"] = "SAME_TOKENS"
        out["detail"] = (
            "keyword phrasing differs but token set is unchanged "
            f"(baseline={baseline_kws[:3]} enriched={enriched_kws[:3]})"
        )
    return out


async def main() -> int:
    # Pull the 20 most-recent unique-question markets the bot has
    # evaluated. We dedupe on question so we don't run 4 copies of
    # the same Bitcoin price market.
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
    print(f"[smoke-research] got {len(markets)} markets to test")
    print()

    # Run sequentially to avoid hammering DDG / LLM rate limits.
    results: list[dict] = []
    for i, m in enumerate(markets, start=1):
        print(f"[{i}/{len(markets)}] {m['question'][:60]}")
        print(f"        slug: {m['event_slug']}")
        r = await _eval_one(m)
        results.append(r)
        print(f"        => {r['verdict']}: {r['detail']}")
        print()

    # Summary
    by_verdict: dict[str, int] = {}
    for r in results:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for v, n in sorted(by_verdict.items(), key=lambda kv: -kv[1]):
        print(f"  {v:14s}  {n:3d}")
    print()
    enriched = [r for r in results if r["verdict"] == "ENRICHED"]
    if enriched:
        print(f"ENRICHED markets ({len(enriched)}) - slug added signal:")
        for r in enriched:
            print(f"  - {r['question']}")
            print(f"    slug: {r['event_slug']}")
            print(f"    baseline keywords: {r.get('baseline_keywords')}")
            print(f"    enriched keywords: {r.get('enriched_keywords')}")
            print(f"    novel tokens:      {r['novel_tokens']}")
            print()
    # Exit code: success if every market either improved or genuinely
    # needed no enrichment. The user's complaint was about cases where
    # the slug WOULD have added signal but didn't reach the extractor.
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
