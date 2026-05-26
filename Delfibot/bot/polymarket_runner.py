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
        # `counterfactual_backfilled` counts how many closed-early rows
        # had `counterfactual_pnl_usd` stamped this sweep. Helps the
        # exit-policy review report verify the loop is healthy.
        "counterfactual_backfilled": 0,
        "errors": 0,
    }

    open_rows   = _fetch_open_positions(short_horizon_only=short_horizon_only)
    legacy_rows = [] if short_horizon_only else _fetch_unresolved_legacy_predictions()
    # Closed-early rows whose underlying market hasn't yet been
    # backfilled with `counterfactual_pnl_usd`. The exit-policy review
    # report needs this number to ask "was the exit premature?";
    # without the backfill it stays NULL forever.
    early_rows = (
        [] if short_horizon_only
        else _fetch_closed_early_pending_counterfactual()
    )

    market_ids = list({p["market_id"] for p in open_rows if p.get("market_id")}
                      | {p["market_id"] for p in legacy_rows if p.get("market_id")}
                      | {p["market_id"] for p in early_rows if p.get("market_id")})
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
                    # Force-refresh the Polymarket /positions cache
                    # before fetching stats so the just-settled
                    # position is OUT of locked_capital. Without
                    # this, get_portfolio_stats() returns a stale
                    # locked sum that still includes this position's
                    # currentValue while bankroll ALREADY reflects
                    # the redeem proceeds via the wallet probe -
                    # the position gets double-counted in equity.
                    # User screenshot 2026-05-25: Israel WIN showed
                    # equity $51.89 = bankroll $13.72 + locked
                    # $38.17, but actual equity was ~$38.71;
                    # Israel was double-counted ($13.18 in both).
                    # This is a scheduled job, not an HTTP request
                    # path, so a synchronous cache refresh here is
                    # safe (the GIL-wedge concern from a8e9625's
                    # revert applies only to /api/* handlers).
                    try:
                        from feeds.polymarket_wallet import (
                            _refresh_positions_cache,
                            _refresh_closed_realized_cache,
                            get_poly_signer_info,
                            _POSITIONS_VALUE_LOCK,
                        )
                        from engine.user_config import (
                            get_active_polymarket_creds,
                            get_user_config as _gcfg,
                        )
                        _cfg = _gcfg(user_id)
                        _creds = get_active_polymarket_creds(_cfg)
                        _pk = (_creds or {}).get("private_key")
                        if _pk:
                            _info = get_poly_signer_info(_pk)
                            _funder = (_info or {}).get("funder")
                            if _funder:
                                _acq = _POSITIONS_VALUE_LOCK.acquire(
                                    blocking=False,
                                )
                                if _acq:
                                    try:
                                        _refresh_positions_cache(_funder)
                                    finally:
                                        _POSITIONS_VALUE_LOCK.release()
                                # The closed-positions cache also
                                # needs a refresh so realized_pnl
                                # picks up this just-redeemed winner.
                                try:
                                    _refresh_closed_realized_cache(_funder)
                                except Exception:
                                    pass
                    except Exception as exc:
                        print(
                            f"[pm_runner] settle-time cache refresh "
                            f"failed for position #{p.get('id')}: "
                            f"{type(exc).__name__}: {exc}",
                            file=sys.stderr, flush=True,
                        )
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
                    # NOTE: previous revision (a8e9625) called
                    # force_refresh_all_polymarket_caches here to
                    # ensure the settled_win/loss Telegram message
                    # read post-redeem wallet truth. Reverted
                    # 2026-05-23 because the synchronous HTTPS calls
                    # (wallet probe + 2 data-api endpoints) wedged
                    # the daemon under load — same GIL-contention
                    # pattern as the earlier user-pnl-in-request-path
                    # bugs. The pending_payout SQL narrowing in
                    # a718bc3 fixes the root cause of the inflated-
                    # Balance bug (stale 3-day-old invalid-market
                    # rows were the actual $10.88 culprit); cache
                    # freshness is handled by the 60s scheduler
                    # refresh, which is sufficient for the
                    # settled_win/loss message use case.
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
                    locked_capital = float(
                        stats.get("locked_capital", stats.get("open_cost", 0.0))
                        or 0.0
                    )
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
                        locked_capital=locked_capital,
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

    # ── Phase C - counterfactual P&L backfill for closed-early rows ─────
    # For every closed-early row whose market just reached natural
    # resolution, write `counterfactual_pnl_usd` = (hold P&L) - (exit P&L).
    # Lets the review report ask: was the exit wise? Positive = the bot
    # left money on the table; negative = the bot dodged a loss.
    for p in early_rows:
        raw = rows.get(p["market_id"])
        if not raw or not raw.get("closed", False):
            continue
        prices = _parse_price_list(raw.get("outcomePrices"))
        if len(prices) != 2:
            continue
        yes_won = prices[0] >= 0.99
        no_won  = prices[1] >= 0.99
        if not (yes_won or no_won):
            yp, np_ = prices[0], prices[1]
            both_mid = 0.40 <= yp <= 0.60 and 0.40 <= np_ <= 0.60
            if not both_mid:
                continue
            outcome = "INVALID"
            sp: float = 0.5
        elif yes_won:
            outcome, sp = "YES", 1.0
        else:
            outcome, sp = "NO", 1.0

        user_id = p.get("user_id")
        if not user_id:
            continue
        executor = _executor_for(user_id)
        if executor is None:
            continue
        ok = executor.backfill_counterfactual_pnl(
            position_id=int(p["id"]),
            winning_outcome=outcome,
            settlement_price=sp,
        )
        if ok:
            result["counterfactual_backfilled"] += 1

    print(f"[pm_runner] resolve_positions: {result}", flush=True)
    return result


