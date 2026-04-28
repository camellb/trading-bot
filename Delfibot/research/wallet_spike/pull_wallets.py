"""
Pull top 100 Polymarket wallets by 30-day volume + their settled trades.

One-shot research script. Not part of the bot. Run from this directory:

    python3 pull_wallets.py

Outputs (gitignored):
    wallets.csv  - one row per leaderboard entry (rank, wallet, vol, pnl)
    trades.csv   - one row per closed position across all 100 wallets
    raw/         - raw JSON payloads for re-analysis

Endpoints used (post-V2 Polymarket data-api):
    GET https://data-api.polymarket.com/v1/leaderboard
        ?timePeriod=MONTH&orderBy=VOL&limit=50&offset={0,50}
    GET https://data-api.polymarket.com/closed-positions
        ?user={0x...}&sortBy=TIMESTAMP&sortDirection=DESC&limit=50&offset={...}

Why VOL (not PNL) for ranking: ordering by PNL pre-selects this-month
winners. We want active traders without leaking outcomes into selection,
then we MEASURE who actually has edge. Survivorship test in analyze.py
re-checks this.

Throttle: 100ms sleep between requests. ~300-1500 requests total at
~5-10 req/sec.
"""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


HERE = Path(__file__).parent
WALLETS_CSV = HERE / "wallets.csv"
TRADES_CSV = HERE / "trades.csv"
RAW_DIR = HERE / "raw"

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
CLOSED_POSITIONS_URL = "https://data-api.polymarket.com/closed-positions"

USER_AGENT = "delfi-research/1.0"
THROTTLE_SECONDS = 0.1
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0

WINDOW_DAYS = 30
TARGET_WALLETS = 100
PER_PAGE = 50


def http_get_json(url: str, params: dict) -> object:
    """GET with retries and exponential backoff. Returns parsed JSON or None."""
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
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                print(f"  429 rate-limit, sleeping {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  http {exc.code} on {full_url}: {exc.read()[:200]!r}", file=sys.stderr)
            return None
        except (URLError, json.JSONDecodeError, TimeoutError) as exc:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                print(f"  {type(exc).__name__}, retrying in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  giving up on {full_url}: {exc}", file=sys.stderr)
            return None
    return None


def fetch_leaderboard(target: int = TARGET_WALLETS) -> list[dict]:
    """Pull the top `target` wallets ranked by 30-day volume."""
    out: list[dict] = []
    offset = 0
    while len(out) < target:
        page = http_get_json(LEADERBOARD_URL, {
            "timePeriod": "MONTH",
            "orderBy":    "VOL",
            "limit":      str(PER_PAGE),
            "offset":     str(offset),
        })
        if not isinstance(page, list) or not page:
            print(f"  leaderboard exhausted at offset={offset}", file=sys.stderr)
            break
        out.extend(page)
        offset += len(page)
        time.sleep(THROTTLE_SECONDS)
    return out[:target]


def _row_ts(row: dict) -> float | None:
    """Polymarket returns timestamp as unix-seconds OR ISO string. Handle both."""
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


def fetch_closed_positions(wallet: str) -> list[dict]:
    """
    Walk closed positions for one wallet, paginating until either:
      - the response is empty
      - the oldest row on a page is older than the 30d cutoff
      - we hit a 1000-offset safety cap
    Returns rows whose timestamp is inside the 30d window.
    """
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp()
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
        if offset > 1000:
            break
        time.sleep(THROTTLE_SECONDS)
    return [r for r in all_rows if (_row_ts(r) or 0) >= cutoff_ts]


def write_csvs(leaderboard: list[dict], all_trades: dict[str, list[dict]]) -> None:
    """Write wallets.csv + trades.csv + raw JSON dumps."""
    RAW_DIR.mkdir(exist_ok=True)
    (RAW_DIR / "leaderboard.json").write_text(json.dumps(leaderboard, indent=2))
    (RAW_DIR / "closed_positions.json").write_text(json.dumps(all_trades, indent=2))

    with open(WALLETS_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "rank", "proxyWallet", "userName", "vol", "pnl",
            "verifiedBadge", "n_trades_in_window",
        ])
        for i, row in enumerate(leaderboard):
            wallet = row.get("proxyWallet", "")
            n = len(all_trades.get(wallet, []))
            w.writerow([
                row.get("rank", i + 1),
                wallet,
                row.get("userName", ""),
                row.get("vol", 0),
                row.get("pnl", 0),
                row.get("verifiedBadge", False),
                n,
            ])

    with open(TRADES_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "wallet", "conditionId", "title", "outcome", "avgPrice",
            "totalBought", "realizedPnl", "timestamp", "endDate",
        ])
        for wallet, rows in all_trades.items():
            for r in rows:
                ts = _row_ts(r)
                ts_iso = (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                          if ts else "")
                w.writerow([
                    wallet,
                    r.get("conditionId", ""),
                    (r.get("title") or "").strip()[:100],
                    r.get("outcome", ""),
                    r.get("avgPrice", ""),
                    r.get("totalBought", ""),
                    r.get("realizedPnl", ""),
                    ts_iso,
                    r.get("endDate", ""),
                ])


def main():
    print(f"[pull] target {TARGET_WALLETS} wallets, {WINDOW_DAYS}d window, ranked by 30d volume")
    t0 = time.time()
    leaderboard = fetch_leaderboard(TARGET_WALLETS)
    print(f"[pull] got {len(leaderboard)} wallets in {time.time() - t0:.1f}s")
    if not leaderboard:
        print("[pull] no wallets pulled; aborting", file=sys.stderr)
        sys.exit(1)

    all_trades: dict[str, list[dict]] = {}
    for i, row in enumerate(leaderboard):
        wallet = row.get("proxyWallet", "")
        if not wallet:
            continue
        trades = fetch_closed_positions(wallet)
        all_trades[wallet] = trades
        if (i + 1) % 10 == 0 or i + 1 == len(leaderboard):
            elapsed = time.time() - t0
            total_trades = sum(len(v) for v in all_trades.values())
            print(f"[pull] {i + 1}/{len(leaderboard)} wallets, "
                  f"{total_trades} trades total, {elapsed:.1f}s elapsed")

    write_csvs(leaderboard, all_trades)
    print(f"[pull] wrote {WALLETS_CSV.name} + {TRADES_CSV.name}")
    print(f"[pull] done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
