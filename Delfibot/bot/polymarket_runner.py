"""
Polymarket runner - the two scheduled entrypoints wired into main.py.

scan_and_analyze(limit):
    Fetch candidate markets → research → Claude evaluation → sizing →
    open simulation/live position. All gated by the PMAnalyst pipeline.

resolve_positions():
    For every open pm_positions row, check whether the underlying Polymarket
    market has resolved. If yes, settle via PMExecutor (writes settlement
    price, realized P&L, and feeds the calibration ledger). Also opportunistically
    backfills legacy simulation-mode predictions that lack a pm_positions row.

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
from feeds.polymarket_feed import PolymarketFeed, extract_resolution_estimate


SOURCE = "polymarket"



_scan_lock = asyncio.Lock()


# ── Scan + analyse (primary entrypoint) ──────────────────────────────────────
async def scan_and_analyze(
    limit:          int   = 20,
    min_volume_24h: float = 5_000.0,
    analyst:        PMAnalyst | None = None,
) -> dict:
    """
    Delegate to PMAnalyst. Returns the analyst's summary dict.
    Uses a module-level lock to prevent overlapping scans from the
    scheduler and the dashboard's manual "scan now" button.
    """
    if _scan_lock.locked():
        print("[pm_runner] scan already in progress, skipping", flush=True)
        return {"skipped": True, "reason": "scan already in progress"}
    async with _scan_lock:
        if analyst is None:
            analyst = PMAnalyst()
        return await analyst.scan_and_analyze(limit=limit,
                                                min_volume_24h=min_volume_24h)


# ── Resolve positions + legacy predictions ──────────────────────────────────
async def resolve_positions(short_horizon_only: bool = False) -> dict:
    """
    Two-phase settlement:

        Phase A - for each onboarded user, settle every open pm_positions row
                  against the resolved Polymarket market state using a
                  PMExecutor bound to that user.
        Phase B - resolve legacy `predictions` rows from the simulation-only
                  era that lack a pm_positions partner.

    Returns: {"positions_checked", "positions_settled",
              "predictions_checked", "predictions_resolved", "errors"}
    """
    result = {
        "positions_checked": 0, "positions_settled": 0,
        # `positions_invalid` is a legitimate resolution outcome (the
        # market itself resolved as 50/50 / void), counted separately
        # so it doesn't pollute the `errors` bucket the way it used to.
        "positions_invalid": 0,
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

    # Per-user executor cache - one PMExecutor per distinct user_id.
    executors: dict[str, PMExecutor] = {}

    def _executor_for(user_id: str) -> PMExecutor | None:
        if user_id in executors:
            return executors[user_id]
        try:
            ex = PMExecutor(user_id)
        except Exception as exc:
            print(f"[resolve] PMExecutor init failed for user={user_id}: {exc}",
                  file=sys.stderr)
            return None
        executors[user_id] = ex
        return ex

    # Phase A - pm_positions (fan out by row.user_id).
    refresh_pairs: list[tuple[int, datetime]] = []
    for p in open_rows:
        result["positions_checked"] += 1
        raw = rows.get(p["market_id"])
        if not raw:
            continue
        # Refresh the dashboard's resolution-time estimate before
        # deciding whether to settle. `endDate` on Polymarket is a
        # trading-window close, often days off the actual deadline,
        # and Polymarket revises it as events get clearer. Without
        # this refresh the dashboard shows stale countdowns ("6d"
        # on a market that resolves today). The settler is the only
        # place that already fetches raw rows for every open
        # position, so piggy-back the update here - no extra API
        # cost.
        new_resolution_at = extract_resolution_estimate(raw)
        if new_resolution_at is not None:
            refresh_pairs.append((int(p["id"]), new_resolution_at))
        if not raw.get("closed", False):
            continue
        prices = _parse_price_list(raw.get("outcomePrices"))
        if len(prices) != 2:
            continue

        user_id = p.get("user_id")
        if not user_id:
            print(f"[resolve] skipping position #{p['id']} - no user_id",
                  file=sys.stderr)
            result["errors"] += 1
            continue
        executor = _executor_for(user_id)
        if executor is None:
            result["errors"] += 1
            continue

        yes_won = prices[0] >= 0.99
        no_won  = prices[1] >= 0.99
        if not (yes_won or no_won):
            # `closed=true` is the primary finality gate, but we've
            # observed Polymarket flag `closed=true` briefly with
            # prices like 0.97 / 0.03 (still in the rounding tail to
            # 1.0/0.0). Only treat as genuinely INVALID when prices
            # are clearly mid-range (both within [0.4, 0.6]) - that's
            # the real 50/50 refund signature. Polarised-but-not-yet-
            # 0.99 prices mean the market is still finalising; skip
            # the row and retry on the next tick.
            yp, np_ = prices[0], prices[1]
            both_mid = 0.40 <= yp <= 0.60 and 0.40 <= np_ <= 0.60
            if not both_mid:
                print(f"[resolve] pos #{p['id']} market={p['market_id']} "
                      f"closed but prices not finalised "
                      f"({yp:.2f}/{np_:.2f}); will retry next tick",
                      flush=True)
                continue
            print(f"[resolve] INVALID settlement for pos #{p['id']} "
                  f"market={p['market_id']} prices={prices} "
                  f"(both in [0.4, 0.6])", flush=True)
            executor.settle_position(p["id"], "INVALID", 0.5)
            # INVALID is a real resolution outcome, not an error. Log
            # to the event feed so the dashboard reflects what
            # happened, and count separately so /api/scheduler stats
            # don't flag the cycle as failing.
            result["positions_invalid"] += 1
            try:
                from db.logger import log_event
                log_event(
                    event_type="position_invalid",
                    severity=20,
                    description=(
                        f"Position #{p['id']} resolved INVALID "
                        f"({(p.get('question') or '')[:120]}). "
                        f"Stake refunded; no P&L."
                    ),
                    source="polymarket_runner",
                )
            except Exception as exc:
                print(f"[resolve] log_event invalid failed: {exc}",
                      file=sys.stderr)
            continue
        outcome = "YES" if yes_won else "NO"
        if executor.settle_position(p["id"], outcome):
            result["positions_settled"] += 1
            try:
                from db.logger import log_event
                side = p.get("side", "?")
                # Read DB-truth `realized_pnl_usd` instead of
                # recomputing with `(1.0 if outcome==side else 0.0) *
                # shares - cost`. The recompute assumes settlement
                # price is exactly 1.0, which IS true today but
                # silently drifts the moment `settle_position` ever
                # uses a non-1.0 price (e.g. partial fills, future
                # variant). Source-of-truth is the row.
                pnl = _read_realized_pnl(p["id"])
                if pnl is None:
                    pnl = (1.0 if outcome == side else 0.0) * p["shares"] - p["cost_usd"]
                description = (
                    f"Settled {side} on {(p.get('question') or '')[:120]}: "
                    f"outcome {outcome}, P&L ${pnl:+.2f}, "
                    f"cost ${p['cost_usd']:.2f}, "
                    f"mode {p.get('mode') or 'simulation'}, "
                    f"position={p['id']}"
                )
                # Telegram rendering follows the SaaS Messages Spec v1.
                # tm.settled_win / tm.settled_loss differ in glyph and
                # P&L line wording, picked off the win/loss boolean.
                telegram_html: str | None = None
                try:
                    from feeds import telegram_messages as _tm
                    # Pull both values from the same stats snapshot so
                    # they agree. bankroll = cash available to deploy;
                    # equity = cash + cost-basis of all open positions.
                    # Earlier code passed `equity = bankroll`, which only
                    # holds when the user has zero other open positions
                    # at settlement time. In practice the bot usually
                    # holds several at once, so the two numbers were
                    # silently identical in the Telegram message.
                    # Mode-scoped stats for the settlement notification.
                    # The primary `executor` is bound to the user's current
                    # trading mode (typically 'live'), but THIS position
                    # might have been recorded under a different mode
                    # (e.g. simulation fallback when V2 signer mismatch
                    # gated live orders). Showing live-mode bankroll on a
                    # sim settlement gave the wrong "$1000" balance on
                    # Telegram win/loss messages. Build a view-mode-
                    # overridden executor when the position's mode differs
                    # so the message reflects the ledger that actually
                    # changed. Cached per (user, mode) so repeated
                    # settlements in the same scan are cheap.
                    position_mode = p.get("mode") or "simulation"
                    if position_mode == executor.mode:
                        stats_executor = executor
                    else:
                        cache_key = f"{user_id}:{position_mode}"
                        cached = executors.get(cache_key)
                        if cached is None:
                            try:
                                cached = PMExecutor(
                                    user_id,
                                    view_mode_override=position_mode,
                                )
                                executors[cache_key] = cached
                            except Exception:
                                cached = executor  # fall back to primary
                        stats_executor = cached
                    stats = stats_executor.get_portfolio_stats()
                    bankroll_after = float(stats.get("bankroll", 0.0))
                    equity_after   = float(stats.get("equity",   bankroll_after))
                    cost = float(p.get("cost_usd", 0.0) or 0.0)
                    roi = (pnl / cost) if cost > 0 else 0.0
                    common = dict(
                        question=(p.get("question") or ""),
                        side=side,
                        outcome=outcome,
                        pnl=pnl,
                        roi=roi,
                        bankroll=bankroll_after,
                        equity=equity_after,
                        mode=p.get("mode") or "simulation",
                    )
                    if outcome == side:
                        telegram_html = _tm.settled_win(**common)
                    else:
                        telegram_html = _tm.settled_loss(**common)
                except Exception as exc:
                    print(f"[pm_runner] telegram render failed: {exc}",
                          file=sys.stderr)
                log_event(
                    event_type="position_settled",
                    severity=20,
                    description=description,
                    source="polymarket_runner",
                    telegram_html=telegram_html,
                )
            except Exception as exc:
                print(f"[pm_runner] event log write failed: {exc}",
                      file=sys.stderr)
        else:
            result["errors"] += 1

    # Flush any expected_resolution_at refreshes accumulated above.
    # Done once at the end of Phase A so we issue a single round-trip
    # per sweep instead of one UPDATE per position. Only updates rows
    # whose value actually changed by more than 1 minute, to avoid
    # rewriting timestamps every 15 minutes for stable markets.
    if refresh_pairs:
        try:
            with get_engine().begin() as conn:
                for pos_id, new_dt in refresh_pairs:
                    conn.execute(text(
                        "UPDATE pm_positions "
                        "SET expected_resolution_at = :new_dt "
                        "WHERE id = :id "
                        "  AND status = 'open' "
                        "  AND ("
                        "    expected_resolution_at IS NULL OR "
                        "    ABS((julianday(expected_resolution_at) - julianday(:new_dt)) * 86400.0) > 60"
                        "  )"
                    ), {"id": pos_id, "new_dt": new_dt})
        except Exception as exc:
            print(f"[resolve] expected_resolution_at refresh failed: {exc}",
                  file=sys.stderr)

    # Phase B - legacy predictions without a pm_positions row.
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


async def resolve_skipped_evaluations(batch_size: int = 200) -> dict:
    """Back-fill `settlement_outcome` on closed-market evaluations.

    The forecaster writes a row to `market_evaluations` for every
    market it analyzes — including ones it decides to skip (most
    of the scan). Skipped rows never open a pm_position, so the
    main resolve loop ignores them.

    But the user explicitly wants to see "would Delfi have won if
    it hadn't skipped?" — both as a transparency feature and as a
    feedback signal for future archetype tuning. So this job
    fetches recent unresolved skipped evaluations, queries gamma
    for the market's current state, and writes the outcome onto
    the row when the market has closed.

    Returns: {"evaluations_checked", "evaluations_resolved",
              "evaluations_invalid", "errors"}.
    """
    result = {
        "evaluations_checked":  0,
        "evaluations_resolved": 0,
        "evaluations_invalid":  0,
        "errors":               0,
    }

    try:
        with get_engine().begin() as conn:
            # Skip evals = the recommendation is NOT one of the four
            # trade verbs the bot uses when it actually wants to open
            # a position. Anything else (SKIP, NO_TRADE, blank, ...)
            # counts as a skip. settlement_outcome IS NULL means we
            # haven't resolved it yet. Anchor on evaluated_at so we
            # don't keep retrying very old rows where gamma might no
            # longer surface the market.
            rows = conn.execute(text(
                "SELECT id, market_id "
                "  FROM market_evaluations "
                " WHERE settlement_outcome IS NULL "
                "   AND COALESCE(UPPER(recommendation), '') "
                "       NOT IN ('BUY_YES', 'YES', 'BUY_NO', 'NO') "
                "   AND market_id IS NOT NULL "
                "   AND evaluated_at >= datetime('now', '-30 days') "
                " ORDER BY evaluated_at DESC "
                " LIMIT :lim"
            ), {"lim": int(batch_size)}).fetchall()
    except Exception as exc:
        print(f"[resolve_skipped] DB read failed: {exc}", file=sys.stderr)
        result["errors"] += 1
        return result

    if not rows:
        return result

    market_ids = sorted({str(r[1]) for r in rows if r[1] is not None})
    try:
        async with PolymarketFeed() as feed:
            gamma_rows = await feed.fetch_many(market_ids)
    except Exception as exc:
        print(f"[resolve_skipped] fetch_many failed: {exc}", file=sys.stderr)
        result["errors"] += 1
        return result

    # Build (eval_id, outcome) pairs to batch-write back.
    updates: list[tuple[int, str]] = []
    for r in rows:
        result["evaluations_checked"] += 1
        eval_id   = int(r[0])
        market_id = str(r[1])
        raw = gamma_rows.get(market_id)
        if not raw or not raw.get("closed", False):
            continue
        prices = _parse_price_list(raw.get("outcomePrices"))
        if len(prices) != 2:
            continue
        yes_won = prices[0] >= 0.99
        no_won  = prices[1] >= 0.99
        if yes_won:
            outcome = "YES"
        elif no_won:
            outcome = "NO"
        else:
            # Resolved but neither side hit 0.99 - treat as invalid
            # (50/50 / void resolution).
            outcome = "INVALID"
        updates.append((eval_id, outcome))

    if not updates:
        return result

    try:
        with get_engine().begin() as conn:
            for eval_id, outcome in updates:
                conn.execute(text(
                    "UPDATE market_evaluations "
                    "   SET settlement_outcome = :o "
                    " WHERE id = :id"
                ), {"o": outcome, "id": eval_id})
    except Exception as exc:
        print(f"[resolve_skipped] DB write failed: {exc}", file=sys.stderr)
        result["errors"] += 1
        return result

    for _, outcome in updates:
        if outcome == "INVALID":
            result["evaluations_invalid"] += 1
        else:
            result["evaluations_resolved"] += 1

    print(f"[pm_runner] resolve_skipped_evaluations: {result}", flush=True)
    return result


# ── SQL helpers ──────────────────────────────────────────────────────────────
def _read_realized_pnl(position_id: int) -> float | None:
    """Read the persisted `realized_pnl_usd` after settlement.

    Used by the resolve-loop to surface the same number the DB
    actually stores in the dashboard event_log + Telegram message,
    rather than recomputing from a 1.0/0.0 settlement-price
    assumption that could drift if `settle_position`'s pricing
    contract ever changes.
    """
    try:
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT realized_pnl_usd FROM pm_positions WHERE id = :pid"
            ), {"pid": position_id}).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:
        print(f"[resolve] _read_realized_pnl({position_id}) failed: {exc}",
              file=sys.stderr)
        return None


def _fetch_open_positions(short_horizon_only: bool = False) -> list[dict]:
    try:
        with get_engine().begin() as conn:
            where = "status = 'open'"
            if short_horizon_only:
                where += " AND expected_resolution_at < datetime('now', '+24 hours')"
            rows = conn.execute(text(
                f"SELECT id, market_id, side, shares, cost_usd, prediction_id, "
                f"       question, user_id, mode "
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
                "user_id":       str(r[7]) if r[7] is not None else None,
                "mode":          str(r[8]) if r[8] is not None else None,
            }
            for r in rows
        ]
    except Exception as exc:
        print(f"[pm_runner] _fetch_open_positions failed: {exc}", file=sys.stderr)
        return []


def _fetch_unresolved_legacy_predictions() -> list[dict]:
    """
    Predictions that don't have a pm_positions row (e.g. from the pre-analyst
    simulation era) and are still unresolved.
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
