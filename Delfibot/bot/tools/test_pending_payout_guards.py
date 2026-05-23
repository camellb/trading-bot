"""
Regression test - pending_payout SQL guards in pm_executor.

Asserts that the pending-payout projection in
`pm_executor.get_portfolio_stats` keeps both guards introduced in
commit a718bc3 (2026-05-23). If either guard disappears, the bug
returns: stale settled-but-not-redeemed rows inflate `bankroll`
forever, the dashboard headline and every Telegram message drift
from Polymarket truth by $10+, and the user loses trust.

The same class of bug - phantom money in Balance because the
projection accumulates rows that never get a redeem_tx_hash - has
been re-introduced FOUR times in this codebase. The user
explicitly asked us to "engrave it somewhere so it doesn't get
fucked up anymore." This file is the engraving. The sidecar
build script runs it; if the asserts fail, the build doesn't
ship.

The two guards we enforce:

  1. NO `OR status = 'invalid'` in the WHERE clause. Polymarket
     auto-refunds voided markets directly to the wallet - they
     never produce a redeem_tx_hash, so including them in the
     projection is a permanent double-count.

  2. A 10-minute floor on settled_at. Anything older means the
     relayer redeem already happened (and we missed capturing the
     tx_hash) or never will, so the wallet probe ALREADY reflects
     the payout and we'd be double-counting.

Run:
    python3 Delfibot/bot/tools/test_pending_payout_guards.py

Exit code 0 on pass, 1 on fail, 2 on missing source.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


SOURCE_PATH = Path(__file__).resolve().parents[1] / "execution" / "pm_executor.py"


def _read_source() -> str:
    if not SOURCE_PATH.exists():
        sys.stderr.write(
            f"FATAL: pm_executor.py not found at {SOURCE_PATH}\n"
        )
        sys.exit(2)
    return SOURCE_PATH.read_text()


def _pending_block(src: str) -> str:
    """Extract a window around the pending_payout SQL definition.

    Anchors on the well-known marker line just before the SQL
    block. Returns roughly the next 3500 chars (covers the SQL
    + the trailing exception handler).
    """
    start = src.find("bankroll_wallet  = float(self.get_bankroll())")
    if start == -1:
        return ""
    return src[start:start + 3500]


def check_no_invalid_branch(src: str) -> bool:
    """Guard 1: no `OR status = 'invalid'` anywhere in the
    pending-payout projection.

    The negative-match is the OR-into-pending-projection variant.
    Outside the projection there are legitimate uses (e.g.
    `status IN ('settled', 'invalid', 'closed_early')` in the
    settled_n count); we scope the search to the pending_payout
    block only.
    """
    block = _pending_block(src)
    if not block:
        sys.stderr.write(
            "FATAL: could not locate pending_payout section in "
            "pm_executor.py; refactor likely broke this guard. "
            "Update the SOURCE_PATH anchor in this test if the "
            "method moved.\n"
        )
        return False
    bad = re.search(r"OR\s+status\s*=\s*['\"]invalid['\"]", block)
    if bad:
        sys.stderr.write(
            "FAIL: pm_executor.get_portfolio_stats pending_payout "
            "SQL contains `OR status = 'invalid'`. This was removed "
            "in a718bc3 because Polymarket auto-refunds invalid "
            "markets directly to the wallet - including them in the "
            "projection causes a permanent double-count of the "
            "refund (the bug a paying user surfaced 2026-05-23). "
            "See Obsidian/Delfi/50_Feedback/log_every_major_bug.md "
            "for the full incident.\n"
        )
        return False
    return True


def check_settled_at_floor(src: str) -> bool:
    """Guard 2: the projection has a `settled_at > datetime('now',
    '-N minutes')` floor, with N <= 10.

    Without this, a settled-winner row whose redeem_tx_hash never
    got captured stays in the projection forever and the wallet
    appears N dollars higher than it actually is on every refresh.
    """
    block = _pending_block(src)
    if not block:
        return False  # already reported in the other check
    floor = re.search(
        r"settled_at\s*>\s*datetime\(\s*['\"]now['\"]\s*,\s*"
        r"['\"]-(\d+)\s*minutes?['\"]",
        block,
    )
    if not floor:
        sys.stderr.write(
            "FAIL: pm_executor.get_portfolio_stats pending_payout "
            "SQL is missing the settled_at floor "
            "(`AND settled_at > datetime('now', '-N minutes')`). "
            "Without this guard, a settled position whose "
            "redeem_tx_hash never gets captured stays in the "
            "projection forever and bankroll drifts by the row's "
            "cost+pnl, indefinitely. The 10-minute window was "
            "chosen in a718bc3 to cover the longest realistic "
            "relayer-redeem latency. See Obsidian/Delfi/50_Feedback"
            "/log_every_major_bug.md.\n"
        )
        return False
    minutes = int(floor.group(1))
    if minutes > 10:
        sys.stderr.write(
            f"FAIL: pending_payout settled_at floor is {minutes} "
            f"minutes; max allowed is 10. Wider windows risk the "
            f"same phantom-payout double-count the 10-min cap was "
            f"chosen to prevent.\n"
        )
        return False
    return True


def main() -> int:
    src = _read_source()
    ok1 = check_no_invalid_branch(src)
    ok2 = check_settled_at_floor(src)
    if ok1 and ok2:
        print("OK: pending_payout guards intact.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
