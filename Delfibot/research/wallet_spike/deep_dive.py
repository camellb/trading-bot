"""
Deep dive on the top-5 wallets by 30-day ROI.

Reads `wallets.csv` + `trades.csv` from pull_wallets.py to find the
top-5 ROI wallets, then re-pulls 90 days of their closed positions
fresh from Polymarket and computes:

    1. 90d totals (trades, win rate, cost, P&L, ROI)
    2. Recent-30d vs earlier-60d split (out-of-sample stability)
    3. Weekly P&L curve per wallet (concentration check)
    4. Mean-entry-price bands per wallet (where the edge is)
    5. Top 5 trades by abs(P&L) (concentration spotlight)

Run from this directory:

    python3 deep_dive.py

Outputs: `deep_dive_report.md` (markdown table + per-wallet detail).
"""

from __future__ import annotations

import csv
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


HERE = Path(__file__).parent
WALLETS_CSV = HERE / "wallets.csv"
TRADES_CSV = HERE / "trades.csv"
RAW_DIR = HERE / "raw"
REPORT_PATH = HERE / "deep_dive_report.md"

CLOSED_POSITIONS_URL = "https://data-api.polymarket.com/closed-positions"

USER_AGENT = "delfi-research/1.0"
THROTTLE_SECONDS = 0.1
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0

WINDOW_DAYS = 90
RECENT_DAYS = 30
MIN_SAMPLE = 20
PER_PAGE = 50
TOP_K = 5


def http_get_json(url: str, params: dict) -> object:
    full_url = f"{url}?{urlencode(params)}"
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(full_url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429 and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
            print(f"  http {exc.code} on {full_url}", file=sys.stderr)
            return None
        except (URLError, json.JSONDecodeError, TimeoutError):
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
            return None
    return None


def _row_ts(row: dict) -> float | None:
    raw = row.get("timestamp")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) / 1000.0 if raw > 1e12 else float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def fetch_closed_positions(wallet: str, window_days: int) -> list[dict]:
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=window_days)).timestamp()
    all_rows: list[dict] = []
    offset = 0
    while True:
        page = http_get_json(CLOSED_POSITIONS_URL, {
            "user":          wallet,
            "limit":         str(PER_PAGE),
            "offset":        str(offset),
            "sortBy":        "TIMESTAMP",
            "sortDirection": "DESC",
        })
        if not isinstance(page, list) or not page:
            break
        all_rows.extend(page)
        oldest_ts = _row_ts(page[-1])
        if oldest_ts is not None and oldest_ts < cutoff_ts:
            break
        offset += len(page)
        if offset > 5000:
            break
        time.sleep(THROTTLE_SECONDS)
    return [r for r in all_rows if (_row_ts(r) or 0) >= cutoff_ts]


@dataclass
class TradeRow:
    wallet:       str
    title:        str
    outcome:      str
    avg_price:    float | None
    cost:         float
    pnl:          float
    timestamp:    datetime


def to_trade_row(wallet: str, raw: dict) -> TradeRow | None:
    try:
        ts_unix = _row_ts(raw)
        if ts_unix is None:
            return None
        ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
        return TradeRow(
            wallet=wallet,
            title=(raw.get("title") or "").strip()[:120],
            outcome=(raw.get("outcome") or "").upper(),
            avg_price=float(raw["avgPrice"]) if raw.get("avgPrice") not in (None, "") else None,
            cost=float(raw.get("totalBought") or 0),
            pnl=float(raw.get("realizedPnl") or 0),
            timestamp=ts,
        )
    except (ValueError, KeyError, TypeError):
        return None


def aggregate(trades: list[TradeRow]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "cost": 0.0, "pnl": 0.0, "roi": 0.0, "mean_entry": None}
    wins = sum(1 for t in trades if t.pnl > 0)
    cost = sum(t.cost for t in trades)
    pnl = sum(t.pnl for t in trades)
    pc = [(t.avg_price, t.cost) for t in trades if t.avg_price is not None and t.cost > 0]
    mean_entry = (sum(p * c for p, c in pc) / sum(c for _, c in pc)) if pc else None
    return {
        "n":          n,
        "wins":       wins,
        "losses":     n - wins,
        "win_rate":   wins / n,
        "cost":       cost,
        "pnl":        pnl,
        "roi":        pnl / cost if cost > 0 else 0.0,
        "mean_entry": mean_entry,
    }


def weekly_breakdown(trades: list[TradeRow]) -> list[dict]:
    by_week: dict[str, list[TradeRow]] = defaultdict(list)
    for t in trades:
        key = t.timestamp.strftime("%G-W%V")
        by_week[key].append(t)
    out: list[dict] = []
    for key in sorted(by_week.keys()):
        rows = by_week[key]
        agg = aggregate(rows)
        out.append({"week": key, **agg})
    return out


