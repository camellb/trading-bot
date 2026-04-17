"""
Polymarket parameter-sensitivity backtester.

Replays past predictions from the database through the sizer with
configurable parameters to answer "what if?" questions:

  - What if min_edge were 300 bps instead of 500?
  - What if we used half-Kelly instead of quarter?
  - What if max_trade were $50 instead of $25?

Usage:
    python -m backtester.pm_backtest
    python -m backtester.pm_backtest --min-edge 300 --kelly 0.5
    python -m backtester.pm_backtest --sweep          # grid search

This does NOT call Claude. It replays historical evaluations that
already happened, re-running the sizing math with alternate parameters.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from sqlalchemy import create_engine, text


@dataclass
class BacktestConfig:
    min_edge_bps: float = 500.0
    min_confidence: float = 0.55
    kelly_fraction: float = 0.25
    max_position_pct: float = 0.05
    min_trade_usd: float = 2.0
    max_trade_usd: float = 25.0
    starting_cash: float = 500.0
    fee_pct: float = 0.02

    def label(self) -> str:
        return (f"edge≥{self.min_edge_bps:.0f}bps "
                f"conf≥{self.min_confidence:.2f} "
                f"kelly={self.kelly_fraction:.2f} "
                f"max${self.max_trade_usd:.0f}")


@dataclass
class Trade:
    prediction_id: int
    question: str
    side: str
    entry_price: float
    shares: float
    cost_usd: float
    edge: float
    claude_p: float
    confidence: float
    resolved: bool
    outcome: Optional[int]
    pnl_usd: Optional[float]


def _size(mp_yes: float, claude_p: float, conf: float,
          bankroll: float, cfg: BacktestConfig) -> Optional[dict]:
    mp = max(0.0, min(1.0, mp_yes))
    cp = max(0.0, min(1.0, claude_p))
    cf = max(0.0, min(1.0, conf))

    if cp > mp:
        side, entry, win_payoff = "YES", mp, 1.0 - mp
    else:
        side, entry, win_payoff = "NO", 1.0 - mp, mp

    edge = abs(cp - mp)

    if edge * 10_000.0 < cfg.min_edge_bps:
        return None
    if cf < cfg.min_confidence:
        return None
    if entry <= 0.02 or entry >= 0.98:
        return None

    kelly_full = edge / win_payoff if win_payoff > 1e-6 else 0.0
    kelly_full = max(0.0, min(1.0, kelly_full))
    fraction = kelly_full * cfg.kelly_fraction * cf
    fraction = min(fraction, cfg.max_position_pct)
    stake = bankroll * fraction

    if stake < cfg.min_trade_usd:
        return None
    stake = min(stake, cfg.max_trade_usd)
    shares = stake / entry if entry > 0 else 0.0

    return {"side": side, "entry": entry, "edge": edge,
            "stake": stake, "shares": shares}


def load_evaluations() -> list[dict]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    eng = create_engine(url)
    with eng.begin() as conn:
        rows = conn.execute(text("""
            SELECT me.id, me.market_id, me.question, me.category,
                   me.market_price_yes, me.claude_probability, me.confidence,
                   me.edge_bps, me.recommendation, me.pm_position_id,
                   p.id AS pred_id, p.resolved_outcome, p.probability
            FROM market_evaluations me
            LEFT JOIN predictions p ON p.id = (
                SELECT pp.prediction_id FROM pm_positions pp
                WHERE pp.id = me.pm_position_id
            )
            WHERE me.claude_probability IS NOT NULL
              AND me.market_price_yes IS NOT NULL
              AND me.confidence IS NOT NULL
            ORDER BY me.evaluated_at ASC
        """)).fetchall()
    evals = []
    for r in rows:
        evals.append({
            "eval_id": r[0], "market_id": r[1], "question": r[2],
            "category": r[3], "market_price_yes": float(r[4]),
            "claude_p": float(r[5]), "confidence": float(r[6]),
            "edge_bps": float(r[7]) if r[7] else 0,
            "recommendation": r[8], "position_id": r[9],
            "prediction_id": r[10],
            "resolved_outcome": int(r[11]) if r[11] is not None else None,
        })
    return evals


def run_backtest(evals: list[dict], cfg: BacktestConfig) -> dict:
    bankroll = cfg.starting_cash
    trades: list[Trade] = []
    seen_markets: set[str] = set()

    for ev in evals:
        mid = ev["market_id"]
        if mid in seen_markets:
            continue

        result = _size(ev["market_price_yes"], ev["claude_p"],
                       ev["confidence"], bankroll, cfg)
        if result is None:
            continue

        seen_markets.add(mid)
        cost = result["stake"]
        cost_with_fee = cost * (1.0 + cfg.fee_pct)
        bankroll -= cost_with_fee

        resolved = ev["resolved_outcome"] is not None
        pnl = None
        if resolved:
            won = ((result["side"] == "YES" and ev["resolved_outcome"] == 1)
                   or (result["side"] == "NO" and ev["resolved_outcome"] == 0))
            proceeds = result["shares"] * 1.0 if won else 0.0
            proceeds_after_fee = proceeds * (1.0 - cfg.fee_pct)
            pnl = proceeds_after_fee - cost_with_fee
            bankroll += proceeds_after_fee

        trades.append(Trade(
            prediction_id=ev.get("prediction_id"),
            question=ev["question"],
            side=result["side"], entry_price=result["entry"],
            shares=result["shares"], cost_usd=cost,
            edge=result["edge"], claude_p=ev["claude_p"],
            confidence=ev["confidence"],
            resolved=resolved, outcome=ev["resolved_outcome"], pnl_usd=pnl,
        ))

    resolved_trades = [t for t in trades if t.resolved]
    wins = [t for t in resolved_trades if t.pnl_usd is not None and t.pnl_usd > 0]
    total_pnl = sum(t.pnl_usd for t in resolved_trades if t.pnl_usd is not None)
    total_cost = sum(t.cost_usd for t in trades)
    open_cost = sum(t.cost_usd for t in trades if not t.resolved)

    return {
        "config": cfg.label(),
        "total_evaluated": len(evals),
        "trades_taken": len(trades),
        "trades_resolved": len(resolved_trades),
        "trades_open": len(trades) - len(resolved_trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(resolved_trades) if resolved_trades else None,
        "total_pnl": total_pnl,
        "total_cost": total_cost,
        "open_exposure": open_cost,
        "final_bankroll": bankroll,
        "roi_pct": (total_pnl / cfg.starting_cash * 100) if resolved_trades else None,
        "trades": trades,
    }


def print_report(result: dict):
    cfg_str = result["config"]
    print(f"\n{'=' * 70}")
    print(f"  Config: {cfg_str}")
    print(f"{'=' * 70}")
    print(f"  Markets evaluated:  {result['total_evaluated']}")
    print(f"  Trades taken:       {result['trades_taken']}")
    print(f"  Trades resolved:    {result['trades_resolved']}")
    print(f"  Trades open:        {result['trades_open']}")
    print(f"  Wins:               {result['wins']}")
    wr = result['win_rate']
    print(f"  Win rate:           {wr:.1%}" if wr is not None else "  Win rate:           —")
    print(f"  Total P&L:          ${result['total_pnl']:+.2f}")
    print(f"  Open exposure:      ${result['open_exposure']:.2f}")
    print(f"  Final bankroll:     ${result['final_bankroll']:.2f}")
    roi = result['roi_pct']
    print(f"  ROI:                {roi:+.1f}%" if roi is not None else "  ROI:                —")

    if result["trades"]:
        print(f"\n  {'#':<4} {'SIDE':<4} {'ENTRY':>6} {'COST':>8} {'EDGE':>6} "
              f"{'P&L':>8} {'QUESTION'}")
        print(f"  {'—'*4} {'—'*4} {'—'*6} {'—'*8} {'—'*6} {'—'*8} {'—'*40}")
        for i, t in enumerate(result["trades"], 1):
            pnl_str = f"${t.pnl_usd:+.2f}" if t.pnl_usd is not None else "open"
            print(f"  {i:<4} {t.side:<4} {t.entry_price:>6.3f} "
                  f"${t.cost_usd:>7.2f} {t.edge*10000:>5.0f}bp "
                  f"{pnl_str:>8} {t.question[:40]}")
    print()


SWEEP_GRID = {
    "min_edge_bps": [300, 500, 750, 1000],
    "kelly_fraction": [0.125, 0.25, 0.5],
    "min_confidence": [0.5, 0.55, 0.6, 0.7],
    "max_trade_usd": [15, 25, 50],
}


def run_sweep(evals: list[dict]):
    print(f"\nSweeping {len(SWEEP_GRID['min_edge_bps'])} x "
          f"{len(SWEEP_GRID['kelly_fraction'])} x "
          f"{len(SWEEP_GRID['min_confidence'])} x "
          f"{len(SWEEP_GRID['max_trade_usd'])} = "
          f"{len(SWEEP_GRID['min_edge_bps']) * len(SWEEP_GRID['kelly_fraction']) * len(SWEEP_GRID['min_confidence']) * len(SWEEP_GRID['max_trade_usd'])} configs\n")

    results = []
    for edge in SWEEP_GRID["min_edge_bps"]:
        for kelly in SWEEP_GRID["kelly_fraction"]:
            for conf in SWEEP_GRID["min_confidence"]:
                for max_t in SWEEP_GRID["max_trade_usd"]:
                    cfg = BacktestConfig(
                        min_edge_bps=edge, kelly_fraction=kelly,
                        min_confidence=conf, max_trade_usd=max_t,
                    )
                    r = run_backtest(evals, cfg)
                    results.append(r)

    results.sort(key=lambda r: r["total_pnl"], reverse=True)

    print(f"{'EDGE':>6} {'KELLY':>6} {'CONF':>5} {'MAX$':>5} "
          f"{'TRADES':>6} {'WINS':>5} {'WR':>6} {'P&L':>8} {'BANKROLL':>10}")
    print("—" * 70)
    for r in results[:20]:
        cfg_parts = r["config"].split()
        edge_s = cfg_parts[0].split("≥")[1]
        kelly_s = cfg_parts[2].split("=")[1]
        conf_s = cfg_parts[1].split("≥")[1]
        max_s = cfg_parts[3].replace("max$", "")
        wr = f"{r['win_rate']:.0%}" if r['win_rate'] is not None else "—"
        print(f"{edge_s:>6} {kelly_s:>6} {conf_s:>5} ${max_s:>4} "
              f"{r['trades_taken']:>6} {r['wins']:>5} {wr:>6} "
              f"${r['total_pnl']:>+7.2f} ${r['final_bankroll']:>9.2f}")

    print(f"\n(showing top 20 of {len(results)} configs by P&L)")


def main():
    parser = argparse.ArgumentParser(description="PM parameter-sensitivity backtester")
    parser.add_argument("--min-edge", type=float, default=500, help="Min edge in bps")
    parser.add_argument("--min-conf", type=float, default=0.55, help="Min confidence")
    parser.add_argument("--kelly", type=float, default=0.25, help="Kelly fraction")
    parser.add_argument("--max-trade", type=float, default=25, help="Max trade USD")
    parser.add_argument("--max-pos-pct", type=float, default=0.05, help="Max position %")
    parser.add_argument("--starting-cash", type=float, default=500, help="Starting cash")
    parser.add_argument("--fee", type=float, default=0.02, help="Fee per side (0.02 = 2%)")
    parser.add_argument("--sweep", action="store_true", help="Run parameter grid search")
    args = parser.parse_args()

    print("Loading evaluations from database...", flush=True)
    evals = load_evaluations()
    print(f"Found {len(evals)} evaluations.\n")

    if not evals:
        print("No evaluations found. Run the scanner first.", file=sys.stderr)
        sys.exit(1)

    if args.sweep:
        run_sweep(evals)
    else:
        cfg = BacktestConfig(
            min_edge_bps=args.min_edge,
            min_confidence=args.min_conf,
            kelly_fraction=args.kelly,
            max_trade_usd=args.max_trade,
            max_position_pct=args.max_pos_pct,
            starting_cash=args.starting_cash,
            fee_pct=args.fee,
        )
        result = run_backtest(evals, cfg)
        print_report(result)


if __name__ == "__main__":
    main()
