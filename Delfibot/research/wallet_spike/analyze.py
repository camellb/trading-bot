"""
Analyze top-100 Polymarket wallet performance over the last 30 days.

Reads wallets.csv + trades.csv produced by pull_wallets.py. No API calls.

Outputs to stdout (and is what wallet_spike.md summarises):
    1. Per-wallet stats sorted by ROI (with min sample-size floor)
    2. Two baselines on the SAME trade set:
         - "always-favourite": always bet the side priced >= 0.50 at entry
         - market-default-with-V1-skip-list (just the favourite proxy here;
           we don't have archetype labels per-trade from Polymarket)
    3. Survivorship test: split each wallet's 30d window into a 23d train
       and 7d test. Pick top wallets by ROI in train; measure their
       performance in test. If they hold up, signal is real; if they
       regress to the mean, the leaderboard is just lookback.

Notes on the data shape (closed-positions endpoint):
    - `outcome`: "Yes" or "No"
    - `avgPrice`: average entry price on the chosen side, in [0, 1]
    - `totalBought`: USD cost
    - `realizedPnl`: USD realised P&L (positive = won)
    - `timestamp`: when the position closed (settled)

A trade is a WIN if realizedPnl > 0. (Polymarket settles winners at $1
and losers at $0, so a positive realisedPnl is unambiguous.)
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
WALLETS_CSV = HERE / "wallets.csv"
TRADES_CSV = HERE / "trades.csv"

MIN_SAMPLE_FOR_RANKING = 20      # min resolved trades to rank a wallet
TRAIN_DAYS = 23                  # 23/7 split for survivorship test
TEST_DAYS = 7
TOP_K_FOR_FORWARD_TEST = 10      # pick top 10 wallets by train ROI


def load_trades() -> list[dict]:
    """Load trades.csv, coercing types."""
    if not TRADES_CSV.exists():
        print(f"error: {TRADES_CSV.name} not found - run pull_wallets.py first", file=sys.stderr)
        sys.exit(1)
    rows: list[dict] = []
    with open(TRADES_CSV) as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                ts = datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None
                rows.append({
                    "wallet":       row["wallet"],
                    "conditionId":  row["conditionId"],
                    "title":        row["title"],
                    "outcome":      (row["outcome"] or "").upper(),
                    "avgPrice":     float(row["avgPrice"]) if row["avgPrice"] else None,
                    "totalBought":  float(row["totalBought"]) if row["totalBought"] else 0.0,
                    "realizedPnl":  float(row["realizedPnl"]) if row["realizedPnl"] else 0.0,
                    "timestamp":    ts,
                })
            except (ValueError, KeyError):
                continue
    return rows


def per_wallet_stats(trades: list[dict]) -> list[dict]:
    """Aggregate per wallet. One row per wallet."""
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_wallet[t["wallet"]].append(t)
    out: list[dict] = []
    for wallet, ts in by_wallet.items():
        n = len(ts)
        if n == 0:
            continue
        wins = sum(1 for t in ts if t["realizedPnl"] > 0)
        losses = sum(1 for t in ts if t["realizedPnl"] <= 0)
        total_cost = sum(t["totalBought"] for t in ts)
        total_pnl  = sum(t["realizedPnl"] for t in ts)
        roi = total_pnl / total_cost if total_cost > 0 else 0.0
        prices_costs = [(t["avgPrice"], t["totalBought"]) for t in ts
                        if t["avgPrice"] is not None and t["totalBought"] > 0]
        if prices_costs:
            mean_entry = sum(p * c for p, c in prices_costs) / sum(c for _, c in prices_costs)
        else:
            mean_entry = None
        out.append({
            "wallet":      wallet,
            "n":           n,
            "wins":        wins,
            "losses":      losses,
            "win_rate":    wins / n if n else 0.0,
            "total_cost":  total_cost,
            "total_pnl":   total_pnl,
            "roi":         roi,
            "mean_entry":  mean_entry,
        })
    return out


def baseline_always_favourite(trades: list[dict]) -> dict:
    """
    Counterfactual: bet the side priced >= 0.50 at entry, with the same
    cost the wallet wagered. Recovers the YES-side market price by
    inverting the wallet's chosen side, then settles via the wallet's
    realised P&L sign to figure out which side actually won.
    """
    n = wins = 0
    total_cost = total_pnl = 0.0
    for t in trades:
        p = t["avgPrice"]
        if p is None or t["totalBought"] <= 0:
            continue
        if t["outcome"] not in ("YES", "NO"):
            continue
        # Recover the YES-side market price from the wallet's chosen side.
        market_p_yes = p if t["outcome"] == "YES" else 1.0 - p
        # Recover whether YES was the winning outcome.
        yes_won = (t["outcome"] == "YES" and t["realizedPnl"] > 0) or \
                  (t["outcome"] == "NO"  and t["realizedPnl"] <= 0)
        favourite_yes = market_p_yes >= 0.5
        entry_price = market_p_yes if favourite_yes else 1.0 - market_p_yes
        won = (favourite_yes == yes_won)
        cost = t["totalBought"]
        shares = cost / entry_price if entry_price > 0 else 0.0
        proceeds = shares if won else 0.0
        n += 1
        if won:
            wins += 1
        total_cost += cost
        total_pnl += proceeds - cost
    return {
        "label":       "always-favourite",
        "n":           n,
        "wins":        wins,
        "win_rate":    wins / n if n else 0.0,
        "total_cost":  total_cost,
        "total_pnl":   total_pnl,
        "roi":         total_pnl / total_cost if total_cost > 0 else 0.0,
    }


def survivorship_test(trades: list[dict], train_days: int = TRAIN_DAYS,
                      test_days: int = TEST_DAYS, top_k: int = TOP_K_FOR_FORWARD_TEST) -> dict:
    """
    Split the 30d window into train (first 23d) and test (last 7d).
    Pick top-K wallets by ROI in the train window, measure their ROI on
    test-window trades. If they keep their edge → real signal. If they
    regress to mean (or worse) → lookback bias.
    """
    if not trades:
        return {"error": "no trades"}
    latest = max(t["timestamp"] for t in trades if t["timestamp"])
    train_cutoff = latest - timedelta(days=test_days)
    train_trades = [t for t in trades if t["timestamp"] and t["timestamp"] < train_cutoff]
    test_trades  = [t for t in trades if t["timestamp"] and t["timestamp"] >= train_cutoff]

    train_stats = per_wallet_stats(train_trades)
    train_stats = [w for w in train_stats if w["n"] >= MIN_SAMPLE_FOR_RANKING // 2]
    train_stats.sort(key=lambda w: w["roi"], reverse=True)
    top_wallets = [w["wallet"] for w in train_stats[:top_k]]

    if not top_wallets:
        return {"error": f"no wallets cleared sample floor in train window of {train_days}d"}

    test_top_trades = [t for t in test_trades if t["wallet"] in top_wallets]
    test_all_trades = test_trades

    def roi_of(rows: list[dict]) -> tuple[int, float, float]:
        n = len(rows)
        cost = sum(t["totalBought"] for t in rows)
        pnl  = sum(t["realizedPnl"] for t in rows)
        return n, cost, pnl / cost if cost > 0 else 0.0

    n_top, cost_top, roi_top = roi_of(test_top_trades)
    n_all, cost_all, roi_all = roi_of(test_all_trades)

    baseline_top = baseline_always_favourite(test_top_trades)
    baseline_all = baseline_always_favourite(test_all_trades)

    return {
        "train_days":        train_days,
        "test_days":         test_days,
        "train_n_wallets":   len(train_stats),
        "top_k_wallets":     top_k,
        "test_top_n":        n_top,
        "test_top_cost":     cost_top,
        "test_top_roi":      roi_top,
        "test_top_baseline": baseline_top["roi"],
        "test_all_n":        n_all,
        "test_all_cost":     cost_all,
        "test_all_roi":      roi_all,
        "test_all_baseline": baseline_all["roi"],
        "top_wallets":       top_wallets,
    }


def fmt_pct(v: float, digits: int = 1) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v * 100:.{digits}f}%"


def fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    abs_v = abs(v)
    if abs_v >= 1000:
        return f"{sign}${abs_v:,.0f}"
    return f"{sign}${abs_v:.2f}"


def main():
    trades = load_trades()
    print(f"\n[analyze] loaded {len(trades)} trades from {TRADES_CSV.name}")
    if not trades:
        print("no trades loaded - aborting")
        return

    earliest = min(t["timestamp"] for t in trades if t["timestamp"])
    latest   = max(t["timestamp"] for t in trades if t["timestamp"])
    print(f"[analyze] window: {earliest.date()} → {latest.date()}")

    stats = per_wallet_stats(trades)
    rankable = [w for w in stats if w["n"] >= MIN_SAMPLE_FOR_RANKING]
    rankable.sort(key=lambda w: w["roi"], reverse=True)

    print(f"\n[analyze] {len(stats)} wallets total, "
          f"{len(rankable)} have ≥{MIN_SAMPLE_FOR_RANKING} resolved trades")
    print()
    print(f"{'WALLET':14} {'N':>4} {'WIN%':>6} {'COST':>10} {'PNL':>12} {'ROI':>8} {'MEAN_P':>8}")
    print("─" * 72)
    for w in rankable[:25]:
        wallet_short = w["wallet"][:12] + "…"
        mp = f"{w['mean_entry']:.2f}" if w["mean_entry"] is not None else "  -"
        print(f"{wallet_short:14} {w['n']:>4} {w['win_rate']*100:>5.1f}% "
              f"{fmt_money(w['total_cost']):>10} {fmt_money(w['total_pnl']):>12} "
              f"{fmt_pct(w['roi']):>8} {mp:>8}")

    if len(rankable) > 25:
        print(f"... and {len(rankable) - 25} more rankable wallets (ranked).")

    print("\n[analyze] baselines on the full trade set")
    print()
    pooled_n     = len(trades)
    pooled_cost  = sum(t["totalBought"] for t in trades)
    pooled_pnl   = sum(t["realizedPnl"] for t in trades)
    pooled_roi   = pooled_pnl / pooled_cost if pooled_cost > 0 else 0.0
    pooled_wins  = sum(1 for t in trades if t["realizedPnl"] > 0)
    pooled_winrate = pooled_wins / pooled_n if pooled_n else 0.0
    baseline = baseline_always_favourite(trades)
    print(f"{'STRATEGY':28} {'N':>5} {'WIN%':>6} {'COST':>12} {'PNL':>12} {'ROI':>8}")
    print("─" * 80)
    print(f"{'top-100 wallets pooled':28} {pooled_n:>5} {pooled_winrate*100:>5.1f}% "
          f"{fmt_money(pooled_cost):>12} {fmt_money(pooled_pnl):>12} {fmt_pct(pooled_roi):>8}")
    print(f"{'always-favourite baseline':28} {baseline['n']:>5} "
          f"{baseline['win_rate']*100:>5.1f}% "
          f"{fmt_money(baseline['total_cost']):>12} {fmt_money(baseline['total_pnl']):>12} "
          f"{fmt_pct(baseline['roi']):>8}")
    print()
    delta = pooled_roi - baseline["roi"]
    if delta > 0.02:
        print(f"   → wallets beat the favourite by {fmt_pct(delta)} pp on aggregate.")
    elif delta < -0.02:
        print(f"   → wallets UNDERPERFORM the favourite by {fmt_pct(-delta)} pp.")
    else:
        print(f"   → wallets are within ±2pp of the favourite baseline (no clear edge).")

    surv = survivorship_test(trades)
    print(f"\n[analyze] survivorship test: train {TRAIN_DAYS}d → test {TEST_DAYS}d")
    print()
    if "error" in surv:
        print(f"   skipped: {surv['error']}")
    else:
        print(f"   train rankable wallets:   {surv['train_n_wallets']}")
        print(f"   top-{TOP_K_FOR_FORWARD_TEST} by train ROI tested in test window")
        print()
        print(f"   {'STRATEGY':32} {'N':>4} {'COST':>10} {'ROI':>8}")
        print("   " + "─" * 60)
        print(f"   {'test: top-K wallets':32} {surv['test_top_n']:>4} "
              f"{fmt_money(surv['test_top_cost']):>10} {fmt_pct(surv['test_top_roi']):>8}")
        print(f"   {'  baseline (always-fav)':32} {'-':>4} {'-':>10} "
              f"{fmt_pct(surv['test_top_baseline']):>8}")
        print()
        print(f"   {'test: ALL top-100 wallets':32} {surv['test_all_n']:>4} "
              f"{fmt_money(surv['test_all_cost']):>10} {fmt_pct(surv['test_all_roi']):>8}")
        print(f"   {'  baseline (always-fav)':32} {'-':>4} {'-':>10} "
              f"{fmt_pct(surv['test_all_baseline']):>8}")
        print()
        edge = surv["test_top_roi"] - surv["test_top_baseline"]
        if edge > 0.02:
            print(f"   → top-K wallets retained {fmt_pct(edge)} pp of edge in the test window.")
            print(f"     SIGNAL CANDIDATE: wallet selection survives one round of forward testing.")
        elif edge < -0.02:
            print(f"   → top-K wallets LOST {fmt_pct(-edge)} pp vs the favourite in test.")
            print(f"     Looks like lookback bias - the train-set winners regressed in test.")
        else:
            print(f"   → top-K wallets within ±2pp of favourite baseline in test.")
            print(f"     No clear forward edge from naive ROI-based wallet selection.")


if __name__ == "__main__":
    main()
