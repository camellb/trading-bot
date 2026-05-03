#!/usr/bin/env python3
"""
Backfill outcomes for skipped market evaluations.

Goal: validate the "Delfi filter saves money" finding from the
2026-05-03 audit by fetching the actual resolution of every market
the bot SKIPPED. The audit's earlier estimate (-$55.61 expected P&L
on 97 skips) used empirical price-band win rates as a proxy because
the local DB doesn't store outcomes for skipped markets. This script
asks Polymarket directly.

Method:
  1. Read market_id + market_price_yes (the recorded entry-time
     price) for every market_evaluations row with recommendation='SKIP'.
  2. For each market_id, hit
     https://gamma-api.polymarket.com/markets?id=<id>
     and check `closed` + `outcomePrices`. A market is resolved when
     `closed=true` and outcomePrices is e.g. ["1","0"] (YES won) or
     ["0","1"] (NO won).
  3. For each resolved skip:
       - market favourite at evaluation time = side with implied
         prob >= 0.50 (using the recorded market_price_yes).
       - simulated trade = bet that side, at that price, for the
         same average stake the bot was using at the time.
       - simulated P&L = +stake/price - cost on a win; -cost on a loss.
  4. Aggregate and print: skips resolved / unresolved, net P&L if
     we'd taken every market favourite, comparison to the actual
     entered-trade P&L.

Run from the repo root:

    python3 Delfibot/research/skip_audit/backfill_skipped_outcomes.py

Read-only against the local DB and the public Polymarket gamma API.
No writes anywhere.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

DB_PATH = os.path.expanduser(
    "~/Library/Application Support/com.delfi.desktop/delfi.db"
)
GAMMA_BASE = "https://gamma-api.polymarket.com/markets"
RATE_LIMIT_S = 0.15  # ~7 req/s, well below Polymarket's published cap


def fetch_market(market_id: str) -> Optional[dict]:
    """Return the market dict from gamma, or None on any failure.

    Gamma's path-style endpoint /markets/{id} returns the single
    market object. The ?id= query-string variant returns an empty
    array - confirmed against this DB on 2026-05-03. Use the path
    form.

    Cloudflare in front of gamma blocks the default Python
    "Python-urllib/X" user agent with a 1010 challenge. Send a
    browser-style UA - curl works without one but urllib gets
    flagged. The bot's own polymarket feed avoids this by going
    through the data-api endpoint with a different path; the
    research script doesn't have that infrastructure available
    so we just impersonate.

    Failures here aren't fatal - we skip the row and report the
    count at the end.
    """
    url = f"{GAMMA_BASE}/{market_id}"
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
        if isinstance(payload, list):
            return payload[0] if payload else None
        return payload
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return None


def parse_outcome(market: dict) -> Optional[str]:
    """Return 'YES' / 'NO' / None for a binary market.

    Polymarket binary markets have outcomes [YES, NO]. The
    `outcomePrices` field after resolution reads as a JSON-encoded
    string list, e.g. '["1", "0"]' for YES win or '["0", "1"]' for
    NO win. Some markets resolve as 50/50 (refund) - we treat those
    as None ("invalid").
    """
    if not market.get("closed"):
        return None
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    try:
        yes_p = float(raw[0])
        no_p  = float(raw[1])
    except (TypeError, ValueError):
        return None
    if yes_p >= 0.99:  return "YES"
    if no_p  >= 0.99:  return "NO"
    return None  # ambiguous / 50-50 refund


def main() -> int:
    if not os.path.isfile(DB_PATH):
        print(f"DB not found at {DB_PATH}", file=sys.stderr)
        return 2

    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row

    skips = list(c.execute(
        "SELECT id, market_id, question, market_price_yes, market_archetype "
        "FROM market_evaluations "
        "WHERE recommendation='SKIP' AND market_price_yes IS NOT NULL "
        "ORDER BY evaluated_at DESC"
    ))
    print(f"Skipped evaluations to backfill: {len(skips)}")
    print()

    # Average stake on entered trades, used as the per-skip stake
    # proxy so the simulated P&L is comparable to actual entered-trade
    # totals.
    stake_row = c.execute(
        "SELECT AVG(cost_usd) AS avg_cost, COUNT(*) AS n "
        "FROM pm_positions "
        "WHERE mode='simulation' AND status IN ('settled','invalid')"
    ).fetchone()
    avg_stake = float(stake_row["avg_cost"] or 0)
    print(f"Average stake on entered trades: ${avg_stake:.2f} (n={stake_row['n']})")
    print()

    n_resolved = 0
    n_unresolved = 0
    n_invalid = 0
    n_unfetchable = 0

    pnl_total = 0.0
    pnl_by_band: dict[str, list[float]] = {}
    pnl_by_arch: dict[str, list[float]] = {}

    BANDS = [
        (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
        (0.70, 0.80), (0.80, 0.90), (0.90, 1.001),
    ]
    def band_label(p: float) -> str:
        for lo, hi in BANDS:
            if lo <= p < hi:
                hi_disp = 1.0 if hi > 1.0 else hi
                return f"{lo:.2f}+" if hi_disp >= 1.0 else f"{lo:.2f}-{hi_disp:.2f}"
        return "?"

    for i, s in enumerate(skips, 1):
        if i % 10 == 0:
            print(f"  [{i}/{len(skips)}]...", flush=True)

        m_yes = float(s["market_price_yes"])
        fav_side = "YES" if m_yes >= 0.50 else "NO"
        fav_price = m_yes if m_yes >= 0.50 else 1.0 - m_yes

        market = fetch_market(s["market_id"])
        time.sleep(RATE_LIMIT_S)

        if market is None:
            n_unfetchable += 1
            continue

        outcome = parse_outcome(market)
        if outcome is None:
            if market.get("closed"):
                n_invalid += 1
            else:
                n_unresolved += 1
            continue

        n_resolved += 1
        # Simulated P&L assuming the favourite-side bet at recorded price.
        won = outcome == fav_side
        if won:
            shares = avg_stake / fav_price
            pnl = shares - avg_stake  # redeem at $1, paid fav_price per share
        else:
            pnl = -avg_stake

        pnl_total += pnl
        pnl_by_band.setdefault(band_label(fav_price), []).append(pnl)
        arch = s["market_archetype"] or "<null>"
        pnl_by_arch.setdefault(arch, []).append(pnl)

    print()
    print("=" * 70)
    print("BACKFILL RESULTS")
    print("=" * 70)
    print(f"  resolved:        {n_resolved}")
    print(f"  still open:      {n_unresolved}")
    print(f"  invalid (50/50): {n_invalid}")
    print(f"  unfetchable:     {n_unfetchable}")
    print()
    print(f"Pure-market simulation on the {n_resolved} resolved skips:")
    print(f"  total simulated P&L: ${pnl_total:+.2f}")
    if n_resolved:
        print(f"  per-trade avg:       ${pnl_total/n_resolved:+.2f}")
    print()
    print("By price band (resolved skips only):")
    print(f"  {'band':<12} {'n':>4} {'wins':>5} {'win%':>6} {'pnl':>10}")
    for lbl, pnls in sorted(pnl_by_band.items()):
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        print(f"  {lbl:<12} {n:>4} {wins:>5} {100*wins/n:>5.0f}% ${total:>+8.2f}")
    print()
    print("By archetype (resolved skips only, top 10 by n):")
    print(f"  {'archetype':<22} {'n':>4} {'wins':>5} {'win%':>6} {'pnl':>10}")
    for arch, pnls in sorted(pnl_by_arch.items(), key=lambda kv: -len(kv[1]))[:10]:
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        print(f"  {arch:<22} {n:>4} {wins:>5} {100*wins/n:>5.0f}% ${total:>+8.2f}")

    print()
    print("=" * 70)
    print("Comparison to actual entered-trade P&L:")
    print("=" * 70)
    actual = c.execute(
        "SELECT COUNT(*) AS n, SUM(realized_pnl_usd) AS pnl "
        "FROM pm_positions "
        "WHERE mode='simulation' AND status IN ('settled','invalid')"
    ).fetchone()
    print(f"  Delfi-filtered (actual):  n={actual['n']}  pnl=${actual['pnl'] or 0:+.2f}")
    print(f"  Pure market (skips only): n={n_resolved}  pnl=${pnl_total:+.2f}  (simulated)")
    print(f"  Pure market (combined):   n={(actual['n'] or 0) + n_resolved}  pnl=${(actual['pnl'] or 0) + pnl_total:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
