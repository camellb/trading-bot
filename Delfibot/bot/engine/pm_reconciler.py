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

    # Look up every existing (condition_id, side) pair in pm_positions
    # in one query so the per-row check is in-memory. Set of
    # (condition_id_lower, side) tuples.
    existing: set[tuple[str, str]] = set()
    with get_engine().connect() as conn:
        cur = conn.execute(text(
            "SELECT condition_id, side FROM pm_positions "
            "WHERE condition_id IS NOT NULL AND user_id = :uid"
        ), {"uid": user_id})
        for cond, side in cur.fetchall():
            if cond and side:
                existing.add((cond.lower(), side.upper()))

    for r in rows:
        try:
            cond_id = (r.get("conditionId") or "").lower()
            if not cond_id:
                continue
            side = _outcome_to_side(r)
            if side is None:
                # Negative-risk multi-outcome market with an outcome
                # index we don't map cleanly. Log and skip - importing
                # without a side would corrupt the row.
                print(f"[pm_reconciler] skipping unmappable outcome "
                      f"for cond={cond_id} outcome="
                      f"{r.get('outcome')!r} idx={r.get('outcomeIndex')!r}",
                      flush=True)
                continue
            if (cond_id, side) in existing:
                summary["already"] += 1
                continue

            _import_position(user_id=user_id, row=r, side=side)
            summary["imported"] += 1
            existing.add((cond_id, side))
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


def _import_position(*, user_id: str, row: dict, side: str) -> None:
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
    init_value  = float(row.get("initialValue") or (size * avg_price))
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

    # Match the original market_id used by the bot's open path when
    # possible. data-api returns conditionId, not the gamma marketId,
    # so we look it up in market_evaluations first - the analyst's
    # evaluation row keyed off the gamma marketId and stored the same
    # conditionId. If no eval exists (e.g. position predates the eval
    # logging), fall back to using conditionId as the market_id.
    market_id: Optional[str] = None
    pred_id: Optional[int] = None
    with get_engine().connect() as conn:
        cur = conn.execute(text(
            "SELECT market_id, prediction_id FROM market_evaluations "
            "WHERE LOWER(condition_id) = :c "
            "ORDER BY id DESC LIMIT 1"
        ), {"c": cond_id})
        hit = cur.fetchone()
        if hit:
            market_id, pred_id = hit
    if not market_id:
        market_id = cond_id  # last-resort identifier

    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO pm_positions ("
            "  user_id, prediction_id, market_id, condition_id, "
            "  slug, question, side, shares, entry_price, cost_usd, "
            "  mode, status, settled_at, settlement_outcome, "
            "  realized_pnl_usd, event_slug, venue, reasoning"
            ") VALUES ("
            "  :uid, :pid, :mid, :cond, :slug, :q, :side, :sz, "
            "  :px, :cost, 'live', :status, "
            "  CASE WHEN :status = 'settled' THEN CURRENT_TIMESTAMP ELSE NULL END, "
            "  :outcome, :pnl, :event_slug, 'polymarket', "
            "  '[imported by reconciler from Polymarket data-api]'"
            ")"
        ), {
            "uid":         user_id,
            "pid":         pred_id,
            "mid":         market_id,
            "cond":        cond_id,
            "slug":        slug,
            "q":           title,
            "side":        side,
            "sz":          size,
            "px":          avg_price,
            "cost":        init_value if init_value > 0 else (size * avg_price),
            "status":      status,
            "outcome":     settlement_outcome,
            "pnl":         realized_pnl,
            "event_slug":  event_slug,
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


def _opposite(side: str) -> str:
    return "NO" if side.upper() == "YES" else "YES"


if __name__ == "__main__":
    # One-shot CLI: `python -m engine.pm_reconciler` runs a single
    # reconciliation pass against the local user and prints the
    # summary. Used to backtrack a stale DB after the executor's
    # poll-timeout bug ate fills.
    import json
    result = reconcile_positions()
    print(json.dumps(result, indent=2))
