"""
Markout tracker — measures whether the market moved toward Claude's estimate.

After each evaluation, the market YES price is recorded. This module checks
the price at T+1h, T+6h, and T+24h to see if the market agreed with Claude's
view. A consistently positive direction_correct rate is a diagnostic that
the forecaster saw something the market had not yet priced in — used to
track forecaster health, not as a trading gate.

Run via scheduler every hour (same cadence as the resolver).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone, timedelta

from sqlalchemy import text

from db.engine import get_engine
from feeds.polymarket_feed import PolymarketFeed, _parse_price_list

MARKOUT_HOURS = [1, 6, 24]


async def check_markouts() -> None:
    """
    Scan market_evaluations for pending markout checks, fetch current prices,
    and record results.
    """
    loop = asyncio.get_running_loop()

    # Step 1: Find evaluations that need markout checks.
    def _find_pending():
        now = datetime.now(timezone.utc)
        rows = []
        with get_engine().begin() as conn:
            evals = conn.execute(text(
                "SELECT me.id, me.evaluated_at, me.market_id, "
                "       me.market_price_yes, me.claude_probability "
                "FROM market_evaluations me "
                "WHERE me.evaluated_at >= NOW() - INTERVAL '48 hours' "
                "ORDER BY me.evaluated_at DESC"
            )).fetchall()

            for ev in evals:
                eval_id, evaluated_at, market_id, price_yes, claude_p = ev
                if evaluated_at is None or price_yes is None or claude_p is None:
                    continue

                # Which hours_after values already exist?
                existing = conn.execute(text(
                    "SELECT hours_after FROM markouts "
                    "WHERE evaluation_id = :eid"
                ), {"eid": eval_id}).fetchall()
                done = {r[0] for r in existing}

                for h in MARKOUT_HOURS:
                    if h in done:
                        continue
                    due_at = evaluated_at + timedelta(hours=h)
                    if now >= due_at:
                        rows.append({
                            "eval_id": eval_id,
                            "market_id": market_id,
                            "hours_after": h,
                            "price_yes_at_eval": float(price_yes),
                            "claude_probability": float(claude_p),
                        })
        return rows

    pending = await loop.run_in_executor(None, _find_pending)
    if not pending:
        print("[markout] no pending markouts", flush=True)
        return

    # Step 2: Collect unique market IDs to fetch.
    unique_market_ids = list({r["market_id"] for r in pending})
    print(
        f"[markout] {len(pending)} pending checks across "
        f"{len(unique_market_ids)} markets",
        flush=True,
    )

    # Step 3: Fetch current prices with bounded concurrency.
    async with PolymarketFeed() as feed:
        market_data = await feed.fetch_many(unique_market_ids)

    # Build a map of market_id -> current YES price.
    current_prices: dict[str, float] = {}
    for mid, raw in market_data.items():
        prices = _parse_price_list(raw.get("outcomePrices"))
        if len(prices) >= 1:
            current_prices[mid] = float(prices[0])

    # Step 4: Compute direction_correct and insert rows.
    recorded = 0

    def _insert_markouts():
        nonlocal recorded
        with get_engine().begin() as conn:
            for row in pending:
                mid = row["market_id"]
                if mid not in current_prices:
                    continue

                price_now = current_prices[mid]
                price_at_eval = row["price_yes_at_eval"]
                claude_p = row["claude_probability"]

                # Direction correct: did price move toward Claude's estimate?
                # If Claude said p > market_price → YES underpriced → price going UP is correct.
                # If Claude said p < market_price → NO underpriced → price going DOWN is correct.
                if claude_p > price_at_eval:
                    direction_correct = price_now > price_at_eval
                elif claude_p < price_at_eval:
                    direction_correct = price_now < price_at_eval
                else:
                    # Claude agreed with the market exactly — mark as correct
                    # regardless of where price drifted. The forecaster
                    # did not claim a directional move, so the markout
                    # shouldn't punish the absence of one.
                    direction_correct = True

                from engine.user_config import DEFAULT_USER_ID
                conn.execute(text(
                    "INSERT INTO markouts "
                    "(user_id, evaluation_id, market_id, hours_after, "
                    " price_yes_at_check, price_yes_at_eval, "
                    " claude_probability, direction_correct) "
                    "VALUES (:user_id, :eid, :mid, :h, :pc, :pe, :cp, :dc)"
                ), {
                    "user_id": DEFAULT_USER_ID,
                    "eid": row["eval_id"],
                    "mid": mid,
                    "h":   row["hours_after"],
                    "pc":  price_now,
                    "pe":  price_at_eval,
                    "cp":  claude_p,
                    "dc":  direction_correct,
                })
                recorded += 1

    await loop.run_in_executor(None, _insert_markouts)

    print(
        f"[markout] checked {len(pending)} evaluations, "
        f"recorded {recorded} markouts",
        flush=True,
    )
