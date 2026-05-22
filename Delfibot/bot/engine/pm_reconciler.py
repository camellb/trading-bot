"""
Polymarket position reconciliation.

Why this exists: the bot's `_open_live` flow used to be the ONLY path
that wrote rows into `pm_positions`. When an order was placed,
`_poll_order_filled` waited for fill confirmation; if the poll timed
out before the fill landed, the bot logged "order rejected", attempted
a cancel, and forgot about the order. But the order often filled AFTER
the poll timeout - the CLOB matcher just hadn't broadcast the fill in
the bot's poll window. The position then existed on-chain but had no
counterpart in Delfi's DB. The user saw fewer positions in Delfi than
on Polymarket, and worse, the bot would happily open duplicate
positions on the same market because its own DB said it had no
exposure there.

This module is the safety net: every N minutes, pull every position
on-chain from Polymarket's data-api, look up each one in
`pm_positions`, and INSERT any that are missing using the on-chain
truth (size, avg fill price, condition id, slug). Settled-but-unsynced
positions get marked `status='settled'` so the closed-positions tab
catches up too.

The reconciler is conservative: it only ADDS missing rows. It never
deletes or modifies an existing pm_positions row (drift between the DB
and on-chain on EXISTING rows is a separate problem class - usually
fill-price mismatches from the limit-vs-fill bug - and silently
overwriting would lose audit trail). If you need to fix an existing
row, do it manually with sqlite3.

Public surface:
  reconcile_positions(user_id) -> dict
    Run one reconciliation pass for `user_id`. Returns a summary dict:
    {
      "checked":   int,  # positions seen on-chain
      "imported":  int,  # newly inserted into pm_positions
      "already":   int,  # already tracked, skipped
      "errors":    int,  # exceptions during import (logged to stderr)
    }
"""

from __future__ import annotations

import sys
from typing import Optional

import requests
from sqlalchemy import text

from db.engine import get_engine
from engine.user_config import (
    DEFAULT_USER_ID,
    get_active_polymarket_creds,
    get_user_config,
)
from feeds.polymarket_wallet import get_poly_signer_info


# Data-api endpoint used by the official Polymarket frontend. Returns
# every CTF position the user holds with size > sizeThreshold. We use a
# tiny threshold so even dust (e.g. 2-share leftover from an old trade
# that resolved to zero) is surfaced - those still need a status flag
# in Delfi to land on the Closed positions tab.
_DATA_API_POSITIONS = "https://data-api.polymarket.com/positions"
# Activity feed exposes per-trade `usdcSize` - the actual USDC sent
# on-chain INCLUDING taker fees. We aggregate this per
# (conditionId, outcomeIndex) to compute the true cost of every
# position, instead of relying on /positions.initialValue which is
# `size * avgPrice` and strips the fee. The two differ by ~1-2% per
# trade and compound into realized-P&L drift against Polymarket's own
# numbers.
_DATA_API_ACTIVITY  = "https://data-api.polymarket.com/activity"
_DATA_API_TIMEOUT_S = 15.0
_HTTP_HEADERS = {"User-Agent": "delfibot/1.0 reconciler"}

# An open order with zero matched-size that has sat on the CLOB book
# longer than this gets cancelled by the reconciler. The bot places
# marketable BUYs at the ask; if Polymarket's matcher hasn't paired it
# within an hour, the limit was wrong, the market moved, or the order
# is otherwise abandoned debris from a poll-timeout in _open_live.
# Conservative: 1h is much longer than the 30-90s real fill window so
# we never cancel an order that's about to land.
_STALE_ORDER_AGE_S = 60 * 60

# Drift tolerance: the reconciler logs a warning when an existing
# pm_positions row's `shares` or `cost_usd` diverges from on-chain by
# more than this fraction. Values inside the tolerance can drift
# legitimately (early-exit partials, fees, MTM noise) and aren't a
# bug. Outside the tolerance is almost always the limit-vs-fill price
# bug or a missed close - both worth surfacing to the user.
_DRIFT_TOLERANCE = 0.10

# Track conditions for which we've already fired a "multi-outcome
# skipped" alert. Without this, the same untrackable position would
# trigger a Telegram ping on every 2-minute reconciler tick. Resets
# when the daemon restarts (intentional: a restart means the user
# saw nothing in stderr for a while and we want to re-flag).
_MULTI_OUTCOME_ALERTED: set[str] = set()


