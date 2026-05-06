"""
Walk-forward backtest CLI.

Loads all settled positions, applies a hypothetical filter / multiplier
configuration, and reports out-of-sample ROI by walking the data
forward in 25-trade test windows. The point: a finding that looks
profitable in-sample (the learning cadence proposed it from
historical data) only counts as "real" if it ALSO improves ROI in
the held-out trades that came AFTER the proposal would have fired.

Usage
=====
  python -m tools.backtest                # baseline only
  python -m tools.backtest --skip crypto  # what if we'd skipped crypto?
  python -m tools.backtest --skip-band crypto:0.55-0.65
  python -m tools.backtest --multiplier crypto:0.5
  python -m tools.backtest --train 75 --test 25 --skip basketball

Walk-forward semantics
======================
  - Sort settled trades by `settled_at` ascending.
  - Partition into [train: 0..N), [test: N..N+W), then slide N forward
    by W. For each window:
      * apply the rule on the test slice only
      * compare ROI(test) under baseline vs under rule
  - Aggregate: a rule is considered "validated" when the rule's ROI
    beats baseline's ROI in >= 60% of windows AND the cumulative
    rule PnL across all test slices exceeds baseline by >= 1%.

Why walk-forward instead of one big train/test split: single splits
are sensitive to which 25 trades happen to land in test. Walk-forward
averages over many possible splits, surfacing rules that survive
across regimes vs rules that only worked in one slice.
"""

from __future__ import annotations

import argparse
import os
import sys
import sqlite3
from dataclasses import dataclass
from typing import Callable


def _default_db_path() -> str:
    """Resolve the same DB the sidecar uses (DELFI_DB_PATH override
    aware)."""
    override = os.environ.get("DELFI_DB_PATH")
    if override:
        return override
    home = os.path.expanduser("~")
    # Tauri AppData on macOS.
    candidate = os.path.join(
        home, "Library", "Application Support",
        "com.delfi.desktop", "delfi.db",
    )
    if os.path.exists(candidate):
        return candidate
    # Legacy fallback.
    return os.path.join(
        home, "Library", "Application Support", "Delfi", "delfi.db",
    )