def price_band_breakdown(trades: list[TradeRow]) -> list[dict]:
    bands = [
        ("0.00-0.10", 0.00, 0.10),
        ("0.10-0.30", 0.10, 0.30),
        ("0.30-0.50", 0.30, 0.50),
        ("0.50-0.70", 0.50, 0.70),
        ("0.70-0.90", 0.70, 0.90),
        ("0.90-1.00", 0.90, 1.01),
    ]
    out: list[dict] = []
    for label, lo, hi in bands:
        rows = [t for t in trades if t.avg_price is not None and lo <= t.avg_price < hi]
        if not rows:
            continue
        agg = aggregate(rows)
        out.append({"band": label, **agg})
    return out


def top_market_concentration(trades: list[TradeRow], k: int = 5) -> tuple[float, list[TradeRow]]:
    if not trades:
        return 0.0, []
    abs_total = sum(abs(t.pnl) for t in trades) or 1.0
    top = sorted(trades, key=lambda t: abs(t.pnl), reverse=True)[:k]
    top_share = sum(abs(t.pnl) for t in top) / abs_total
    return top_share, top


def split_window(trades: list[TradeRow], recent_days: int) -> tuple[list[TradeRow], list[TradeRow]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    earlier = [t for t in trades if t.timestamp < cutoff]
    recent  = [t for t in trades if t.timestamp >= cutoff]
    return earlier, recent


def fmt_pct(v: float, d: int = 1) -> str:
    return f"{'+' if v >= 0 else ''}{v * 100:.{d}f}%"


def fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1000:
        return f"{sign}${a:,.0f}"
    return f"{sign}${a:.2f}"


def load_top_wallets(k: int) -> list[tuple[str, str]]:
    """Re-rank top wallets by 30d ROI computed from trades.csv (not by leaderboard volume)."""
    if not WALLETS_CSV.exists():
        print(f"error: {WALLETS_CSV.name} not found - run pull_wallets.py first", file=sys.stderr)
        sys.exit(1)
    if not TRADES_CSV.exists():
        print(f"error: {TRADES_CSV.name} not found - run pull_wallets.py first", file=sys.stderr)
        sys.exit(1)
    by_wallet: dict[str, dict] = {}
    with open(WALLETS_CSV) as f:
        for row in csv.DictReader(f):
            by_wallet[row["proxyWallet"]] = {
                "name": row.get("userName", "") or row["proxyWallet"][:10] + "…",
            }
    roi: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "pnl": 0.0, "n": 0})
    with open(TRADES_CSV) as f:
        for row in csv.DictReader(f):
            w = row["wallet"]
            try:
                roi[w]["cost"] += float(row.get("totalBought") or 0)
                roi[w]["pnl"]  += float(row.get("realizedPnl") or 0)
                roi[w]["n"]    += 1
            except ValueError:
                continue
    rankable = [
        (w, roi[w]["pnl"] / roi[w]["cost"] if roi[w]["cost"] > 0 else 0.0)
        for w in roi if roi[w]["n"] >= MIN_SAMPLE
    ]
    rankable.sort(key=lambda p: p[1], reverse=True)
    return [(w, by_wallet.get(w, {}).get("name", w[:10] + "…")) for w, _ in rankable[:k]]