def reconcile_positions(user_id: str = DEFAULT_USER_ID) -> dict:
    """Pull on-chain positions, import any missing ones, cancel stale
    unfilled orders.

    Args:
        user_id: which Delfi user to reconcile against. Single-user
            install (`'local'`) is the only real caller today, but the
            arg matches every other engine function so future
            multi-tenant work doesn't have to retrofit.

    Returns:
        Summary dict (see module docstring) extended with two
        open-order counters:
          "orders_seen":      int  # CLOB open orders for this account
          "orders_cancelled": int  # zero-fill orders we cancelled
    """
    summary = {
        "checked":          0,
        "imported":         0,
        "already":          0,
        "errors":           0,
        "orders_seen":      0,
        "orders_cancelled": 0,
    }

    # Resolve the funder address. The PM private key signs orders, but
    # POSITIONS live on the funder (the EOA for sig_type=0, the proxy
    # for sig_type=1/2). get_poly_signer_info encapsulates that probe;
    # we always query positions against `funder`, never against the
    # signing-key's bare EOA.
    cfg = get_user_config(user_id)
    creds = get_active_polymarket_creds(cfg)
    pk = creds.get("private_key")
    if not pk:
        print("[pm_reconciler] no Polymarket private key configured - "
              "skipping reconciliation", flush=True)
        return summary

    info = get_poly_signer_info(pk)
    if not info or not info.get("funder"):
        print("[pm_reconciler] could not derive funder address - "
              "skipping reconciliation", flush=True)
        summary["errors"] += 1
        return summary
    funder = info["funder"]

    # Fetch on-chain positions. data-api occasionally 429s under load;
    # treat any non-2xx as a transient error and try again next tick.
    try:
        resp = requests.get(
            _DATA_API_POSITIONS,
            params={
                "user": funder,
                "sizeThreshold": "0.01",  # surface dust too
                "limit": "200",
            },
            headers=_HTTP_HEADERS,
            timeout=_DATA_API_TIMEOUT_S,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        print(f"[pm_reconciler] data-api fetch failed: "
              f"{type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        summary["errors"] += 1
        return summary

    if not isinstance(rows, list):
        print(f"[pm_reconciler] unexpected data-api shape: "
              f"{type(rows).__name__}",
              file=sys.stderr, flush=True)
        summary["errors"] += 1
        return summary

    summary["checked"] = len(rows)

    # Fetch per-trade USDC totals once per pass - same source the
    # executor uses (client.get_trades). Lets _import_position write
    # the fee-inclusive cost instead of /positions.initialValue
    # which strips the fee.
    activity_costs = _fetch_activity_costs(funder)

    # Look up every existing (condition_id, side) pair in pm_positions
    # in one query so the per-row check is in-memory. Keyed by
    # (condition_id_lower, side); value carries the existing
    # shares/cost so we can detect drift below without a second query.
    existing: dict[tuple[str, str], dict] = {}
    with get_engine().connect() as conn:
        cur = conn.execute(text(
            "SELECT id, condition_id, side, shares, cost_usd, status "
            "FROM pm_positions "
            "WHERE condition_id IS NOT NULL AND user_id = :uid"
        ), {"uid": user_id})
        for pid, cond, side, shares, cost, status in cur.fetchall():
            if cond and side:
                existing[(cond.lower(), side.upper())] = {
                    "id":     pid,
                    "shares": float(shares or 0.0),
                    "cost":   float(cost or 0.0),
                    "status": (status or "open").lower(),
                }

    for r in rows:
        try:
            cond_id = (r.get("conditionId") or "").lower()
            if not cond_id:
                continue
            side = _outcome_to_side(r)
            if side is None:
                # Negative-risk multi-outcome market. Delfi's
                # pm_positions.side is CHAR(3) YES/NO and we don't
                # have a clean mapping for 3+ outcome markets yet.
                # Log to stderr AND fire a one-shot event_log entry
                # per cond so the user can spot any exposure that
                # Delfi can't track.
                print(f"[pm_reconciler] skipping unmappable outcome "
                      f"for cond={cond_id} outcome="
                      f"{r.get('outcome')!r} idx={r.get('outcomeIndex')!r}",
                      flush=True)
                _alert_multi_outcome_skip(row=r)
                continue
            if (cond_id, side) in existing:
                summary["already"] += 1
                _check_drift(existing[(cond_id, side)], r, side)
                continue

            _import_position(
                user_id=user_id, row=r, side=side,
                activity_costs=activity_costs,
            )
            summary["imported"] += 1
            existing[(cond_id, side)] = {
                "id":     None, "shares": float(r.get("size") or 0.0),
                "cost":   float(r.get("initialValue") or 0.0),
                "status": "open",
            }
            _alert_import(row=r, side=side)
            print(f"[pm_reconciler] imported on-chain position "
                  f"cond={cond_id[:10]}... side={side} "
                  f"size={r.get('size')} title={(r.get('title') or '')[:60]!r}",
                  flush=True)
        except Exception as exc:
            print(f"[pm_reconciler] import failed for "
                  f"cond={r.get('conditionId')!r}: "
                  f"{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            summary["errors"] += 1

    if summary["imported"] > 0:
        print(f"[pm_reconciler] backfilled {summary['imported']} "
              f"on-chain position(s) into pm_positions "
              f"(checked={summary['checked']}, "
              f"already_tracked={summary['already']})",
              flush=True)

    # ── Stale open-order cleanup ────────────────────────────────────
    # Catches the OTHER class of bug: _open_live places an order, the
    # fill poll times out, the cancel attempt fails (rare but happens
    # when the CLOB is matching mid-cancel), the order stays live on
    # Polymarket's book and never fills. The position never appears
    # because no fill ever lands - it's just stuck capital.
    #
    # Strategy: any open order older than `_STALE_ORDER_AGE_S` (1
    # hour) with zero matched-size is considered abandoned and gets
    # cancelled. We don't touch orders that have started matching
    # (size_matched > 0): they may still complete, and a mid-fill
    # cancel is a footgun.
    try:
        cfg.wallet_address  # noqa: B018 - sanity-touch
        from execution.pm_executor import _get_clob_client
        client = _get_clob_client(cfg.wallet_address, pk)
        if client is None:
            raise RuntimeError("CLOB client construction returned None")
        open_orders = client.get_open_orders()  # type: ignore[union-attr]
    except Exception as exc:
        print(f"[pm_reconciler] open-orders fetch failed: "
              f"{type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return summary

    if not isinstance(open_orders, list):
        return summary
    summary["orders_seen"] = len(open_orders)

    import time as _time
    now_ms = int(_time.time() * 1000)
    stale_age_ms = _STALE_ORDER_AGE_S * 1000
    to_cancel: list[str] = []
    for o in open_orders:
        try:
            order_id = o.get("id") or o.get("orderID") or o.get("orderId")
            if not order_id:
                continue
            size_matched = float(o.get("size_matched") or 0.0)
            if size_matched > 0:
                # Partial fill in progress - hands off.
                continue
            # CLOB returns created_at as ISO string OR unix-ms int
            # depending on version. Handle both.
            created = o.get("created_at") or o.get("createdAt") or 0
            if isinstance(created, str):
                # ISO 8601. Parse with datetime to avoid pulling in
                # python-dateutil.
                from datetime import datetime
                try:
                    created_dt = datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    )
                    created_ms = int(created_dt.timestamp() * 1000)
                except Exception:
                    created_ms = 0
            else:
                created_ms = int(created) if created else 0
                # Polymarket sometimes returns SECONDS instead of ms.
                # Heuristic: anything before year-2001 in ms is
                # almost certainly seconds.
                if 0 < created_ms < 10_000_000_000:
                    created_ms *= 1000
            if created_ms == 0 or (now_ms - created_ms) < stale_age_ms:
                continue
            to_cancel.append(str(order_id))
        except Exception as exc:
            print(f"[pm_reconciler] order inspect failed: "
                  f"{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)

    if to_cancel:
        try:
            client.cancel_orders(to_cancel)  # type: ignore[union-attr]
            summary["orders_cancelled"] = len(to_cancel)
            print(f"[pm_reconciler] cancelled {len(to_cancel)} stale "
                  f"open order(s) (older than "
                  f"{_STALE_ORDER_AGE_S // 60} min, zero fill)",
                  flush=True)
        except Exception as exc:
            print(f"[pm_reconciler] cancel batch failed: "
                  f"{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            summary["errors"] += 1

    return summary


def _fetch_activity_costs(funder: str) -> dict:
    """Return {(cond_id_lower, outcome_idx) -> total_usdc_paid}.

    Walks the Polymarket activity feed and aggregates BUY trades.
    `usdcSize` on each trade is the actual USDC sent on-chain
    INCLUDING taker fees - the same number Polymarket uses to
    compute its realized P&L. Reading this instead of /positions'
    `initialValue` keeps Delfi's cost_usd within rounding error of
    Polymarket's view.

    Returns an empty dict on fetch failure; the caller falls back
    to /positions.initialValue, so the reconciler still works
    (just with the ~1-2% fee gap).
    """
    out: dict[tuple[str, int], float] = {}
    try:
        resp = requests.get(
            _DATA_API_ACTIVITY,
            params={"user": funder, "limit": "200", "type": "TRADE"},
            headers=_HTTP_HEADERS,
            timeout=_DATA_API_TIMEOUT_S,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        print(f"[pm_reconciler] activity fetch failed: "
              f"{type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return out
    if not isinstance(rows, list):
        return out
    for r in rows:
        if r.get("type") != "TRADE" or r.get("side") != "BUY":
            continue
        cid = (r.get("conditionId") or "").lower()
        idx = r.get("outcomeIndex")
        if not cid or idx is None:
            continue
        try:
            u = float(r.get("usdcSize") or 0)
        except (TypeError, ValueError):
            continue
        if u <= 0:
            continue
        key = (cid, int(idx))
        out[key] = out.get(key, 0.0) + u
    return out


def _outcome_to_side(row: dict) -> Optional[str]:
    """Map a data-api position outcome to a Delfi pm_positions.side.

    Polymarket's binary markets always present outcomeIndex 0 as the
    "yes/up/over/positive" side and outcomeIndex 1 as the
    "no/down/under/negative" side. Delfi's pm_positions.side is
    CHAR(3) constrained to 'YES'/'NO'.

    For negative-risk multi-outcome markets the indices are
    contract-specific and we don't have a stable mapping yet - return
    None and let the caller skip the import.
    """
    if row.get("negativeRisk"):
        # Multi-outcome markets (e.g. "Which team wins the title?")
        # use a different on-chain contract and don't have a clean
        # YES/NO mapping. Skip for now; future work will widen
        # pm_positions.side or add a separate table.
        return None
    idx = row.get("outcomeIndex")
    if idx == 0:
        return "YES"
    if idx == 1:
        return "NO"
    return None


def _import_position(*, user_id: str, row: dict, side: str,
                     activity_costs: Optional[dict] = None) -> None:
    """INSERT a single on-chain position into pm_positions.

    All fields populated from the data-api row are the on-chain truth:
    `size` is the actual share count after partial fills, `avgPrice`
    is the volume-weighted average fill price (not the original limit
    price), `cashPnl` and `currentValue` reflect the live mark.

    Status detection:
      redeemable=False -> 'open' (market still live or pending
                                  resolution)
      redeemable=True  -> 'settled' (on-chain resolution decided), with
                                    settlement_outcome inferred from
                                    `currentValue / size`: > 0.5 means
                                    the held side won, else it lost.
    """
    cond_id     = (row.get("conditionId") or "").lower()
    size        = float(row.get("size") or 0.0)
    avg_price   = float(row.get("avgPrice") or 0.0)
    cur_value   = float(row.get("currentValue") or 0.0)
    # Prefer the fee-inclusive cost from /activity if we have it
    # (matches the executor's new _extract_filled_cost behaviour).
    # Falls back to /positions.initialValue, then size*avgPrice.
    init_value  = None
    if activity_costs is not None:
        idx = 0 if side.upper() == "YES" else 1
        init_value = activity_costs.get((cond_id, idx))
    if not init_value:
        init_value = float(row.get("initialValue") or (size * avg_price))
    init_value = float(init_value)
    redeemable  = bool(row.get("redeemable"))
    title       = (row.get("title") or "(unknown)").strip()
    slug        = row.get("slug") or None
    event_slug  = row.get("eventSlug") or None

    if size <= 0:
        # 0-size redeemable entries are leftover dust the user has
        # already redeemed but data-api still surfaces. Skip the
        # import - there's nothing to track.
        return

    if redeemable:
        status = "settled"
        won = (cur_value / size) > 0.5  # per-share payout > $0.50
        settlement_outcome = side if won else _opposite(side)
        # Approximate realized P&L. The exact number is the on-chain
        # CTF.redeemPositions emission, which we don't have here -
        # data-api's currentValue is close enough for the dashboard.
        realized_pnl = cur_value - init_value
    else:
        status = "open"
        settlement_outcome = None
        realized_pnl = None

    # Pull the original forecaster fields from market_evaluations so
    # the reconciler-imported row shows the same context a normally-
    # opened position does: category, archetype, Claude probability,
    # confidence, ev_bps, the full reasoning narrative, and the
    # gamma market_id (not just the condition_id). Bot-placed
    # positions always have a matching evaluation; if the lookup
    # misses (very rare - pre-eval-logging row, or evaluation table
    # was wiped), category + archetype still get derived locally
    # from the question text via the same classifier the bot uses,
    # so the UI never shows a blank category cell for an imported
    # position.
    market_id: Optional[str] = None
    pred_id: Optional[int]   = None
    eval_category:           Optional[str]   = None
    eval_archetype:          Optional[str]   = None
    eval_claude_probability: Optional[float] = None
    eval_confidence:         Optional[float] = None
    eval_ev_bps:             Optional[float] = None
    eval_reasoning:          Optional[str]   = None
    with get_engine().connect() as conn:
        cur = conn.execute(text(
            "SELECT market_id, prediction_id, category, market_archetype, "
            "       claude_probability, confidence, ev_bps, reasoning "
            "FROM market_evaluations "
            "WHERE LOWER(condition_id) = :c "
            "ORDER BY id DESC LIMIT 1"
        ), {"c": cond_id})
        hit = cur.fetchone()
        if hit:
            (market_id, pred_id, eval_category, eval_archetype,
             eval_claude_probability, eval_confidence, eval_ev_bps,
             eval_reasoning) = hit
    if not market_id:
        market_id = cond_id  # last-resort identifier

    # Classifier fallback for category + archetype when no eval row
    # exists. classify_archetype is the same pure function the bot
    # runs on every market during scan, so the derived label matches
    # what a bot-opened position would have. Archetype is the fine-
    # grained label (crypto_short, sports_other, etc.); category is
    # the broad human-facing family (crypto, sports, politics, ...)
    # that the UI's Category column expects.
    if not eval_archetype or not eval_category:
        try:
            from engine.archetype_classifier import classify_archetype
            derived = classify_archetype(title, event_slug=event_slug)
            if not eval_archetype:
                eval_archetype = derived
            if not eval_category:
                eval_category = _archetype_to_category(derived)
        except Exception as exc:
            print(f"[pm_reconciler] classifier fallback failed for "
                  f"cond={cond_id}: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)

    # data-api gives us the market's natural resolution date (gamma
    # endDate, YYYY-MM-DD). Parse into a datetime for the
    # expected_resolution_at column so the Positions "Closes" column
    # populates the same way a normally-opened row does. Failure to
    # parse is non-fatal; we just leave the column NULL.
    expected_resolution_at = _parse_end_date(row.get("endDate"))

    # Reasoning fallback: prefer the forecaster's original narrative
    # so the expanded row reads identically to a bot-opened position.
    # When the eval is missing entirely (legacy row), tag the
    # placeholder clearly so the user can spot reconciler-only
    # imports.
    reasoning_text = (
        eval_reasoning if eval_reasoning
        else "[imported by reconciler from Polymarket data-api - "
             "no matching evaluation found]"
    )

    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO pm_positions ("
            "  user_id, prediction_id, market_id, condition_id, "
            "  slug, question, category, "
            "  side, shares, entry_price, cost_usd, "
            "  claude_probability, ev_bps, confidence, "
            "  mode, status, settled_at, settlement_outcome, "
            "  realized_pnl_usd, expected_resolution_at, "
            "  event_slug, market_archetype, venue, reasoning"
            ") VALUES ("
            "  :uid, :pid, :mid, :cond, :slug, :q, :cat, "
            "  :side, :sz, :px, :cost, "
            "  :cp, :ev, :conf, "
            "  'live', :status, "
            "  CASE WHEN :status = 'settled' THEN CURRENT_TIMESTAMP ELSE NULL END, "
            "  :outcome, :pnl, :resolved_at, "
            "  :event_slug, :arch, 'polymarket', :reasoning"
            ")"
        ), {
            "uid":         user_id,
            "pid":         pred_id,
            "mid":         market_id,
            "cond":        cond_id,
            "slug":        slug,
            "q":           title,
            "cat":         eval_category,
            "side":        side,
            "sz":          size,
            "px":          avg_price,
            "cost":        init_value if init_value > 0 else (size * avg_price),
            "cp":          eval_claude_probability,
            "ev":          eval_ev_bps,
            "conf":        eval_confidence,
            "status":      status,
            "outcome":     settlement_outcome,
            "pnl":         realized_pnl,
            "resolved_at": expected_resolution_at,
            "event_slug":  event_slug,
            "arch":        eval_archetype,
            "reasoning":   reasoning_text,
        })

    # If we found a matching evaluation, link it back. This makes the
    # Positions row clickable into the original forecast reasoning -
    # same UX as bot-originated rows.
    if pred_id is not None:
        with get_engine().begin() as conn:
            conn.execute(text(
                "UPDATE market_evaluations "
                "SET pm_position_id = ("
                "  SELECT id FROM pm_positions "
                "  WHERE LOWER(condition_id) = :c AND side = :side "
                "  ORDER BY id DESC LIMIT 1"
                ") "
                "WHERE prediction_id = :pid AND pm_position_id IS NULL"
            ), {"c": cond_id, "side": side, "pid": pred_id})


def _parse_end_date(s: Optional[str]):
    """Convert data-api's gamma endDate to a datetime, or None.

    Polymarket returns this as "YYYY-MM-DD" most of the time, but the
    occasional full ISO-8601 timestamp shows up too. Bare dates land
    at midnight UTC - close enough for the "Closes in X" UI column.
    """
    if not s or not isinstance(s, str):
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None


def _opposite(side: str) -> str:
    return "NO" if side.upper() == "YES" else "YES"


# Broad-family category derivation from the bot's fine-grained
# archetype taxonomy. The Positions page's Category column expects
# a short human-readable family ("crypto", "sports", "politics", ...)
# not the internal sub-tier label ("crypto_short", "sports_other"...).
# When the reconciler can't pull a category from a matching
# evaluation, classify_archetype gives us the archetype and this
# mapping collapses it into the user-facing family.
_ARCHETYPE_FAMILY: dict[str, str] = {
    # crypto
    "crypto":            "crypto",
    "crypto_short":      "crypto",
    "price_threshold":   "crypto",
    # sports
    "basketball":        "sports",
    "baseball":          "sports",
    "football":          "sports",
    "soccer":            "sports",
    "hockey":            "sports",
    "tennis":            "sports",
    "cricket":           "sports",
    "esports":           "sports",
    "sports_other":      "sports",
    # politics / geo
    "election":          "politics",
    "policy_event":      "politics",
    "geopolitical_event": "geopolitics",
    # misc
    "activity_count":    "other",
    "binary_event":      "other",
}


def _archetype_to_category(archetype: Optional[str]) -> Optional[str]:
    """Collapse a fine-grained archetype into the broad category the
    UI Category column expects. Unknown labels (e.g. a new archetype
    added since this map was written) fall through unchanged so
    nothing gets silently mislabelled - the user will spot the new
    label in the column and we can update the map."""
    if not archetype:
        return None
    return _ARCHETYPE_FAMILY.get(archetype, archetype)


def _check_drift(existing: dict, row: dict, side: str) -> None:
    """Warn the user when the DB and on-chain disagree on size or cost.

    The reconciler does not auto-overwrite existing rows (that would
    destroy the audit trail) but it can detect when something is
    definitely wrong and surface it. The dominant case is the
    limit-vs-fill bug: pm_executor recorded `cost_usd = shares * limit
    price`, but the order actually filled at a better price, so
    on-chain cost < DB cost. The Solana 1AM ET position was the first
    instance the user noticed ($3.85 DB vs $2.31 on-chain).
    """
    if existing.get("status") != "open":
        # Settled rows can legitimately drift from the live MTM that
        # data-api returns; don't alarm on those.
        return
    onchain_shares = float(row.get("size") or 0.0)
    onchain_cost   = float(row.get("initialValue") or 0.0)
    db_shares = existing.get("shares") or 0.0
    db_cost   = existing.get("cost") or 0.0

    drifts = []
    if onchain_shares > 0 and abs(db_shares - onchain_shares) / onchain_shares > _DRIFT_TOLERANCE:
        drifts.append(f"shares: db={db_shares:.3f} on-chain={onchain_shares:.3f}")
    if onchain_cost > 0 and abs(db_cost - onchain_cost) / onchain_cost > _DRIFT_TOLERANCE:
        drifts.append(f"cost: db=${db_cost:.2f} on-chain=${onchain_cost:.2f}")
    if not drifts:
        return

    pid = existing.get("id")
    title = (row.get("title") or "")[:60]
    msg = (
        f"pm_positions id={pid} ({title!r}) drift: "
        + "; ".join(drifts)
    )
    print(f"[pm_reconciler] DRIFT WARNING: {msg}", flush=True)
    try:
        from db.logger import log_event
        log_event(
            event_type="position_drift",
            severity=2,
            description=(
                "Polymarket position #" + str(pid) + " (" + title +
                ") disagrees with on-chain state: " + "; ".join(drifts) +
                ". Most likely the original fill landed at a better "
                "price than the limit Delfi recorded. The DB row will "
                "NOT be auto-overwritten; review and fix manually if "
                "the difference matters for P&L."
            ),
            source="pm_reconciler.drift",
        )
    except Exception as exc:
        print(f"[pm_reconciler] log_event failed: {exc}",
              file=sys.stderr, flush=True)


def _alert_multi_outcome_skip(row: dict) -> None:
    """Fire a user-visible alert the first time we see an unmappable
    multi-outcome position. Delfi can't track it (pm_positions.side
    is binary YES/NO), so the user needs to know they have exposure
    the dashboard won't reflect.

    Deduplicated by conditionId so we don't spam Telegram on every
    reconciler tick - same market only fires once per daemon
    incarnation.
    """
    cond_id = (row.get("conditionId") or "").lower()
    if not cond_id or cond_id in _MULTI_OUTCOME_ALERTED:
        return
    _MULTI_OUTCOME_ALERTED.add(cond_id)
    title    = (row.get("title") or "(unknown)")[:80]
    size     = row.get("size")
    outcome  = row.get("outcome")
    try:
        from db.logger import log_event
        log_event(
            event_type="position_untracked",
            severity=2,
            description=(
                f"Polymarket position on '{title}' (outcome: "
                f"{outcome!r}, size: {size}) cannot be imported into "
                f"Delfi - it's a multi-outcome / negative-risk market "
                f"and pm_positions.side only supports binary YES/NO. "
                f"The position is real on-chain; manage it manually "
                f"on polymarket.com until multi-outcome support lands."
            ),
            source="pm_reconciler.untracked",
        )
    except Exception as exc:
        print(f"[pm_reconciler] log_event failed: {exc}",
              file=sys.stderr, flush=True)


def _alert_import(row: dict, side: str) -> None:
    """Fire a user-visible event when the reconciler backfills a
    position. If this fires regularly, _open_live's poll-timeout
    behaviour is leaking - the safety net is loud about catching
    each case so the user notices.
    """
    title = (row.get("title") or "")[:80]
    size  = row.get("size")
    cost  = row.get("initialValue")
    redeemable = bool(row.get("redeemable"))
    state = "settled" if redeemable else "open"
    try:
        from db.logger import log_event
        log_event(
            event_type="position_reconciled",
            severity=1,
            description=(
                f"Reconciler imported missing Polymarket position "
                f"({state}): {title} {side} size={size} cost=${cost}. "
                f"This means _open_live placed the order but lost the "
                f"fill confirmation; the safety net caught it."
            ),
            source="pm_reconciler.import",
        )
    except Exception as exc:
        print(f"[pm_reconciler] log_event failed: {exc}",
              file=sys.stderr, flush=True)


if __name__ == "__main__":
    # One-shot CLI: `python -m engine.pm_reconciler` runs a single
    # reconciliation pass against the local user and prints the
    # summary. Used to backtrack a stale DB after the executor's
    # poll-timeout bug ate fills.
    import json
    result = reconcile_positions()
    print(json.dumps(result, indent=2))
