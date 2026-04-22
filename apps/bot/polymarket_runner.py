"""
Polymarket runner — the two scheduled entrypoints wired into main.py.

scan_and_analyze(limit):
    Fetch candidate markets → research → Claude evaluation → sizing →
    open shadow/live position. All gated by the PMAnalyst pipeline.

resolve_positions():
    For every open pm_positions row, check whether the underlying Polymarket
    market has resolved. If yes, settle via PMExecutor (writes settlement
    price, realized P&L, and feeds the calibration ledger). Also opportunistically
    backfills legacy shadow-mode predictions that lack a pm_positions row.

Both functions are safe to call repeatedly and never raise on partial failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import text

import calibration
import config
from db.engine import get_engine
from engine.pm_analyst import PMAnalyst
from execution.pm_executor import PMExecutor
from feeds.polymarket_feed import PolymarketFeed


SOURCE = "polymarket"



_scan_lock = asyncio.Lock()


# ── Scan + analyse (primary entrypoint) ──────────────────────────────────────
async def scan_and_analyze(
    limit:          int   = 20,
    min_volume_24h: float = 5_000.0,
    notifier:       object = None,
    analyst:        PMAnalyst | None = None,
) -> dict:
    """
    Delegate to PMAnalyst. Returns the analyst's summary dict.
    Uses a module-level lock to prevent overlapping scans from
    scheduler, Telegram, and dashboard.
    """
    if _scan_lock.locked():
        print("[pm_runner] scan already in progress, skipping", flush=True)
        return {"skipped": True, "reason": "scan already in progress"}
    async with _scan_lock:
        if analyst is None:
            analyst = PMAnalyst(notifier=notifier)
        return await analyst.scan_and_analyze(limit=limit,
                                                min_volume_24h=min_volume_24h)


# ── Resolve positions + legacy predictions ──────────────────────────────────
async def resolve_positions(short_horizon_only: bool = False, notifier=None, executor: PMExecutor | None = None) -> dict:
    """
    Two-phase settlement:

        Phase A — settle every open pm_positions row against the resolved
                  Polymarket market state.
        Phase B — resolve legacy `predictions` rows from the shadow-only
                  era that lack a pm_positions partner.

    Returns: {"positions_checked", "positions_settled",
              "predictions_checked", "predictions_resolved", "errors"}
    """
    result = {
        "positions_checked": 0, "positions_settled": 0,
        "predictions_checked": 0, "predictions_resolved": 0,
        "errors": 0,
    }

    open_rows   = _fetch_open_positions(short_horizon_only=short_horizon_only)
    legacy_rows = [] if short_horizon_only else _fetch_unresolved_legacy_predictions()

    market_ids = list({p["market_id"] for p in open_rows if p.get("market_id")}
                      | {p["market_id"] for p in legacy_rows if p.get("market_id")})
    if not market_ids:
        return result

    async with PolymarketFeed() as feed:
        rows = await feed.fetch_many(market_ids)

    if executor is None:
        executor = PMExecutor()

    # Phase A — pm_positions.
    for p in open_rows:
        result["positions_checked"] += 1
        raw = rows.get(p["market_id"])
        if not raw or not raw.get("closed", False):
            continue
        prices = _parse_price_list(raw.get("outcomePrices"))
        if len(prices) != 2:
            continue
        yes_won = prices[0] >= 0.99
        no_won  = prices[1] >= 0.99
        if not (yes_won or no_won):
            print(f"[resolve] INVALID settlement for pos #{p['id']} "
                  f"market={p['market_id']} prices={prices} "
                  f"(neither >= 0.99)", flush=True)
            executor.settle_position(p["id"], "INVALID", 0.5)
            result["errors"] += 1
            continue
        outcome = "YES" if yes_won else "NO"
        if executor.settle_position(p["id"], outcome):
            result["positions_settled"] += 1
            if notifier and hasattr(notifier, "notify_settlement"):
                side = p.get("side", "?")
                pnl = (1.0 if outcome == side else 0.0) * p["shares"] - p["cost_usd"]
                try:
                    await notifier.notify_settlement(
                        position_id=p["id"],
                        question=p.get("question", ""),
                        side=side, outcome=outcome, pnl=pnl,
                        cost=p["cost_usd"],
                    )
                except Exception:
                    pass
        else:
            result["errors"] += 1

    # Phase B — legacy predictions without a pm_positions row.
    for p in legacy_rows:
        result["predictions_checked"] += 1
        raw = rows.get(p["market_id"])
        if not raw or not raw.get("closed", False):
            continue
        prices = _parse_price_list(raw.get("outcomePrices"))
        if len(prices) != 2:
            continue
        yes_won = prices[0] >= 0.99
        no_won  = prices[1] >= 0.99
        if not (yes_won or no_won):
            continue

        predicted_yes  = float(p["probability"])
        claude_bet_yes = predicted_yes > 0.5
        claude_correct = (claude_bet_yes and yes_won) or (not claude_bet_yes and no_won)

        meta = _parse_json(p.get("metadata"))
        yes_px_at_pred = float((meta or {}).get("yes_price_at_prediction", 0.5))
        if claude_bet_yes:
            cost   = yes_px_at_pred
            payoff = 1.0 if yes_won else 0.0
        else:
            cost   = 1.0 - yes_px_at_pred
            payoff = 1.0 if no_won else 0.0
        pnl = payoff - cost  # per $1 of notional stake

        ok = calibration.resolve_prediction_by_id(
            prediction_id = int(p["id"]),
            outcome       = 1 if claude_correct else 0,
            pnl_usd       = pnl,
            note          = f"legacy_resolution YES={yes_won} NO={no_won}",
        )
        if ok:
            result["predictions_resolved"] += 1
        else:
            result["errors"] += 1

    print(f"[pm_runner] resolve_positions: {result}", flush=True)
    return result


# ── SQL helpers ──────────────────────────────────────────────────────────────
def _fetch_open_positions(short_horizon_only: bool = False) -> list[dict]:
    try:
        with get_engine().begin() as conn:
            where = "status = 'open'"
            if short_horizon_only:
                where += " AND expected_resolution_at < NOW() + INTERVAL '24 hours'"
            rows = conn.execute(text(
                f"SELECT id, market_id, side, shares, cost_usd, prediction_id, question "
                f"FROM pm_positions WHERE {where}"
            )).fetchall()
        return [
            {
                "id":            int(r[0]),
                "market_id":     str(r[1]),
                "side":          str(r[2]),
                "shares":        float(r[3]),
                "cost_usd":      float(r[4]),
                "prediction_id": int(r[5]) if r[5] is not None else None,
                "question":      str(r[6] or ""),
            }
            for r in rows
        ]
    except Exception as exc:
        print(f"[pm_runner] _fetch_open_positions failed: {exc}", file=sys.stderr)
        return []


def _fetch_unresolved_legacy_predictions() -> list[dict]:
    """
    Predictions that don't have a pm_positions row (e.g. from the pre-analyst
    shadow era) and are still unresolved.
    """
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT p.id, p.probability, p.subject_key, p.metadata "
                "FROM predictions p "
                "LEFT JOIN pm_positions pp ON pp.prediction_id = p.id "
                "WHERE p.source = :src "
                "  AND p.resolved_at IS NULL "
                "  AND pp.id IS NULL"
            ), {"src": SOURCE}).fetchall()
        out = []
        for r in rows:
            sk = r[2] or ""
            mid = sk.split(":", 1)[1] if sk.startswith("polymarket:") else None
            if not mid:
                continue
            out.append({
                "id":          int(r[0]),
                "probability": float(r[1]),
                "market_id":   mid,
                "metadata":    r[3],
            })
        return out
    except Exception as exc:
        print(f"[pm_runner] _fetch_legacy failed: {exc}", file=sys.stderr)
        return []


def _parse_price_list(raw) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except Exception:
            return []
    try:
        return [float(x) for x in json.loads(raw)]
    except Exception:
        return []


def _parse_json(raw) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ── Back-compat shims for any callers of the old names ───────────────────────
# Kept only during the rewrite; remove once nothing imports these.
async def scrape_and_evaluate(*args, **kwargs):
    return await scan_and_analyze(*args, **kwargs)

async def resolve_pending(*args, **kwargs):
    return await resolve_positions(*args, **kwargs)


# ── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "resolve", "both"],
                    default="scan", nargs="?")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-volume", type=float, default=5_000.0)
    args = ap.parse_args()

    async def _main():
        if args.mode in ("scan", "both"):
            await scan_and_analyze(limit=args.limit,
                                    min_volume_24h=args.min_volume)
        if args.mode in ("resolve", "both"):
            await resolve_positions()

    asyncio.run(_main())
