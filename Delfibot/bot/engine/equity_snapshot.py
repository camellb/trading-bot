"""
Equity snapshot writer + reader.

Periodically records (bankroll, open_cost, equity) for the active user
into `equity_snapshots`. Drives the Dashboard + Performance "Equity
history" chart.

Why this exists: the chart was previously reconstructed on the
frontend from `currentEquity - cumulative_realized_pnl`, which made
the curve silently retcon the past whenever the wallet balance jumped
(e.g. user deposited cash, withdrew cash, or had a payout land
between settlements). Real periodic snapshots fix that - a deposit
becomes a natural step-up at the actual time it happened, instead of
parallel-shifting the entire historical curve.

The snapshot is the SAME triple the dashboard tile reads from
/api/summary, captured at a moment in time:
  bankroll  = PMExecutor.get_bankroll()   (live: wallet probe, sim: DB formula)
  open_cost = sum of currentValue across open positions
  equity    = bankroll + open_cost

Called by the `equity_snapshot` scheduler job in main.py every 10 min.
Failure is logged but swallowed so a transient probe issue doesn't
crash the scheduler.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from db.engine import get_engine, iso_utc


# Snapshot cadence is 10 minutes. With ~144 rows/day per active mode
# (live or sim), a year of continuous use is ~52k rows - trivial for
# SQLite. The read endpoint downsamples to ~500 points server-side
# so the chart stays performant even on multi-year histories.
SNAPSHOT_INTERVAL_MINUTES = 10


def record_equity_snapshot(user_id: str = "local") -> bool:
    """Capture one (bankroll, open_cost, equity) row for the active
    user's current mode.

    Returns True if a row was inserted, False on any failure. Failures
    are logged to stderr but never raised - the scheduler keeps
    running.

    The snapshot mirrors whatever /api/summary would return RIGHT
    NOW: same code path (PMExecutor.get_portfolio_stats), so the
    chart endpoint and the headline tile read from the exact same
    source of truth.
    """
    try:
        # Import inside the function so a module-load failure in
        # downstream code (e.g. a stale Polymarket import) doesn't
        # break the scheduler at boot.
        from engine.user_config import get_user_config
        from execution.pm_executor import PMExecutor

        cfg = get_user_config(user_id)
        mode = (cfg.mode or "").lower()
        if mode not in ("live", "simulation"):
            # User hasn't finished onboarding yet. Nothing to record.
            print(
                f"[equity_snapshot] skip: mode={mode!r} (waiting for onboarding)",
                file=sys.stderr, flush=True,
            )
            return False

        executor = PMExecutor(user_id=user_id, user_config=cfg)

        # Use the canonical executor accessors. They read from the
        # wallet probe cache (warmed every 60s by pm_balance_refresh)
        # in live mode, and from the DB-derived formula in sim.
        # `get_portfolio_stats()` returns None for `bankroll` /
        # `equity` whenever the wallet probe is cold - those Nones
        # are intentional because /api/summary applies its own live
        # overlay on top. We can't reuse that overlay here, so we
        # call the underlying accessors directly.
        bankroll = float(executor.get_bankroll())
        equity = float(executor.get_equity())
        # open_cost is the difference - same identity the rest of
        # the codebase relies on (equity = bankroll + open_cost).
        open_cost = max(0.0, equity - bankroll)

        # Wallet-probe-failure guard. In live mode, a snapshot with
        # bankroll=0 AND open_cost>0 is impossible in reality - the
        # user would have to be holding open positions with literally
        # zero cash, which never happens because the bot leaves a
        # tiny gas float. The pattern is the unambiguous signature
        # of the wallet probe returning a stale/failed read (timeout
        # during VPN flip, geo-block window, cold cache on first
        # boot). 2026-06-16 incident: 8 such rows had been written
        # over a week of network blips and showed up on the equity
        # chart as sharp downward dips that mystified the user. Skip
        # the write instead - a small gap in the curve is honest;
        # a phantom dip is not.
        if mode == "live" and bankroll == 0.0 and open_cost > 0:
            print(
                f"[equity_snapshot] skip: bankroll=0 + open_cost="
                f"{open_cost:.2f} (wallet probe returned a stale/"
                f"failed read; not writing a phantom dip)",
                file=sys.stderr, flush=True,
            )
            return False

        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        with get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO equity_snapshots "
                    "  (user_id, mode, ts, bankroll, open_cost, equity) "
                    "VALUES "
                    "  (:uid, :mode, :ts, :bankroll, :open_cost, :equity)"
                ),
                {
                    "uid":       user_id,
                    "mode":      mode,
                    "ts":        ts,
                    "bankroll":  float(bankroll),
                    "open_cost": float(open_cost),
                    "equity":    float(equity),
                },
            )
        return True
    except Exception as exc:
        print(
            f"[equity_snapshot] record failed for user={user_id}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def get_equity_history(
    user_id: str = "local",
    mode: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """Return the recorded equity snapshots for one user+mode, oldest
    first.

    When the number of stored snapshots exceeds `limit`, downsample
    by picking evenly-spaced rows. This keeps the chart payload
    bounded regardless of history depth.

    Each item:
      {"ts": "2026-05-26T19:30:00", "bankroll": 12.50,
       "open_cost": 5.00, "equity": 17.50}
    """
    if mode not in ("live", "simulation"):
        return []
    try:
        with get_engine().begin() as conn:
            # Total count first so we can compute the downsample stride.
            count_row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM equity_snapshots "
                    "WHERE user_id = :uid AND mode = :m"
                ),
                {"uid": user_id, "m": mode},
            ).fetchone()
            total = int(count_row[0]) if count_row else 0
            if total == 0:
                return []
            if total <= limit:
                rows = conn.execute(
                    text(
                        "SELECT ts, bankroll, open_cost, equity "
                        "FROM equity_snapshots "
                        "WHERE user_id = :uid AND mode = :m "
                        "ORDER BY ts ASC"
                    ),
                    {"uid": user_id, "m": mode},
                ).fetchall()
            else:
                # ROW_NUMBER() based stride downsample. Always keeps
                # the first AND last snapshots so the chart endpoints
                # stay anchored to the true start + current state.
                stride = max(1, total // limit)
                rows = conn.execute(
                    text(
                        "SELECT ts, bankroll, open_cost, equity FROM ("
                        "  SELECT ts, bankroll, open_cost, equity, "
                        "         ROW_NUMBER() OVER (ORDER BY ts ASC) AS rn "
                        "  FROM equity_snapshots "
                        "  WHERE user_id = :uid AND mode = :m"
                        ") "
                        "WHERE rn = 1 OR rn = :total OR rn % :stride = 0 "
                        "ORDER BY ts ASC"
                    ),
                    {
                        "uid":    user_id,
                        "m":      mode,
                        "total":  total,
                        "stride": stride,
                    },
                ).fetchall()
        out = []
        for r in rows:
            # iso_utc tags SQLite's naive datetime strings with an
            # explicit "+00:00" so the JS Date parser doesn't drop
            # them into the user's local time. Same anchoring trick
            # /api/brier-trend uses for settled_at.
            ts_iso = iso_utc(r[0]) or ""
            out.append({
                "ts":        ts_iso,
                "bankroll":  float(r[1] or 0.0),
                "open_cost": float(r[2] or 0.0),
                "equity":    float(r[3] or 0.0),
            })
        return out
    except Exception as exc:
        print(
            f"[equity_snapshot] history read failed for user={user_id} "
            f"mode={mode}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return []
