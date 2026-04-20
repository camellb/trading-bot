"""
Polymarket runner — the two scheduled entrypoints wired into main.py.

scan_and_analyze(limit):
    Fetch candidate markets → research → Claude evaluation → sizing →
    open shadow/live position. All gated by the PMAnalyst pipeline.

resolve_positions():
    For every open pm_positions row, check whether the underlying Polymarket
    market has resolved. If yes, settle via PMExecutor (writes settlement
    price, realized P&L, and feeds the calibration ledger). Also opportunistically
    backfills legacy shadow-mode predictions that lack a pm_positions row.

Both functions are safe to call repeatedly and never raise on partial failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import calibration
import config
from db.engine import get_engine
from engine.pm_analyst import PMAnalyst
from execution.pm_executor import PMExecutor
from feeds.polymarket_feed import (
    PolymarketFeed,
    estimate_market_settlement_deadline,
    parse_market_end_time,
)


SOURCE = "polymarket"



_scan_lock = asyncio.Lock()
_resolve_lock = asyncio.Lock()

# ── Subprocess scan (primary entrypoint for production) ─────────────────────
_subprocess_scan_running = False
_scan_proc: Optional[asyncio.subprocess.Process] = None  # for shutdown cleanup


def kill_scan_subprocess() -> None:
    """Kill any running scan subprocess. Called during bot shutdown."""
    global _scan_proc
    if _scan_proc is not None:
        try:
            _scan_proc.kill()
        except (ProcessLookupError, OSError):
            pass
        _scan_proc = None


async def scan_via_subprocess(
    limit: int = 20,
    min_volume_24h: float = 10_000.0,
    timeout: int = 1500,
) -> dict:
    """
    Run scan_and_analyze in a subprocess to isolate GIL/event loop.

    The scan's heavy I/O and CPU work (trafilatura/lxml, Anthropic SDK,
    DuckDuckGo threads) cause GIL contention that freezes the main
    event loop's timer callbacks.  Running in a subprocess gives the
    scan its own GIL, event loop, and thread pools — the main process's
    heartbeat, watchdog, and scheduler remain responsive.

    Returns the same summary dict as scan_and_analyze().
    """
    global _subprocess_scan_running
    if _subprocess_scan_running:
        return {"skipped": True, "reason": "scan already in progress"}

    _subprocess_scan_running = True
    worker_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "scan_worker.py"
    )
    scan_log_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs", "scan_last.log"
    )
    global _scan_proc
    try:
        # Redirect stderr to a file instead of PIPE — pipe buffers are lost
        # when asyncio.wait_for() cancels communicate() on timeout.
        log_fh = open(scan_log_path, "w")
        # PYTHONUNBUFFERED ensures print output hits the file immediately —
        # without it, block-buffering loses output when the process is killed.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, worker_path,
            str(limit), str(min_volume_24h), str(timeout),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=log_fh,
        )
        _scan_proc = proc

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            print(
                f"[scan_subprocess] TIMEOUT ({timeout}s) — killing worker "
                f"(pid={proc.pid})",
                file=sys.stderr, flush=True,
            )
            proc.kill()
            await proc.wait()
            log_fh.close()
            log_fh = None
            # Read last lines from log file for diagnostics
            try:
                with open(scan_log_path) as f:
                    scan_log = f.read()
                lines = [l for l in scan_log.splitlines() if l.strip()]
                for line in lines[-10:]:
                    print(f"[scan:timeout] {line.strip()}",
                          file=sys.stderr, flush=True)
            except Exception:
                pass
            return {"error": f"timeout ({timeout}s)"}

        log_fh.close()
        log_fh = None

        # Surface errors/timeouts from scan log to main stderr.
        try:
            with open(scan_log_path) as f:
                scan_log = f.read()
            for line in scan_log.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                low = stripped.lower()
                if any(kw in low for kw in (
                    "error", "timeout", "failed", "traceback",
                    "exception", "killed", "scan_worker",
                )):
                    print(f"[scan] {stripped}", file=sys.stderr, flush=True)
        except Exception:
            pass

        if proc.returncode != 0:
            print(
                f"[scan_subprocess] worker exited {proc.returncode}",
                file=sys.stderr, flush=True,
            )
            return {"error": f"exit code {proc.returncode}"}

        # Parse result JSON from stdout.
        raw_stdout = stdout.decode(errors="replace").strip()
        if not raw_stdout:
            return {"error": "empty result"}
        try:
            return json.loads(raw_stdout)
        except json.JSONDecodeError as exc:
            print(
                f"[scan_subprocess] bad JSON: {exc}\n"
                f"  stdout preview: {raw_stdout[:300]!r}",
                file=sys.stderr, flush=True,
            )
            return {"error": "unparseable result"}

    except Exception as exc:
        print(
            f"[scan_subprocess] launch failed: {exc}",
            file=sys.stderr, flush=True,
        )
        return {"error": f"launch failed: {exc}"}

    finally:
        _scan_proc = None
        _subprocess_scan_running = False
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:
                pass


# ── Scan + analyse (in-process, used by subprocess worker) ───────────────────
async def scan_and_analyze(
    limit:          int   = 20,
    min_volume_24h: float = 5_000.0,
    notifier:       object = None,
    memory:         object = None,
    analyst:        PMAnalyst | None = None,
    max_seconds:    int   = 0,
) -> dict:
    """
    Delegate to PMAnalyst. Returns the analyst's summary dict.
    Uses a module-level lock to prevent overlapping scans from
    scheduler, Telegram, and dashboard.
    """
    if _scan_lock.locked():
        print("[pm_runner] scan already in progress, skipping", flush=True)
        return {"skipped": True, "reason": "scan already in progress"}
    async with _scan_lock:
        if analyst is None:
            analyst = PMAnalyst(notifier=notifier, memory=memory)
        try:
            catchup = await asyncio.wait_for(
                reconcile_overdue_positions(
                    notifier=notifier,
                    executor=getattr(analyst, "executor", None),
                    max_passes=int(getattr(config, "PM_STARTUP_RECONCILE_PASSES", 3)),
                ),
                timeout=180,  # 3 min max for reconciliation
            )
        except asyncio.TimeoutError:
            print("[pm_runner] reconciliation TIMEOUT (180s)", file=sys.stderr, flush=True)
            catchup = {"stale_before": 0, "stale_after": 0, "awaiting_after": 0, "passes": 0}
        stale_before = catchup.get("stale_before")
        stale_after = catchup.get("stale_after")
        if stale_before is None or stale_after is None:
            print(
                "[pm_runner] scan blocked: unable to verify unresolved-market backlog",
                flush=True,
            )
            return {
                "skipped": True,
                "reason": "unable to verify resolution backlog",
                "catchup": catchup,
            }
        if stale_after > 0:
            print(
                "[pm_runner] scan blocked: "
                f"{stale_after} stale unresolved positions remain after "
                f"{catchup.get('passes', 0)} reconciliation pass(es)",
                flush=True,
            )
            return {
                "skipped": True,
                "reason": "stale positions pending settlement",
                "catchup": catchup,
            }
        print("[pm_runner] reconciliation passed, starting market scan",
              flush=True)
        summary = await analyst.scan_and_analyze(
            limit=limit,
            min_volume_24h=min_volume_24h,
            max_seconds=max_seconds,
        )
        if (stale_before or 0) > 0 or (catchup.get("awaiting_after") or 0) > 0:
            summary["catchup"] = catchup
        return summary


async def reconcile_overdue_positions(
    notifier=None,
    executor: PMExecutor | None = None,
    max_passes: int = 3,
) -> dict:
    """
    Best-effort recovery pass for unresolved positions. Distinguishes between
    normal "awaiting official result" markets and genuinely stale unresolved
    markets that should block fresh scans.
    """
    if executor is None:
        executor = PMExecutor()

    mode = getattr(executor, "mode", None)
    backlog_before = await _summarize_resolution_backlog(mode=mode)
    result = {
        "mode": mode or "all",
        "awaiting_before": None,
        "awaiting_after": None,
        "stale_before": None,
        "stale_after": None,
        "unknown_before": None,
        "unknown_after": None,
        "passes": 0,
        "positions_settled": 0,
        "errors": 0,
    }
    if backlog_before is None:
        return result
    result["awaiting_before"] = backlog_before["awaiting_results"]
    result["awaiting_after"] = backlog_before["awaiting_results"]
    result["stale_before"] = backlog_before["stale_unresolved"]
    result["stale_after"] = backlog_before["stale_unresolved"]
    result["unknown_before"] = backlog_before["unknown"]
    result["unknown_after"] = backlog_before["unknown"]

    if (
        backlog_before["awaiting_results"] <= 0
        and backlog_before["stale_unresolved"] <= 0
        and backlog_before["unknown"] <= 0
    ):
        return result

    max_passes = max(1, int(max_passes))
    remaining_stale = backlog_before["stale_unresolved"]
    for _ in range(max_passes):
        sweep = await resolve_positions(notifier=notifier, executor=executor)
        result["passes"] += 1
        result["positions_settled"] += int(sweep.get("positions_settled", 0) or 0)
        result["errors"] += int(sweep.get("errors", 0) or 0)

        backlog_after = await _summarize_resolution_backlog(mode=mode)
        if backlog_after is None:
            result["awaiting_after"] = None
            result["stale_after"] = None
            result["unknown_after"] = None
            break
        result["awaiting_after"] = backlog_after["awaiting_results"]
        result["stale_after"] = backlog_after["stale_unresolved"]
        result["unknown_after"] = backlog_after["unknown"]

        if backlog_after["stale_unresolved"] <= 0:
            break
        if backlog_after["stale_unresolved"] >= remaining_stale:
            break
        remaining_stale = backlog_after["stale_unresolved"]

    print(f"[pm_runner] reconciliation backlog: {result}", flush=True)
    return result


# ── Resolve positions + legacy predictions ──────────────────────────────────
async def resolve_positions(short_horizon_only: bool = False, notifier=None, executor: PMExecutor | None = None, risk_mgr=None) -> dict:
    """
    Two-phase settlement:

        Phase A — settle every open pm_positions row against the resolved
                  Polymarket market state.
        Phase B — resolve legacy `predictions` rows from the shadow-only
                  era that lack a pm_positions partner.

    Returns: {"positions_checked", "positions_settled",
              "predictions_checked", "predictions_resolved", "errors"}
    """
    async with _resolve_lock:
        result = {
            "positions_checked": 0, "positions_settled": 0,
            "predictions_checked": 0, "predictions_resolved": 0,
            "errors": 0,
        }

        mode = getattr(executor, "mode", None) if executor is not None else None
        open_rows   = await asyncio.to_thread(
            _fetch_open_positions, short_horizon_only=short_horizon_only, mode=mode
        )
        legacy_rows = (
            [] if short_horizon_only
            else await asyncio.to_thread(_fetch_unresolved_legacy_predictions)
        )

        market_ids = list({p["market_id"] for p in open_rows if p.get("market_id")}
                          | {p["market_id"] for p in legacy_rows if p.get("market_id")})
        if not market_ids:
            return result

        async with PolymarketFeed() as feed:
            try:
                rows = await asyncio.wait_for(
                    feed.fetch_many(market_ids),
                    timeout=90,
                )
            except asyncio.TimeoutError:
                print(f"[pm_runner] resolve fetch_many TIMEOUT (90s) for "
                      f"{len(market_ids)} markets", file=sys.stderr)
                return result

        if executor is None:
            executor = PMExecutor()

        # Phase A — pm_positions.
        for p in open_rows:
            result["positions_checked"] += 1
            raw = rows.get(p["market_id"])
            if not raw:
                continue
            market_end = parse_market_end_time(raw)
            if market_end is not None:
                await asyncio.to_thread(_sync_expected_resolution_at, p["id"], market_end)
            if not raw.get("closed", False):
                continue
            prices = _parse_price_list(raw.get("outcomePrices"))
            if len(prices) != 2:
                continue
            yes_won = prices[0] >= 0.99
            no_won  = prices[1] >= 0.99
            if not (yes_won or no_won):
                print(f"[resolve] INVALID settlement for pos #{p['id']} "
                      f"market={p['market_id']} prices={prices} "
                      f"(neither >= 0.99)", flush=True)
                await asyncio.to_thread(executor.settle_position, p["id"], "INVALID", 0.5)
                result["errors"] += 1
                continue
            outcome = "YES" if yes_won else "NO"
            if await asyncio.to_thread(executor.settle_position, p["id"], outcome):
                result["positions_settled"] += 1
                side = p.get("side", "?")
                pnl = (1.0 if outcome == side else 0.0) * p["shares"] - p["cost_usd"]
                is_win = (outcome == side)
                # Update risk manager streak tracking.
                if risk_mgr is not None:
                    try:
                        risk_mgr.record_outcome(pnl=pnl, is_win=is_win)
                    except Exception:
                        pass
                if notifier and hasattr(notifier, "notify_settlement"):
                    try:
                        await notifier.notify_settlement(
                            position_id=p["id"],
                            question=p.get("question", ""),
                            side=side, outcome=outcome, pnl=pnl,
                            cost=p["cost_usd"],
                        )
                    except Exception:
                        pass
            else:
                result["errors"] += 1

        # Phase B — legacy predictions without a pm_positions row.
        for p in legacy_rows:
            result["predictions_checked"] += 1
            raw = rows.get(p["market_id"])
            if not raw or not raw.get("closed", False):
                continue
            prices = _parse_price_list(raw.get("outcomePrices"))
            if len(prices) != 2:
                continue
            yes_won = prices[0] >= 0.99
            no_won  = prices[1] >= 0.99
            if not (yes_won or no_won):
                continue

            meta = _parse_json(p.get("metadata"))
            predicted_yes = float(p["probability"])
            claude_bet_yes = predicted_yes >= 0.5
            yes_px_at_pred = float((meta or {}).get("yes_price_at_prediction", 0.5))
            if claude_bet_yes:
                cost   = yes_px_at_pred
                payoff = 1.0 if yes_won else 0.0
            else:
                cost   = 1.0 - yes_px_at_pred
                payoff = 1.0 if no_won else 0.0
            pnl = payoff - cost  # per $1 of notional stake

            ok = await asyncio.to_thread(
                calibration.resolve_prediction_by_id,
                prediction_id = int(p["id"]),
                outcome       = 1 if yes_won else 0,
                pnl_usd       = pnl,
                note          = f"legacy_resolution YES={yes_won} NO={no_won}",
            )
            if ok:
                result["predictions_resolved"] += 1
            else:
                result["errors"] += 1

        if result["positions_settled"] > 0 or result["predictions_resolved"] > 0:
            await asyncio.to_thread(
                calibration.record_calibration_snapshot,
                source=SOURCE,
                note=(
                    "resolution sweep "
                    f"positions={result['positions_settled']} "
                    f"legacy_predictions={result['predictions_resolved']}"
                ),
            )

        print(f"[pm_runner] resolve_positions: {result}", flush=True)
        return result


# ── SQL helpers ──────────────────────────────────────────────────────────────
def _fetch_open_positions(short_horizon_only: bool = False, mode: str | None = None) -> list[dict]:
    try:
        with get_engine().begin() as conn:
            where = "status = 'open'"
            params = {}
            if mode:
                where += " AND mode = :mode"
                params["mode"] = mode
            if short_horizon_only:
                where += " AND expected_resolution_at < NOW() + INTERVAL '24 hours'"
            rows = conn.execute(text(
                f"SELECT id, market_id, side, shares, cost_usd, prediction_id, question "
                f"FROM pm_positions WHERE {where}"
            ), params).fetchall()
        return [
            {
                "id":            int(r[0]),
                "market_id":     str(r[1]),
                "side":          str(r[2]),
                "shares":        float(r[3]),
                "cost_usd":      float(r[4]),
                "prediction_id": int(r[5]) if r[5] is not None else None,
                "question":      str(r[6] or ""),
            }
            for r in rows
        ]
    except Exception as exc:
        print(f"[pm_runner] _fetch_open_positions failed: {exc}", file=sys.stderr)
        return []


def _fetch_unresolved_legacy_predictions() -> list[dict]:
    """
    Predictions that don't have a pm_positions row (e.g. from the pre-analyst
    shadow era) and are still unresolved.
    """
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT p.id, p.probability, p.subject_key, p.metadata "
                "FROM predictions p "
                "LEFT JOIN pm_positions pp ON pp.prediction_id = p.id "
                "WHERE p.source = :src "
                "  AND p.resolved_at IS NULL "
                "  AND pp.id IS NULL"
            ), {"src": SOURCE}).fetchall()
        out = []
        for r in rows:
            sk = r[2] or ""
            mid = sk.split(":", 1)[1] if sk.startswith("polymarket:") else None
            if not mid:
                continue
            out.append({
                "id":          int(r[0]),
                "probability": float(r[1]),
                "market_id":   mid,
                "metadata":    r[3],
            })
        return out
    except Exception as exc:
        print(f"[pm_runner] _fetch_legacy failed: {exc}", file=sys.stderr)
        return []


async def _summarize_resolution_backlog(mode: str | None = None) -> dict | None:
    open_rows = await asyncio.to_thread(_fetch_open_positions, mode=mode)
    if not open_rows:
        return {
            "open_positions": 0,
            "awaiting_results": 0,
            "stale_unresolved": 0,
            "unknown": 0,
            "upcoming": 0,
        }

    market_ids = list({row["market_id"] for row in open_rows if row.get("market_id")})
    if not market_ids:
        return {
            "open_positions": len(open_rows),
            "awaiting_results": 0,
            "stale_unresolved": 0,
            "unknown": 0,
            "upcoming": 0,
        }

    try:
        async with PolymarketFeed() as feed:
            rows = await asyncio.wait_for(
                feed.fetch_many(market_ids),
                timeout=60,
            )
    except asyncio.TimeoutError:
        print("[pm_runner] _summarize_resolution_backlog TIMEOUT (60s)",
              file=sys.stderr)
        return None
    except Exception as exc:
        print(
            f"[pm_runner] _summarize_resolution_backlog failed: {exc}",
            file=sys.stderr,
        )
        return None

    now = datetime.now(timezone.utc)
    summary = {
        "open_positions": len(open_rows),
        "awaiting_results": 0,
        "stale_unresolved": 0,
        "unknown": 0,
        "upcoming": 0,
    }
    # Grace period: markets often take hours after their end time before
    # the Gamma API reports the resolution outcome. Don't block scans for
    # recently-ended markets — only flag them stale after 24h past end time.
    _STALE_GRACE = timedelta(hours=24)

    for row in open_rows:
        raw = rows.get(row["market_id"])
        if not raw:
            summary["unknown"] += 1
            continue

        market_end = parse_market_end_time(raw)
        stale_deadline = estimate_market_settlement_deadline(raw) or market_end

        if market_end is not None and market_end > now:
            # Market hasn't ended yet
            summary["upcoming"] += 1
        elif stale_deadline is not None and stale_deadline > now:
            # Past end time but within settlement window — normal
            summary["awaiting_results"] += 1
        elif market_end is not None and (now - market_end) < _STALE_GRACE:
            # Past settlement window but within 24h grace — still awaiting.
            # Many markets take hours to post official results, especially
            # sports, geopolitics, and markets ending outside US hours.
            summary["awaiting_results"] += 1
        else:
            # Truly stale: >24h past end time with no resolution
            summary["stale_unresolved"] += 1
    return summary


def _sync_expected_resolution_at(position_id: int, expected_at: datetime) -> None:
    try:
        with get_engine().begin() as conn:
            conn.execute(text(
                "UPDATE pm_positions "
                "SET expected_resolution_at = :expected_at "
                "WHERE id = :pid "
                "  AND status = 'open' "
                "  AND (expected_resolution_at IS NULL "
                "       OR ABS(EXTRACT(EPOCH FROM (expected_resolution_at - :expected_at))) > 60)"
            ), {
                "pid": int(position_id),
                "expected_at": expected_at,
            })
    except Exception as exc:
        print(
            f"[pm_runner] _sync_expected_resolution_at failed for {position_id}: {exc}",
            file=sys.stderr,
        )


def _parse_price_list(raw) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except Exception:
            return []
    try:
        return [float(x) for x in json.loads(raw)]
    except Exception:
        return []


def _parse_json(raw) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ── Back-compat shims for any callers of the old names ───────────────────────
# Kept only during the rewrite; remove once nothing imports these.
async def scrape_and_evaluate(*args, **kwargs):
    return await scan_and_analyze(*args, **kwargs)

async def resolve_pending(*args, **kwargs):
    return await resolve_positions(*args, **kwargs)


# ── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "resolve", "both"],
                    default="scan", nargs="?")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-volume", type=float, default=5_000.0)
    args = ap.parse_args()

    async def _main():
        from engine.memory import MemoryManager
        memory = MemoryManager()
        if args.mode in ("scan", "both"):
            await scan_and_analyze(limit=args.limit,
                                    min_volume_24h=args.min_volume,
                                    memory=memory)
        if args.mode in ("resolve", "both"):
            await resolve_positions()

    asyncio.run(_main())