def _load_settled(db_path: str) -> list[dict]:
    """Load all settled simulation trades, oldest first."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rs = con.execute(
        "SELECT id, market_archetype, side, entry_price, "
        "       claude_probability, cost_usd, realized_pnl_usd, "
        "       settled_at "
        "FROM pm_positions "
        "WHERE status='settled' AND mode='simulation' "
        "  AND cost_usd IS NOT NULL AND realized_pnl_usd IS NOT NULL "
        "ORDER BY settled_at ASC"
    )
    return [dict(r) for r in rs]


# ── Filter rules ─────────────────────────────────────────────────────────────

@dataclass
class Rule:
    """A rule transforms a sequence of trades into the subset/scaled
    version the bot would have produced under the rule.

    `apply` returns the modified row list. A `skip` rule omits rows;
    a `multiplier` rule scales (cost_usd, realized_pnl_usd) jointly so
    ROI is preserved but absolute PnL changes - matching how the
    sizer's archetype multiplier actually works.
    """
    name: str
    apply: Callable[[list[dict]], list[dict]]


def _rule_baseline() -> Rule:
    return Rule("baseline", lambda rows: list(rows))


def _rule_skip_archetype(arch: str) -> Rule:
    def f(rows):
        return [r for r in rows if r.get("market_archetype") != arch]
    return Rule(f"skip:{arch}", f)


def _rule_skip_price_band(arch: str, lo: float, hi: float) -> Rule:
    def f(rows):
        out = []
        for r in rows:
            if r.get("market_archetype") == arch:
                p = float(r.get("entry_price") or 0.0)
                if lo <= p < hi:
                    continue
            out.append(r)
        return out
    return Rule(f"skip-band:{arch}:{lo:.2f}-{hi:.2f}", f)


def _rule_multiplier(arch: str, mult: float) -> Rule:
    def f(rows):
        out = []
        for r in rows:
            if r.get("market_archetype") == arch:
                rr = dict(r)
                rr["cost_usd"] = float(r.get("cost_usd") or 0.0) * mult
                rr["realized_pnl_usd"] = (
                    float(r.get("realized_pnl_usd") or 0.0) * mult
                )
                out.append(rr)
            else:
                out.append(r)
        return out
    return Rule(f"mult:{arch}:{mult:.2f}x", f)


# ── Walk-forward driver ──────────────────────────────────────────────────────

def _roi_pct(rows: list[dict]) -> float:
    cost = sum(float(r.get("cost_usd") or 0.0) for r in rows)
    pnl = sum(float(r.get("realized_pnl_usd") or 0.0) for r in rows)
    return (pnl / cost * 100.0) if cost > 0 else 0.0


def walk_forward(
    rows: list[dict],
    rule: Rule,
    *,
    train: int = 75,
    test: int = 25,
) -> dict:
    """Run the walk-forward backtest for one rule.

    Returns:
        {
          "windows":      [{ start, end, roi_baseline, roi_rule, delta }, ...],
          "wins":         int,   # windows where rule beat baseline
          "windows_total": int,
          "win_rate":     float, # wins / total
          "cum_baseline_pnl": float,
          "cum_rule_pnl":     float,
          "cum_delta_pnl":    float,
          "validated":    bool,  # >=60% windows + >=1% cum delta
        }
    """
    n = len(rows)
    windows = []
    cum_b = cum_r = 0.0
    wins = 0
    i = train
    while i + test <= n:
        slice_rows = rows[i:i + test]
        rule_rows = rule.apply(slice_rows)
        b = _roi_pct(slice_rows)
        r = _roi_pct(rule_rows)
        b_pnl = sum(float(x.get("realized_pnl_usd") or 0.0) for x in slice_rows)
        r_pnl = sum(float(x.get("realized_pnl_usd") or 0.0) for x in rule_rows)
        cum_b += b_pnl
        cum_r += r_pnl
        if r > b:
            wins += 1
        windows.append({
            "start":         i,
            "end":           i + test,
            "roi_baseline":  round(b, 2),
            "roi_rule":      round(r, 2),
            "delta_pct":     round(r - b, 2),
            "baseline_pnl":  round(b_pnl, 2),
            "rule_pnl":      round(r_pnl, 2),
        })
        i += test

    total = len(windows)
    win_rate = (wins / total) if total else 0.0
    cum_delta = cum_r - cum_b
    validated = (
        win_rate >= 0.60
        and total >= 2
        and cum_delta > 0
        and (cum_delta / max(abs(cum_b), 1.0)) >= 0.01
    )
    return {
        "rule":              rule.name,
        "windows":           windows,
        "windows_total":     total,
        "wins":              wins,
        "win_rate":          round(win_rate, 2),
        "cum_baseline_pnl":  round(cum_b, 2),
        "cum_rule_pnl":      round(cum_r, 2),
        "cum_delta_pnl":     round(cum_delta, 2),
        "validated":         validated,
    }


def _print_result(res: dict) -> None:
    print(
        f"\n--- rule: {res['rule']}\n"
        f"   windows: {res['windows_total']}  wins: {res['wins']}/"
        f"{res['windows_total']}  win-rate: {res['win_rate']:.0%}\n"
        f"   cum baseline PnL: {res['cum_baseline_pnl']:+.2f}\n"
        f"   cum rule     PnL: {res['cum_rule_pnl']:+.2f}\n"
        f"   cum delta    PnL: {res['cum_delta_pnl']:+.2f}\n"
        f"   VALIDATED: {res['validated']}\n"
        f"   per-window:"
    )
    for w in res["windows"]:
        marker = "+" if w["delta_pct"] > 0 else "-"
        print(
            f"   [{marker}] window [{w['start']:>3}..{w['end']:>3})  "
            f"baseline={w['roi_baseline']:+6.2f}%  rule={w['roi_rule']:+6.2f}%  "
            f"delta={w['delta_pct']:+6.2f}%"
        )


def _parse_skip_band(s: str) -> Rule:
    # "crypto:0.55-0.65"
    arch, band = s.split(":", 1)
    lo_s, hi_s = band.split("-", 1)
    return _rule_skip_price_band(arch, float(lo_s), float(hi_s))


def _parse_multiplier(s: str) -> Rule:
    # "crypto:0.5"
    arch, m = s.split(":", 1)
    return _rule_multiplier(arch, float(m))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Walk-forward backtest of strategy rules against settled trades."
    )
    ap.add_argument("--db", default=_default_db_path(),
                    help="Path to delfi.db (defaults to the sidecar's DB).")
    ap.add_argument("--train", type=int, default=75,
                    help="Initial train-window size (rule kicks in after this many trades).")
    ap.add_argument("--test", type=int, default=25,
                    help="Out-of-sample window size.")
    ap.add_argument("--skip", action="append", default=[],
                    help="Skip an archetype entirely. Can repeat.")
    ap.add_argument("--skip-band", action="append", default=[],
                    metavar="ARCH:LO-HI",
                    help="Skip an archetype within a price band. Repeatable.")
    ap.add_argument("--multiplier", action="append", default=[],
                    metavar="ARCH:MULT",
                    help="Apply a stake multiplier to an archetype. Repeatable.")
    args = ap.parse_args(argv)

    rows = _load_settled(args.db)
    if len(rows) < args.train + args.test:
        print(
            f"[backtest] only {len(rows)} settled trades; need at least "
            f"{args.train + args.test} for one walk-forward window. "
            "Lower --train / --test or wait for more data."
        )
        return 1

    print(f"[backtest] loaded {len(rows)} settled trades from {args.db}")
    print(f"[backtest] train={args.train}, test={args.test}")

    # Build rule list. Always include baseline first.
    rules: list[Rule] = [_rule_baseline()]
    for a in args.skip:
        rules.append(_rule_skip_archetype(a))
    for s in args.skip_band:
        rules.append(_parse_skip_band(s))
    for m in args.multiplier:
        rules.append(_parse_multiplier(m))

    for rule in rules:
        res = walk_forward(rows, rule, train=args.train, test=args.test)
        _print_result(res)

    return 0


if __name__ == "__main__":
    sys.exit(main())