# ── Evaluate open positions for early exit (TP / SL / time-decay) ──────────
async def evaluate_open_positions() -> dict:
    """
    Walk every open pm_positions row, fetch the market's current
    state from gamma, ask `execution.position_exit.evaluate_exit` if
    the user's policy says to close, and call PMExecutor.close_position_early
    if so. Cheap-enough to run on a 60-second cadence: a single
    `PolymarketFeed.fetch_many(market_ids)` round-trip per cycle plus
    one DB read per user_config snapshot.
    """
    from execution.position_exit import evaluate_exit
    from engine.user_config import get_user_config

    result = {
        "positions_checked": 0,
        "policy_skipped":    0,  # user has exit_policy_enabled=False
        "evaluated":         0,
        "closed_early":      0,
        "errors":            0,
    }

    open_rows = _fetch_open_positions_with_prices()
    if not open_rows:
        return result

    market_ids = list({r["market_id"] for r in open_rows if r.get("market_id")})
    if not market_ids:
        return result

    try:
        async with PolymarketFeed() as feed:
            gamma_rows = await feed.fetch_many(market_ids)
    except Exception as exc:
        print(f"[evaluate_exit] gamma fetch failed: {exc}", file=sys.stderr)
        result["errors"] += 1
        return result

    # Per-user config + executor cache. user_config is the source of
    # truth for the exit-policy toggles and thresholds; never assume
    # globals.
    cfg_cache: dict[str, object] = {}
    exe_cache: dict[str, PMExecutor] = {}

    for p in open_rows:
        result["positions_checked"] += 1
        user_id   = p.get("user_id")
        market_id = p.get("market_id")
        if not user_id or not market_id:
            continue
        raw = gamma_rows.get(market_id)
        if not raw:
            continue
        # Skip already-closed markets — natural-resolution path handles them.
        if raw.get("closed", False):
            continue
        # Use the position's own SIDE bid for the exit decision. We
        # approximate "current bid" with gamma's `outcomePrices[side]`,
        # which is the midpoint of the orderbook — a reasonable proxy
        # for a market that's liquid enough to have entered. If we
        # later want true best-bid we can swap to a CLOB
        # get_price(side=SELL) call here, but at 60s cadence the gamma
        # midpoint is cheap and correctness-equivalent for TP/SL
        # decisions.
        prices = _parse_price_list(raw.get("outcomePrices"))
        if len(prices) != 2:
            continue
        side = p.get("side") or ""
        if side == "YES":
            current_bid = prices[0]
        elif side == "NO":
            current_bid = prices[1]
        else:
            continue

        # Mark-to-market: write the position's current market value to
        # pm_positions.current_value_usd so pm_executor.get_portfolio_stats
        # can report market value in "Locked Capital" / "Total Equity"
        # instead of cost basis. value = shares * current_bid using the
        # same outcomePrices midpoint we already parsed. Fires every 60s
        # for every open position, regardless of whether the user has
        # exit-policy enabled (the rest of this loop short-circuits when
        # the policy is off, but the mark-to-market is universal). Cheap
        # single-row UPDATE per position.
        try:
            _shares = float(p.get("shares") or 0.0)
            if _shares > 0.0:
                _value_usd = float(current_bid) * _shares
                # Same UPDATE also records the running max / min mid-
                # price on the bought side. The learning system reads
                # these to backtest threshold sweeps without needing
                # a separate price-history table - "what would the
                # take-profit threshold of X% have done on this row"
                # only needs to know the price extreme each position
                # actually reached. CASE expressions keep this a
                # single row touch per 60s tick.
                _bid_f = float(current_bid)
                with get_engine().begin() as _conn:
                    _conn.execute(text(
                        "UPDATE pm_positions "
                        "SET current_value_usd = :v, "
                        "    max_price_seen = CASE "
                        "      WHEN max_price_seen IS NULL OR :bid > max_price_seen "
                        "        THEN :bid ELSE max_price_seen "
                        "    END, "
                        "    min_price_seen = CASE "
                        "      WHEN min_price_seen IS NULL OR :bid < min_price_seen "
                        "        THEN :bid ELSE min_price_seen "
                        "    END "
                        "WHERE id = :pid"
                    ), {"v": _value_usd, "bid": _bid_f, "pid": p["id"]})
        except Exception as _exc:
            print(
                f"[evaluate_exit] current_value_usd write failed for "
                f"#{p.get('id')}: {_exc}",
                file=sys.stderr, flush=True,
            )

        # Per-user config + executor: snapshot once per user.
        if user_id not in cfg_cache:
            try:
                cfg_cache[user_id] = get_user_config(user_id)
            except Exception as exc:
                print(f"[evaluate_exit] user_config fetch failed for "
                      f"{user_id}: {exc}", file=sys.stderr)
                result["errors"] += 1
                continue
        cfg = cfg_cache[user_id]
        # User has the master switch off — count and skip.
        if not getattr(cfg, "exit_policy_enabled", False):
            result["policy_skipped"] += 1
            continue

        # Resolution-time estimate, used for both safety floor and the
        # stop-loss min-time-remaining gate inside evaluate_exit.
        expected_resolution_at = extract_resolution_estimate(raw)

        result["evaluated"] += 1
        try:
            decision = evaluate_exit(
                position={
                    "entry_price": p.get("entry_price"),
                    "created_at":  p.get("created_at"),
                    "status":      "open",
                },
                current_bid=float(current_bid) if current_bid is not None else None,
                user_config=cfg,
                expected_resolution_at=expected_resolution_at,
            )
        except Exception as exc:
            print(f"[evaluate_exit] decision engine failed for pos "
                  f"#{p['id']}: {exc}", file=sys.stderr)
            result["errors"] += 1
            continue
        if not decision.should_exit:
            continue

        # Find the side's clob_token_id for the SELL leg in live mode.
        clob_token_id: str | None = None
        token_ids = _parse_token_id_list(raw.get("clobTokenIds"))
        if len(token_ids) == 2:
            clob_token_id = token_ids[0] if side == "YES" else token_ids[1]

        if user_id not in exe_cache:
            try:
                # IMPORTANT: per-row mode override so the close lands
                # on the same ledger the position was opened against.
                exe_cache[user_id] = PMExecutor(
                    user_id, view_mode_override=p.get("mode"),
                )
            except Exception as exc:
                print(f"[evaluate_exit] PMExecutor init failed for user "
                      f"{user_id}: {exc}", file=sys.stderr)
                result["errors"] += 1
                continue
        executor = exe_cache[user_id]

        ok = executor.close_position_early(
            position_id=int(p["id"]),
            reason=decision.reason or "take_profit",
            details=decision.details,
            current_bid=float(current_bid),
            clob_token_id=clob_token_id,
        )
        if ok:
            result["closed_early"] += 1
        else:
            result["errors"] += 1

    print(f"[pm_runner] evaluate_open_positions: {result}", flush=True)
    return result