def render_report(per_wallet: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Top-5 wallet deep dive (90-day window)\n")
    lines.append(f"Generated {datetime.now(timezone.utc).isoformat()}.\n")
    lines.append("Top-5 by trailing 30d ROI from the broader spike, "
                 "extended back to 90 days for stability + out-of-sample evidence.\n")

    lines.append("## Stability check: 30d (recent) vs 60d (earlier)\n")
    lines.append("If the wallet's edge is real, ROI in the earlier 60d window should be "
                 "in the same ballpark as the recent 30d. If it's lookback bias, you'll "
                 "see a big regression.\n")
    lines.append("| Wallet | 90d N | 90d ROI | recent 30d ROI | earlier 60d ROI | Δ (recent − earlier) |")
    lines.append("|---|---|---|---|---|---|")
    for w in per_wallet:
        full = w["full"]
        rec = w["recent"]
        ear = w["earlier"]
        delta = rec["roi"] - ear["roi"]
        lines.append(
            f"| `{w['wallet'][:12]}…` ({w['name'][:20]}) | {full['n']} | "
            f"{fmt_pct(full['roi'])} | {fmt_pct(rec['roi'])} | {fmt_pct(ear['roi'])} | "
            f"{fmt_pct(delta)} |"
        )
    lines.append("")

    for w in per_wallet:
        lines.append(f"## {w['wallet']} ({w['name']})\n")
        full = w["full"]
        if full["n"] == 0:
            lines.append("(No 90d trades — wallet is either new or paused.)\n")
            continue
        if full["mean_entry"] is not None:
            lines.append(
                f"**90 days · {full['n']} trades · {fmt_money(full['cost'])} cost · "
                f"{fmt_money(full['pnl'])} P&L · "
                f"{fmt_pct(full['roi'])} ROI · "
                f"{full['win_rate']*100:.1f}% win rate · "
                f"mean entry {full['mean_entry']:.2f}**\n"
            )
        else:
            lines.append(f"**90d · {full['n']} trades · {fmt_pct(full['roi'])} ROI**\n")

        rec, ear = w["recent"], w["earlier"]
        lines.append("### Window split\n")
        lines.append(f"- Recent 30d: {rec['n']} trades, {fmt_money(rec['cost'])} cost, "
                     f"{fmt_money(rec['pnl'])} P&L, {fmt_pct(rec['roi'])} ROI, {rec['win_rate']*100:.1f}% wr")
        lines.append(f"- Earlier 60d: {ear['n']} trades, {fmt_money(ear['cost'])} cost, "
                     f"{fmt_money(ear['pnl'])} P&L, {fmt_pct(ear['roi'])} ROI, {ear['win_rate']*100:.1f}% wr\n")

        share, top_trades = w["concentration"]
        lines.append("### Concentration\n")
        lines.append(f"Top 5 trades by abs P&L account for **{share*100:.1f}%** of total |P&L|.\n")
        if share > 0.5:
            lines.append("> ⚠️  Highly concentrated. A handful of trades drove most of the P&L. "
                         "Strategy reading is questionable.\n")
        if top_trades:
            lines.append("Top 5 trades by |P&L|:")
            for t in top_trades:
                lines.append(f"- `{t.title[:80]}` · {t.outcome} @ {t.avg_price or 0:.2f} · "
                             f"cost {fmt_money(t.cost)} · pnl {fmt_money(t.pnl)} · "
                             f"{t.timestamp.date()}")
            lines.append("")

        lines.append("### Weekly ROI\n")
        lines.append("| Week | N | Cost | P&L | ROI | Win% |")
        lines.append("|---|---|---|---|---|---|")
        for wk in w["weekly"]:
            lines.append(f"| {wk['week']} | {wk['n']} | {fmt_money(wk['cost'])} | "
                         f"{fmt_money(wk['pnl'])} | {fmt_pct(wk['roi'])} | "
                         f"{wk['win_rate']*100:.0f}% |")
        lines.append("")

        lines.append("### By mean entry price band\n")
        lines.append("Where their P&L actually comes from. A band that dominates with mean entry "
                     "near 0 or 1 is mechanical scalping; a band near 0.5 is real picking.\n")
        lines.append("| Band | N | Cost | P&L | ROI | Win% |")
        lines.append("|---|---|---|---|---|---|")
        for b in w["bands"]:
            lines.append(f"| {b['band']} | {b['n']} | {fmt_money(b['cost'])} | "
                         f"{fmt_money(b['pnl'])} | {fmt_pct(b['roi'])} | "
                         f"{b['win_rate']*100:.0f}% |")
        lines.append("")

    return "\n".join(lines)


def main():
    top = load_top_wallets(TOP_K)
    print(f"[deep] selected top {len(top)} wallets by 30d ROI:")
    for i, (w, name) in enumerate(top, 1):
        print(f"  {i}. {w}  ({name})")

    per_wallet: list[dict] = []
    for i, (w, name) in enumerate(top, 1):
        print(f"\n[deep] [{i}/{len(top)}] pulling 90d for {w}...")
        t0 = time.time()
        raw = fetch_closed_positions(w, WINDOW_DAYS)
        elapsed = time.time() - t0
        trades = [tr for tr in (to_trade_row(w, r) for r in raw) if tr is not None]
        print(f"        got {len(trades)} trades in {elapsed:.1f}s")
        full = aggregate(trades)
        ear, rec = split_window(trades, RECENT_DAYS)
        per_wallet.append({
            "wallet":         w,
            "name":           name,
            "full":           full,
            "earlier":        aggregate(ear),
            "recent":         aggregate(rec),
            "weekly":         weekly_breakdown(trades),
            "bands":          price_band_breakdown(trades),
            "concentration":  top_market_concentration(trades, k=5),
        })

    report = render_report(per_wallet)
    REPORT_PATH.write_text(report)
    print(f"\n[deep] wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
