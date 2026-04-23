"""
Forecast backtester shared by the learning cadence and the full
backtest validation pass.

Replays historical market evaluations through the real three-gate sizer
with a caller-supplied UserConfig, then measures the hypothetical
outcomes against the resolved predictions. This does not call Claude -
the language-model forecast happened at evaluation time and is stored.

Sizing uses the sizer's cost model (spread + fees + slippage) so every
simulation reflects the same COST_ASSUMPTION as live execution.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

from execution.pm_sizer import COST_ASSUMPTION, size_position
from engine.user_config import UserConfig


@dataclass
class Evaluation:
    """Minimal shape the backtester needs. Matches market_evaluations join."""
    market_id:           str
    market_price_yes:    float
    claude_probability:  float
    confidence:          float
    category:            Optional[str]
    resolved_outcome:    Optional[int]   # 1 = YES resolved true, 0 = NO


@dataclass
class SimulatedTrade:
    market_id:       str
    side:            str
    entry_price:     float
    stake_usd:       float
    ev:              float
    resolved:        bool
    outcome:         Optional[int]
    pnl_usd:         Optional[float]
    category:        Optional[str]


def simulate_with_config(
    evaluations:   list[Evaluation],
    user_config:   UserConfig,
    starting_cash: float = 1000.0,
) -> dict:
    """
    Replay the evaluations through the new sizer + cost model. Returns a
    report dict with ROI, win rate, P&L, and trade list.
    """
    bankroll = starting_cash
    trades: list[SimulatedTrade] = []
    seen: set[str] = set()
    skip_tally: dict[str, int] = {
        "direction": 0, "p_win": 0, "return": 0,
        "confidence": 0, "archetype": 0, "other": 0,
    }
    evaluated_unique = 0

    for ev in evaluations:
        if ev.market_id in seen:
            continue
        evaluated_unique += 1
        # Approximate ask_no from ask_yes since historical evaluations only
        # persist yes_price. The doctrine's EV formula doesn't require a
        # separate NO ask beyond this - over-approximating slightly narrows
        # NO-side EV which is the conservative choice.
        ask_no = max(1e-6, 1.0 - ev.market_price_yes)
        decision = size_position(
            claude_p    = ev.claude_probability,
            confidence  = ev.confidence,
            ask_yes     = ev.market_price_yes,
            ask_no      = ask_no,
            bankroll    = bankroll * (1.0 - user_config.dry_powder_reserve_pct),
            user_config = user_config,
            archetype   = ev.category,
        )
        if not decision.should_trade:
            skip_tally[_classify_skip_reason(decision.skip_reason)] += 1
            continue
        seen.add(ev.market_id)

        cost = decision.stake_usd
        bankroll -= cost

        resolved = ev.resolved_outcome is not None
        pnl = None
        if resolved:
            won = (
                (decision.side == "YES" and ev.resolved_outcome == 1) or
                (decision.side == "NO"  and ev.resolved_outcome == 0)
            )
            # Settlement at $1.00 × shares for winners, $0 for losers.
            proceeds = decision.shares * (1.0 if won else 0.0)
            # Apply the exit-side cost too - the sizer's COST_ASSUMPTION is
            # already a round-trip estimate, so only the one side is counted
            # here; subtracting it twice would double-count. Use the same
            # cost model the sizer used at entry.
            pnl = proceeds - cost
            bankroll += proceeds

        trades.append(SimulatedTrade(
            market_id=ev.market_id, side=decision.side,
            entry_price=decision.entry_price, stake_usd=cost,
            ev=decision.ev,
            resolved=resolved, outcome=ev.resolved_outcome, pnl_usd=pnl,
            category=ev.category,
        ))

    resolved_trades = [t for t in trades if t.resolved]
    wins = [t for t in resolved_trades if t.pnl_usd is not None and t.pnl_usd > 0]
    total_pnl = sum(t.pnl_usd for t in resolved_trades if t.pnl_usd is not None)
    total_cost = sum(t.stake_usd for t in trades)

    win_rate = (len(wins) / len(resolved_trades)) if resolved_trades else None
    roi_pct = (total_pnl / starting_cash) if resolved_trades else None

    return {
        "starting_cash":     starting_cash,
        "final_bankroll":    bankroll,
        "evaluated_unique":  evaluated_unique,
        "trades_taken":      len(trades),
        "trades_resolved":   len(resolved_trades),
        "trades_open":       len(trades) - len(resolved_trades),
        "wins":              len(wins),
        "win_rate":          win_rate,
        "total_pnl":         total_pnl,
        "total_cost":        total_cost,
        "roi":               roi_pct,
        "trades":            trades,
        "cost_assumption":   COST_ASSUMPTION,
        "ev_buckets":        ev_bucket_distribution(trades),
        "by_archetype":      archetype_distribution(trades),
        "skip_tally":        skip_tally,
        "stake_distribution": stake_distribution(trades),
    }


def _classify_skip_reason(reason: Optional[str]) -> str:
    """Map free-text skip_reason from the sizer into tally buckets."""
    if not reason:
        return "other"
    r = reason.lower()
    if "direction disagreement" in r or "no direction" in r:
        return "direction"
    if "p_win" in r and "below min_p_win" in r:
        return "p_win"
    if "expected return" in r and "below" in r:
        return "return"
    if "confidence" in r and "below confidence_skip_floor" in r:
        return "confidence"
    if "skip list" in r:
        return "archetype"
    return "other"


def stake_distribution(trades: list[SimulatedTrade]) -> dict:
    """Summarize stake sizes under the confidence softener."""
    stakes = [t.stake_usd for t in trades]
    if not stakes:
        return {"n": 0, "min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0,
                "p25": 0.0, "p75": 0.0}
    s = sorted(stakes)
    def q(frac: float) -> float:
        idx = min(len(s) - 1, int(frac * (len(s) - 1)))
        return s[idx]
    return {
        "n":     len(s),
        "min":   s[0],
        "max":   s[-1],
        "mean":  sum(s) / len(s),
        "median": q(0.50),
        "p25":   q(0.25),
        "p75":   q(0.75),
    }


# ── Distribution analytics (Phase 5) ─────────────────────────────────────────
# EV buckets come straight from the doctrine's report template.
EV_BUCKETS: list[tuple[str, float, float]] = [
    ("3-5%",  0.03, 0.05),
    ("5-10%", 0.05, 0.10),
    ("10-20%", 0.10, 0.20),
    ("20%+",  0.20, float("inf")),
]


def ev_bucket_distribution(trades: list[SimulatedTrade]) -> list[dict]:
    """Group simulated trades by entry EV and roll up n / wins / P&L."""
    out: list[dict] = []
    for label, lo, hi in EV_BUCKETS:
        bucket = [t for t in trades if lo <= t.ev < hi]
        resolved = [t for t in bucket if t.resolved]
        wins = [t for t in resolved if t.pnl_usd is not None and t.pnl_usd > 0]
        pnl = sum(t.pnl_usd for t in resolved if t.pnl_usd is not None)
        cost = sum(t.stake_usd for t in bucket)
        out.append({
            "bucket":   label,
            "ev_lo":    lo,
            "ev_hi":    hi if hi != float("inf") else None,
            "n":        len(bucket),
            "resolved": len(resolved),
            "wins":     len(wins),
            "win_rate": (len(wins) / len(resolved)) if resolved else None,
            "pnl":      pnl,
            "cost":     cost,
            "roi":      (pnl / cost) if cost else None,
        })
    return out


def archetype_distribution(trades: list[SimulatedTrade]) -> list[dict]:
    """Group simulated trades by category (archetype)."""
    buckets: dict[str, dict] = {}
    for t in trades:
        cat = t.category or "other"
        b = buckets.setdefault(cat, {"category": cat, "n": 0, "resolved": 0,
                                     "wins": 0, "pnl": 0.0, "cost": 0.0})
        b["n"]    += 1
        b["cost"] += t.stake_usd
        if t.resolved:
            b["resolved"] += 1
            if t.pnl_usd is not None:
                b["pnl"] += t.pnl_usd
                if t.pnl_usd > 0:
                    b["wins"] += 1
    out = []
    for b in buckets.values():
        b["win_rate"] = (b["wins"] / b["resolved"]) if b["resolved"] else None
        b["roi"] = (b["pnl"] / b["cost"]) if b["cost"] else None
        out.append(b)
    out.sort(key=lambda x: -x["n"])
    return out


def format_phase5_report(result: dict, old_trade_count: int = 35) -> str:
    """
    Pretty-printed report matching the Phase 5 spec: trade count vs the
    old 35, win rate, P&L, ROI, EV bucket breakdown, archetype breakdown.
    """
    lines = []
    lines.append("=" * 70)
    lines.append("  Phase 5 - EV backtester validation (90 days, default UserConfig)")
    lines.append("=" * 70)

    trades_taken = result.get("trades_taken", 0)
    trades_resolved = result.get("trades_resolved", 0)
    wins = result.get("wins", 0)
    win_rate = result.get("win_rate")
    pnl = result.get("total_pnl", 0.0)
    roi = result.get("roi")
    starting = result.get("starting_cash", 0.0)

    lines.append(f"  Trades taken (new sizer):   {trades_taken}")
    lines.append(f"  Trades taken (old sizer):   {old_trade_count}")
    lines.append(f"  Trades resolved:            {trades_resolved}")
    lines.append(f"  Trades open:                "
                 f"{result.get('trades_open', 0)}")
    lines.append(f"  Wins:                       {wins}")
    if win_rate is not None:
        lines.append(f"  Win rate:                   {win_rate*100:.1f}%")
    lines.append(f"  Total P&L:                  ${pnl:+.2f}")
    if roi is not None:
        lines.append(f"  ROI (vs ${starting:.0f} starting): {roi*100:+.1f}%")

    lines.append("")
    lines.append("  EV bucket distribution")
    lines.append("  " + "-" * 66)
    lines.append(f"  {'Bucket':<8} {'N':>5} {'Res':>5} {'Wins':>5} "
                 f"{'WinRt':>7} {'P&L':>10} {'ROI':>8}")
    for b in result.get("ev_buckets", []):
        wr = f"{b['win_rate']*100:.0f}%" if b["win_rate"] is not None else "-"
        br = f"{b['roi']*100:+.1f}%" if b["roi"] is not None else "-"
        lines.append(
            f"  {b['bucket']:<8} {b['n']:>5} {b['resolved']:>5} "
            f"{b['wins']:>5} {wr:>7} ${b['pnl']:>+8.2f} {br:>8}"
        )

    lines.append("")
    lines.append("  Archetype distribution")
    lines.append("  " + "-" * 66)
    lines.append(f"  {'Category':<20} {'N':>5} {'Res':>5} {'Wins':>5} "
                 f"{'WinRt':>7} {'P&L':>10} {'ROI':>8}")
    for b in result.get("by_archetype", []):
        wr = f"{b['win_rate']*100:.0f}%" if b["win_rate"] is not None else "-"
        br = f"{b['roi']*100:+.1f}%" if b["roi"] is not None else "-"
        lines.append(
            f"  {b['category'][:20]:<20} {b['n']:>5} {b['resolved']:>5} "
            f"{b['wins']:>5} {wr:>7} ${b['pnl']:>+8.2f} {br:>8}"
        )

    tally = result.get("skip_tally") or {}
    if tally:
        total_skips = sum(tally.values())
        lines.append("")
        lines.append("  Skip-reason breakdown")
        lines.append("  " + "-" * 66)
        lines.append(f"  {'Reason':<14} {'N':>6} {'Share':>8}")
        order = ["direction", "p_win", "return", "confidence",
                 "archetype", "other"]
        for key in order:
            n = tally.get(key, 0)
            share = (n / total_skips * 100) if total_skips else 0.0
            lines.append(f"  {key:<14} {n:>6} {share:>7.1f}%")
        lines.append(f"  {'TOTAL':<14} {total_skips:>6}")
        evaluated = result.get("evaluated_unique")
        if evaluated:
            lines.append(f"  (Evaluated unique markets: {evaluated}; "
                         f"traded {result.get('trades_taken', 0)}, "
                         f"skipped {total_skips})")

    stakes = result.get("stake_distribution") or {}
    if stakes.get("n"):
        lines.append("")
        lines.append("  Stake distribution under confidence softener")
        lines.append("  " + "-" * 66)
        lines.append(f"  n={stakes['n']}  "
                     f"min=${stakes['min']:.2f}  "
                     f"p25=${stakes['p25']:.2f}  "
                     f"median=${stakes['median']:.2f}  "
                     f"p75=${stakes['p75']:.2f}  "
                     f"max=${stakes['max']:.2f}  "
                     f"mean=${stakes['mean']:.2f}")

    lines.append("=" * 70)
    lines.append("  Decision rule:")
    lines.append("    - Meaningfully positive ROI → resume simulation trading.")
    lines.append("    - Near-zero or negative ROI → forecaster itself needs work;")
    lines.append("      no sizing paradigm can fix a bad forecast.")
    lines.append("=" * 70)
    return "\n".join(lines)


def load_evaluations(since_days: Optional[int] = None) -> list[Evaluation]:
    """Pull evaluations joined to their prediction's resolved_outcome."""
    from sqlalchemy import text
    from db.engine import get_engine

    since_clause = ""
    params: dict = {}
    if since_days is not None:
        since_clause = " AND me.evaluated_at >= NOW() - (:d || ' days')::interval "
        params["d"] = str(int(since_days))

    with get_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT me.market_id, me.market_price_yes, me.claude_probability, "
            "       me.confidence, me.category, p.resolved_outcome "
            "FROM market_evaluations me "
            "LEFT JOIN predictions p ON p.id = ("
            "    SELECT pp.prediction_id FROM pm_positions pp "
            "    WHERE pp.id = me.pm_position_id"
            ") "
            "WHERE me.claude_probability IS NOT NULL "
            "  AND me.market_price_yes IS NOT NULL "
            "  AND me.confidence IS NOT NULL "
            f"  {since_clause}"
            "ORDER BY me.evaluated_at ASC"
        ), params).fetchall()

    evals: list[Evaluation] = []
    for r in rows:
        evals.append(Evaluation(
            market_id          = str(r[0]),
            market_price_yes   = float(r[1]),
            claude_probability = float(r[2]),
            confidence         = float(r[3]),
            category           = r[4],
            resolved_outcome   = int(r[5]) if r[5] is not None else None,
        ))
    return evals


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main() -> None:
    import argparse
    from dotenv import load_dotenv
    load_dotenv(override=True)

    from engine.user_config import get_user_config, DEFAULT_USER_ID

    parser = argparse.ArgumentParser(
        description="Phase 5 - run the EV backtester over recent evaluations.")
    parser.add_argument("--days", type=int, default=90,
                        help="How many days of history to replay (default: 90)")
    parser.add_argument("--starting-cash", type=float, default=1000.0,
                        help="Starting bankroll in USD (default: 1000)")
    parser.add_argument("--user-id", type=str, default=DEFAULT_USER_ID,
                        help="Which user_config row to use (default: default)")
    parser.add_argument("--old-trade-count", type=int, default=35,
                        help="Old sizer's trade count for the comparison line")
    args = parser.parse_args()

    print(f"Loading evaluations from last {args.days} days…", flush=True)
    evals = load_evaluations(since_days=args.days)
    print(f"Loaded {len(evals)} evaluations.\n", flush=True)

    if not evals:
        print("No evaluations found. Run the scanner first, or expand --days.")
        sys.exit(1)

    user_config = get_user_config(args.user_id)
    result = simulate_with_config(
        evals, user_config=user_config, starting_cash=args.starting_cash,
    )
    print(format_phase5_report(result, old_trade_count=args.old_trade_count))


if __name__ == "__main__":
    _main()