def _parse_token_id_list(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _fetch_open_positions_with_prices() -> list[dict]:
    """Rows for the exit-policy job: includes entry_price + created_at
    so the decision engine can compute return and time-open. Mirrors
    `_fetch_open_positions` but with the extra columns and no
    short-horizon filter — the exit policy must see every open position,
    not just the ones about to resolve."""
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT id, user_id, mode, market_id, side, shares, "
                "       entry_price, cost_usd, created_at, question "
                "  FROM pm_positions "
                " WHERE status = 'open'"
            )).fetchall()
        return [
            {
                "id":          int(r[0]),
                "user_id":     str(r[1]) if r[1] is not None else None,
                "mode":        str(r[2]) if r[2] is not None else None,
                "market_id":   str(r[3]) if r[3] is not None else None,
                "side":        str(r[4]) if r[4] is not None else None,
                "shares":      float(r[5]) if r[5] is not None else 0.0,
                "entry_price": float(r[6]) if r[6] is not None else 0.0,
                "cost_usd":    float(r[7]) if r[7] is not None else 0.0,
                "created_at":  r[8],
                "question":    str(r[9]) if r[9] is not None else "",
            }
            for r in rows
        ]
    except Exception as exc:
        print(f"[evaluate_exit] _fetch_open_positions_with_prices failed: "
              f"{exc}", file=sys.stderr)
        return []


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


def _fetch_closed_early_pending_counterfactual() -> list[dict]:
    """Rows for the Phase C backfill: every closed-early position
    whose `counterfactual_pnl_usd` is still NULL. Bounded by 90 days
    to keep the per-sweep cost stable — older rows are unlikely to
    ever resolve via the gamma feed and aren't useful to the review
    report.
    """
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT id, market_id, user_id "
                "  FROM pm_positions "
                " WHERE status = 'closed_early' "
                "   AND counterfactual_pnl_usd IS NULL "
                "   AND closed_at >= datetime('now', '-90 days')"
            )).fetchall()
        return [
            {
                "id":        int(r[0]),
                "market_id": str(r[1]) if r[1] is not None else None,
                "user_id":   str(r[2]) if r[2] is not None else None,
            }
            for r in rows
        ]
    except Exception as exc:
        print(f"[pm_runner] _fetch_closed_early_pending_counterfactual "
              f"failed: {exc}", file=sys.stderr)
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
