"""
50-trade learning-cycle review report.

Fires every LEARNING_CYCLE_TRADE_INTERVAL settled trades (see
`engine/learning_cadence.py`). Produces three artefacts for one user in
one mode:

  1. A short, model-written thesis (2-3 sentences) narrating the cycle's
     story. Guarded by a banned-word sanitiser because user-facing copy
     must never mention Claude / Anthropic / LLM / Gemini / prompts.
  2. A deterministic plain-text data block (headline ROI, per-archetype
     PnL, top wins, top losses, calibration bins, cost validation,
     proposals queued).
  3. The same data block with raw model-reasoning excerpts appended, for
     the admin mirror only.

The public entry point `compose_report` returns both texts plus the
structured data; `save_report` persists them to `learning_reports`;
`list_learning_reports` reads back by user / mode. The module degrades
gracefully: if anthropic is unavailable the thesis falls back to a
deterministic summary; if the DB query fails we return an empty
scaffold so the learning cycle still completes and the user sees a
"no settled trades" message instead of a traceback.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any, Optional

from db.engine import iso_utc
from engine import diagnostics as _diag
from engine import learning_cadence as _cadence


# ── Constants ────────────────────────────────────────────────────────────────
REPORT_FOOTER = (
    "Learning accumulates - the more Delfi runs, the better it gets."
)

_THESIS_MAX_CHARS = 480
_TOP_N_WINS = 3
_TOP_N_LOSSES = 3
_REASONING_EXCERPT_CHARS = 600

# Words forbidden in user-facing copy. The sanitiser scrubs these out of
# any model-generated thesis even when the system prompt holds, so the
# banned-terminology invariant is defended in depth.
_BANNED_WORDS = (
    "claude", "anthropic", "gemini", "openai", "gpt",
    "llm", "prompt", "system prompt",
    "edge", "edge-hunting",
)
_BANNED_REPLACEMENT = "the model"

_THESIS_SYSTEM = (
    "You are Delfi, an autonomous prediction market trader. You are "
    "writing a 2-3 sentence thesis summarising the last 50 settled "
    "trades for the user. Be specific and honest: name the single "
    "biggest driver of profit or loss, name the single biggest "
    "calibration issue if one is visible, and say what the next 50 "
    "trades will focus on. Do not hedge. Do not use em-dashes. Never "
    "mention Claude, Anthropic, Gemini, GPT, LLMs, prompts, or system "
    "prompts under any circumstance. Refer to yourself as 'Delfi' or "
    "'the model'. Plain prose. No markdown, no bullet points, no "
    "headings."
)


# ── Public entry points ──────────────────────────────────────────────────────
def compose_report(user_id: str,
                   mode: str = "simulation",
                   cycle_size: int = 50) -> dict:
    """
    Build the full review report for the last `cycle_size` settled trades
    in `mode`. Returns a dict with:

      - "user_text":     thesis + deterministic tables + footer
      - "admin_text":    user_text plus model-reasoning excerpts
      - "thesis":        the 2-3 sentence narration
      - "data":          structured diagnostic data (JSONB-safe)
      - "settled_count": actual trades found in the window
    """
    data = gather_cycle_data(user_id=user_id, mode=mode, cycle_size=cycle_size)
    settled = int(data.get("settled_count") or 0)

    if settled == 0:
        thesis = (
            "No settled trades in the last cycle window. Delfi will "
            "resume narrating once markets resolve."
        )
        tables = "No settled trades in this cycle window."
        user_text = f"{thesis}\n\n{tables}\n\n{REPORT_FOOTER}"
        return {
            "user_text":     user_text,
            "admin_text":    user_text,
            "thesis":        thesis,
            "data":          data,
            "settled_count": 0,
        }

    thesis = generate_thesis(data)
    tables = render_data_tables(data)
    user_text = f"{thesis}\n\n{tables}\n\n{REPORT_FOOTER}"

    admin_excerpts = render_admin_excerpts(data)
    admin_text = user_text
    if admin_excerpts:
        admin_text = (
            f"{thesis}\n\n{tables}\n\n{admin_excerpts}\n\n{REPORT_FOOTER}"
        )

    return {
        "user_text":     user_text,
        "admin_text":    admin_text,
        "thesis":        thesis,
        "data":          data,
        "settled_count": settled,
    }


def save_report(user_id: str, mode: str, report: dict) -> Optional[int]:
    """INSERT one row into learning_reports. Returns the new id, or None
    if a row already exists for the same (user_id, mode, settled_count)
    bookmark or the persistence call failed.

    The ON CONFLICT clause matches the UNIQUE constraint added in
    migration 025 and turns duplicate-key collisions into a clean None
    return rather than an exception. The learning cadence reads that
    None as "skip Telegram send", which is the defence-in-depth gate
    against the duplicate-review bug.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        data_json = json.dumps(_jsonable(report.get("data") or {}))
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "INSERT INTO learning_reports "
                "(user_id, mode, settled_count, thesis, summary_user, "
                " summary_admin, data) "
                "VALUES (:uid, :m, :sc, :th, :su, :sa, :d) "
                "ON CONFLICT (user_id, mode, settled_count) DO NOTHING "
                "RETURNING id"
            ), {
                "uid": user_id,
                "m":   mode,
                "sc":  int(report.get("settled_count") or 0),
                "th":  report.get("thesis"),
                "su":  report.get("user_text") or "",
                "sa":  report.get("admin_text"),
                "d":   data_json,
            }).fetchone()
            return int(row[0]) if row else None
    except Exception as exc:
        # ON CONFLICT requires the matching unique constraint to exist.
        # If migration 025 has not yet been applied on this database, the
        # INSERT will raise and we fall back to the safe behaviour: no
        # row written, no Telegram sent. The bookmark fix in
        # `_last_cycle_settled_count` already prevents the duplicate-fire
        # at the source, so this fallback is only relevant in the
        # transient pre-migration window.
        print(f"[review_report] save_report failed: {exc}", file=sys.stderr)
        return None


def list_learning_reports(user_id: str,
                          limit: int = 10,
                          include_admin: bool = False,
                          mode: Optional[str] = None) -> list[dict]:
    """Newest-first. `include_admin=True` exposes the reasoning-bearing
    `summary_admin` variant. The structured `data` JSON column is now
    returned in BOTH modes — the desktop UI consumes it to render
    proper review cards (stat grid, per-archetype, top wins/losses,
    calibration) instead of a monospace text dump.

    When `mode` is provided, only reports generated in that mode are
    returned. Intelligence page passes the user's current mode so
    sim-mode reports don't appear in the live view (and vice versa).
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        cols = [
            "id", "created_at", "mode", "settled_count",
            "thesis", "summary_user", "data",
        ]
        if include_admin:
            cols.append("summary_admin")
        params: dict = {"uid": user_id, "lim": int(limit)}
        if mode in ("live", "simulation"):
            mode_clause = " AND mode = :mode "
            params["mode"] = mode
        else:
            mode_clause = " "
        # Newest cycle first by BOOKMARK, not by row created_at. The
        # backfill script can insert all rows within one second, which
        # makes created_at a degenerate sort key (SQLite falls back to
        # id ASC, listing oldest cycle first). The UI assumes
        # newest-first to compute "Trades N-M · Cycle X" labels, so
        # ordering by settled_count is the canonical contract.
        sql = (
            f"SELECT {', '.join(cols)} FROM learning_reports "
            f"WHERE user_id = :uid {mode_clause}"
            "ORDER BY settled_count DESC, created_at DESC LIMIT :lim"
        )
        with get_engine().begin() as conn:
            rows = conn.execute(text(sql), params).fetchall()
    except Exception as exc:
        print(f"[review_report] list_reports failed: {exc}", file=sys.stderr)
        return []

    out: list[dict] = []
    for r in rows:
        raw_data = r[6]
        if isinstance(raw_data, (str, bytes)):
            try:
                raw_data = json.loads(raw_data)
            except (TypeError, ValueError):
                raw_data = None
        item = {
            "id":            int(r[0]),
            "created_at":    iso_utc(r[1]),
            "mode":          r[2],
            "settled_count": int(r[3] or 0),
            "thesis":        r[4],
            "summary_user":  r[5],
            "data":          raw_data,
        }
        if include_admin:
            item["summary_admin"] = r[7]
        out.append(item)
    return out


# ── Data gathering ───────────────────────────────────────────────────────────
def gather_cycle_data(user_id: str, mode: str, cycle_size: int) -> dict:
    """
    Pull the last `cycle_size` settled pm_positions in `mode` and assemble
    the deterministic data block. Two versions of `top_wins` / `top_losses`
    are produced: a public one (reasoning stripped) and an admin one
    (reasoning kept), so the admin mirror can surface raw excerpts without
    leaking them to the user.
    """
    headline = {
        "n":          0,
        "pnl_usd":    0.0,
        "cost_usd":   0.0,
        "roi":        0.0,
        "win_rate":   0.0,
        "brier":      None,
    }
    # Lifetime block aligns with what the in-app Performance page
    # shows (`Delfibot/bot/src/pages/Performance.tsx`). The user
    # complained that the review's ROI did not match the dashboard's
    # ROI: the cycle-window
    # number is ROI on capital staked over the last 50 trades, while the
    # dashboard reports lifetime ROI as `(equity - starting)/starting`.
    # We surface both here, clearly labelled, so the two numbers can be
    # reconciled at a glance.
    lifetime = {
        "settled_total":  0,
        "wins":           0,
        "win_rate":       None,
        "realized_pnl":   0.0,
        "starting_cash":  None,
        "equity":         None,
        "roi":            None,   # dashboard formula: (equity - start) / start
    }
    # `exit_policy` summarises how the take-profit / stop-loss /
    # time-decay rules behaved this cycle. The shaper compares the
    # realized P&L on closed-early rows to their counterfactual hold
    # P&L (set by the resolver Phase C backfill) and aggregates per
    # exit reason. The thesis can then comment on whether the policy
    # made money or left it on the table.
    exit_policy = {
        "early_n":             0,
        "early_pnl_usd":       0.0,
        "counterfactual_n":    0,
        "counterfactual_pnl":  0.0,
        "saved_vs_hold_usd":   None,  # exit_pnl - hold_pnl on backfilled rows
        "by_reason":           [],    # [{reason, n, pnl, hold_pnl, saved}]
    }

    data: dict[str, Any] = {
        "mode":               mode,
        "cycle_size":         cycle_size,
        "settled_count":      0,
        "headline":           headline,
        "lifetime":           lifetime,
        "per_archetype":      [],
        "calibration":        [],
        "cost_validation":    None,
        "exit_policy":        exit_policy,
        # V1.5 diagnostic slices: every section the new proposers
        # reason about gets surfaced here so the Intelligence page
        # can render them as "show your work" tables next to the
        # actual suggestion cards. NULL/empty when there is not
        # enough data; the frontend just hides those sections.
        "horizon_pnl":             [],
        "archetype_price_band":    [],
        "loss_day_recovery":       None,
        "loss_week_recovery":      None,
        "loss_streak":             None,
        "exit_threshold_sweep":    None,
        "exit_policy_attribution": None,
        "aggregate_roi":           None,
        "top_wins":           [],
        "top_losses":         [],
        "top_wins_admin":     [],
        "top_losses_admin":   [],
        "proposals":          [],
        "verdict":            "insufficient_data",
        # ISO-8601 UTC timestamps of the earliest and latest settled
        # trade in this cycle's window. The Reviews UI renders them
        # next to the cycle number ("May 2 - May 5 · Cycle 3"). Null
        # on empty windows.
        "window_start":       None,
        "window_end":         None,
    }

    # Always populate the lifetime block, even when the cycle window is
    # empty - if the user has no settled trades at all it will read all
    # zeros and the report still renders cleanly.
    lifetime.update(_fetch_lifetime_stats(user_id=user_id, mode=mode))

    # V1.5 diagnostic slices. Pull each one in a try/except so a
    # SQL error in any single helper doesn't sink the whole report;
    # the frontend already tolerates null/empty for any of these.
    try:
        from engine import learning_diagnostics as LD
        try:
            data["horizon_pnl"] = LD.horizon_pnl_attribution(user_id, mode)
        except Exception as _exc:
            print(f"[review_report] horizon_pnl slice failed: {_exc}",
                  file=sys.stderr)
        try:
            data["archetype_price_band"] = LD.archetype_price_band_pnl(user_id, mode)
        except Exception as _exc:
            print(f"[review_report] archetype_price_band slice failed: {_exc}",
                  file=sys.stderr)
        try:
            data["loss_day_recovery"] = LD.loss_day_recovery(user_id, mode)
        except Exception as _exc:
            print(f"[review_report] loss_day_recovery slice failed: {_exc}",
                  file=sys.stderr)
        try:
            data["loss_week_recovery"] = LD.loss_week_recovery(user_id, mode)
        except Exception as _exc:
            print(f"[review_report] loss_week_recovery slice failed: {_exc}",
                  file=sys.stderr)
        try:
            data["loss_streak"] = LD.loss_streak_analysis(user_id, mode)
        except Exception as _exc:
            print(f"[review_report] loss_streak slice failed: {_exc}",
                  file=sys.stderr)
        try:
            data["exit_threshold_sweep"] = LD.exit_threshold_backtest(user_id, mode)
        except Exception as _exc:
            print(f"[review_report] exit_threshold_sweep slice failed: {_exc}",
                  file=sys.stderr)
        try:
            data["exit_policy_attribution"] = LD.exit_policy_attribution(user_id, mode)
        except Exception as _exc:
            print(f"[review_report] exit_policy_attribution slice failed: {_exc}",
                  file=sys.stderr)
        try:
            data["aggregate_roi"] = LD.aggregate_roi_and_drawdown(user_id, mode)
        except Exception as _exc:
            print(f"[review_report] aggregate_roi slice failed: {_exc}",
                  file=sys.stderr)
    except Exception as _exc:
        print(f"[review_report] learning_diagnostics import failed: {_exc}",
              file=sys.stderr)

    rows = _fetch_settled_rows(user_id=user_id, mode=mode,
                               cycle_size=cycle_size)
    if not rows:
        return data

    # Window date range — earliest and latest settled_at in this slice.
    # ISO-8601 strings sort chronologically.
    settled_ats = sorted(
        (r.get("settled_at") for r in rows if r.get("settled_at")),
    )
    if settled_ats:
        data["window_start"] = settled_ats[0]
        data["window_end"]   = settled_ats[-1]

    n = len(rows)
    pnl_total  = sum(float(r["pnl"] or 0.0)  for r in rows)
    cost_total = sum(float(r["cost"] or 0.0) for r in rows) or 1.0
    wins = sum(1 for r in rows if (r["pnl"] or 0.0) > 0)
    roi = pnl_total / cost_total
    win_rate = wins / n if n else 0.0

    briers = [
        float(r["brier"]) for r in rows
        if r.get("brier") is not None
    ]
    brier_avg = sum(briers) / len(briers) if briers else None

    headline.update({
        "n":        n,
        "pnl_usd":  round(pnl_total, 2),
        "cost_usd": round(cost_total, 2),
        "roi":      roi,
        "win_rate": win_rate,
        "brier":    brier_avg,
    })

    # Per-archetype breakdown from THIS WINDOW's rows. The earlier
    # version pulled `archetype_pnl_attribution(user_id)` which is a
    # LIFETIME aggregate per user, so a 50-trade cycle review surfaced
    # archetype p&l from all 200+ trades. That's misleading: the
    # cycle card is about what happened in the cycle. Compute from the
    # window's rows so "Best archetype" / "Biggest drag" reflect the
    # cycle, not lifetime.
    arch_agg: dict[str, dict] = {}
    for r in rows:
        a = r.get("archetype") or "other"
        d = arch_agg.setdefault(
            a, {"archetype": a, "n": 0, "pnl": 0.0, "cost": 0.0, "wins": 0},
        )
        d["n"]    += 1
        d["pnl"]  += float(r.get("pnl") or 0.0)
        d["cost"] += float(r.get("cost") or 0.0)
        if (r.get("pnl") or 0.0) > 0:
            d["wins"] += 1
    per_arch = []
    for d in arch_agg.values():
        per_arch.append({
            "archetype": d["archetype"],
            "n":         d["n"],
            "pnl":       d["pnl"],
            "roi":       d["pnl"] / d["cost"] if d["cost"] > 0 else None,
            "win_rate":  d["wins"] / d["n"] if d["n"] else 0.0,
        })
    try:
        brier_by_arch = _diag.brier_by_archetype("all") or []
    except Exception as exc:
        print(f"[review_report] brier_by_arch failed: {exc}", file=sys.stderr)
        brier_by_arch = []
    # Pass the cycle's raw rows so the per-archetype shaper can run
    # bootstrap CI + power calc per cell. Surfaces ci_lo_pct,
    # ci_hi_pct, min_n_required, block_reason, verdict alongside the
    # aggregates - the dashboard uses these to mark thin/noisy cells.
    data["per_archetype"] = _shape_per_archetype(
        per_arch, brier_by_arch, raw_rows=rows,
    )

    # Calibration curve.
    try:
        cal = _diag.calibration_curve("all") or {}
    except Exception as exc:
        print(f"[review_report] calibration_curve failed: {exc}",
              file=sys.stderr)
        cal = {}
    data["calibration"] = _shape_calibration(cal)

    # Cost validation.
    try:
        data["cost_validation"] = _diag.cost_validation(user_id=user_id) or None
    except Exception as exc:
        print(f"[review_report] cost_validation failed: {exc}", file=sys.stderr)

    # Top wins / losses. Sort by absolute pnl descending and slice. The
    # admin copies keep the reasoning excerpt; the public copies strip it.
    sorted_by_pnl = sorted(rows, key=lambda r: float(r["pnl"] or 0.0),
                           reverse=True)
    wins_rows = [r for r in sorted_by_pnl if (r["pnl"] or 0.0) > 0][:_TOP_N_WINS]
    losses_rows = [r for r in reversed(sorted_by_pnl)
                   if (r["pnl"] or 0.0) < 0][:_TOP_N_LOSSES]

    data["top_wins"]         = [_shape_position(r, with_reasoning=False) for r in wins_rows]
    data["top_losses"]       = [_shape_position(r, with_reasoning=False) for r in losses_rows]
    data["top_wins_admin"]   = [_shape_position(r, with_reasoning=True)  for r in wins_rows]
    data["top_losses_admin"] = [_shape_position(r, with_reasoning=True)  for r in losses_rows]

    # Proposals queued as a side-effect of the learning cycle.
    try:
        data["proposals"] = _cadence.list_pending_suggestions(
            user_id=user_id, include_snoozed=False,
        ) or []
    except Exception as exc:
        print(f"[review_report] list_pending failed: {exc}", file=sys.stderr)

    # ── Exit-policy shaping ─────────────────────────────────────────
    # Walk closed-early rows in the cycle. For each, the early exit
    # realized `pnl`; for those where the resolver has back-filled
    # `counterfactual_pnl_usd`, we know what holding to natural
    # resolution would have made. counterfactual_pnl_usd is defined
    # as `hold_pnl - exit_pnl`, so `saved = exit_pnl - hold_pnl =
    # -counterfactual_pnl`. Positive `saved` means the exit was a
    # good call.
    early_rows = [r for r in rows if (r.get("status") or "") == "closed_early"]
    if early_rows:
        by_reason: dict[str, dict] = {}
        cf_pnl_sum = 0.0
        cf_n = 0
        for r in early_rows:
            reason = r.get("close_reason") or "unknown"
            d = by_reason.setdefault(reason, {
                "reason":   reason,
                "n":        0,
                "pnl":      0.0,
                "hold_pnl": 0.0,
                "saved":    0.0,
                "cf_n":     0,
            })
            d["n"]   += 1
            pnl_val = float(r.get("pnl") or 0.0)
            d["pnl"] += pnl_val
            cf = r.get("counterfactual_pnl")
            if cf is not None:
                # exit_pnl + cf == hold_pnl  =>  hold_pnl = pnl + cf
                hold_pnl = pnl_val + float(cf)
                d["hold_pnl"] += hold_pnl
                d["saved"]    += pnl_val - hold_pnl
                d["cf_n"]     += 1
                cf_pnl_sum    += float(cf)
                cf_n          += 1
        early_pnl_total = sum(float(r.get("pnl") or 0.0) for r in early_rows)
        exit_policy.update({
            "early_n":            len(early_rows),
            "early_pnl_usd":      round(early_pnl_total, 2),
            "counterfactual_n":   cf_n,
            "counterfactual_pnl": round(cf_pnl_sum, 2),
            "saved_vs_hold_usd": (
                None if cf_n == 0
                else round(sum(d["saved"] for d in by_reason.values()), 2)
            ),
            "by_reason": [
                {
                    "reason":   d["reason"],
                    "n":        d["n"],
                    "pnl":      round(d["pnl"],      2),
                    "hold_pnl": round(d["hold_pnl"], 2),
                    "saved":    round(d["saved"],    2),
                    "cf_n":     d["cf_n"],
                }
                for d in sorted(
                    by_reason.values(), key=lambda x: x["n"], reverse=True,
                )
            ],
        })

    data["settled_count"] = n
    data["verdict"] = _verdict(roi=roi, brier=brier_avg, n=n)
    return data


def _fetch_lifetime_stats(user_id: str, mode: str) -> dict:
    """Pull the lifetime numbers the dashboard's Performance page shows so
    the review's headline reconciles with what the user sees there.

    Returns the same keys as the lifetime block declared in
    `gather_cycle_data`. ROI uses the dashboard formula
    `(equity - starting_cash) / starting_cash`, which differs from the
    cycle-window ROI (`pnl/cost_staked`). Both are surfaced in the
    rendered report so the user can reconcile.
    """
    out = {
        "settled_total":  0,
        "wins":           0,
        "win_rate":       None,
        "realized_pnl":   0.0,
        "starting_cash":  None,
        "equity":         None,
        "roi":            None,
    }
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        from engine.user_config import get_user_config
    except Exception as exc:
        print(f"[review_report] lifetime stats import failed: {exc}",
              file=sys.stderr)
        return out

    try:
        cfg = get_user_config(user_id)
        starting = float(getattr(cfg, "starting_cash", 0.0) or 0.0)
    except Exception as exc:
        print(f"[review_report] starting_cash lookup failed: {exc}",
              file=sys.stderr)
        starting = 0.0

    try:
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT "
                "  COUNT(*) FILTER (WHERE status IN ('settled', 'invalid')) AS settled_n, "
                "  COUNT(*) FILTER (WHERE status IN ('settled', 'invalid') "
                "                    AND realized_pnl_usd > 0) AS wins, "
                "  COALESCE(SUM(realized_pnl_usd) "
                "           FILTER (WHERE status IN ('settled', 'invalid')), 0) AS realized "
                "FROM pm_positions WHERE user_id = :uid AND mode = :m"
            ), {"uid": user_id, "m": mode}).fetchone()
        settled_n = int((row[0] if row else 0) or 0)
        wins      = int((row[1] if row else 0) or 0)
        realized  = float((row[2] if row else 0.0) or 0.0)
    except Exception as exc:
        print(f"[review_report] lifetime stats query failed: {exc}",
              file=sys.stderr)
        return out

    equity = starting + realized
    roi = ((equity - starting) / starting) if starting > 0 else None

    out.update({
        "settled_total":  settled_n,
        "wins":           wins,
        "win_rate":       (wins / settled_n) if settled_n else None,
        "realized_pnl":   round(realized, 2),
        "starting_cash":  round(starting, 2),
        "equity":         round(equity, 2),
        "roi":            roi,
    })
    return out


def _fetch_settled_rows(user_id: str, mode: str,
                        cycle_size: int) -> list[dict]:
    """Most-recently-settled `cycle_size` positions in `mode` for `user_id`
    joined to their market_evaluation for the research reasoning excerpt."""
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            raw = conn.execute(text(
                "SELECT p.id, p.question, p.market_archetype, p.category, "
                "       p.side, p.shares, p.entry_price, p.cost_usd, "
                "       p.realized_pnl_usd, p.settlement_outcome, "
                "       p.delfi_probability, p.confidence, p.settled_at, "
                "       p.reasoning AS pos_reasoning, "
                "       e.reasoning AS eval_reasoning, "
                "       p.status, p.close_reason, p.counterfactual_pnl_usd "
                "FROM pm_positions p "
                "LEFT JOIN market_evaluations e ON e.pm_position_id = p.id "
                "WHERE p.user_id = :uid AND p.mode = :m "
                # `closed_early` rows carry realized P&L and a chosen
                # side, so the review treats them as settled. The
                # `exit_policy` block built in gather_cycle_data
                # surfaces them separately so the user can see how
                # the policy affected the cycle.
                "  AND p.status IN ('settled', 'invalid', 'closed_early') "
                "ORDER BY p.settled_at DESC LIMIT :lim"
            ), {"uid": user_id, "m": mode,
                "lim": int(cycle_size)}).fetchall()
    except Exception as exc:
        print(f"[review_report] fetch_settled_rows failed: {exc}",
              file=sys.stderr)
        return []

    out: list[dict] = []
    for r in raw:
        p_win = _chosen_side_probability(
            delfi_p=r[10], side=r[4],
        )
        outcome_bit = _outcome_bit(side=r[4], settlement=r[9])
        brier = None
        if p_win is not None and outcome_bit is not None:
            brier = (float(p_win) - float(outcome_bit)) ** 2

        out.append({
            "id":          int(r[0]),
            "question":    r[1] or "",
            "archetype":   r[2] or (r[3] or "other"),
            "side":        r[4],
            "shares":      float(r[5] or 0.0),
            "entry_price": float(r[6] or 0.0),
            "cost":        float(r[7] or 0.0),
            "pnl":         float(r[8] or 0.0),
            "outcome":     r[9],
            "p_win":       p_win,
            "confidence":  float(r[11]) if r[11] is not None else None,
            "settled_at":  iso_utc(r[12]),
            "reasoning":   (r[14] or r[13] or "").strip(),
            "brier":       brier,
            # Exit-policy fields. status is one of 'settled'/'invalid'/
            # 'closed_early'. close_reason is NULL for natural
            # settlements. counterfactual_pnl_usd is NULL until the
            # natural-settlement backfill stamps it.
            "status":               r[15] if r[15] is not None else "settled",
            "close_reason":         r[16] if r[16] is not None else None,
            "counterfactual_pnl":   float(r[17]) if r[17] is not None else None,
        })
    return out


def _chosen_side_probability(delfi_p, side) -> Optional[float]:
    """pm_positions stores delfi_probability as p(YES). Convert to the
    probability we assigned to the side we actually bought."""
    if delfi_p is None or side is None:
        return None
    p = float(delfi_p)
    s = str(side).upper()
    if s == "YES":
        return p
    if s == "NO":
        return 1.0 - p
    return None


def _outcome_bit(side, settlement) -> Optional[int]:
    if settlement is None:
        return None
    s = str(settlement).upper()
    if s == "INVALID":
        return None
    return 1 if s == str(side).upper() else 0


# ── Shaping helpers ──────────────────────────────────────────────────────────
def _shape_per_archetype(pnl_rows: list[dict],
                         brier_rows: list[dict],
                         raw_rows: list[dict] | None = None) -> list[dict]:
    """Per-archetype rows for the review report.

    When `raw_rows` is provided (the cycle's settled positions in the
    raw shape `{archetype, pnl, cost, ...}`), we compute per-cell
    bootstrap CI + power-calc sample-size required and surface those
    on every row. The dashboard renders these so the user can see why
    a cell with apparently-good ROI is still classified noise (CI
    overlaps the global mean, or n < min_n_required).

    Without raw_rows we fall back to the legacy aggregate-only shape -
    keeps backwards compat for older callers / saved reports that
    didn't include the raw data.
    """
    brier_map = {
        str(r.get("archetype") or "").lower(): r.get("brier")
        for r in brier_rows
    }

    # Optional CI augmentation. Lazy-import so the legacy path doesn't
    # pull in stats helpers when raw_rows is None.
    arch_to_rows: dict[str, list[dict]] = {}
    global_rows: list[dict] = []
    summarize_cell = None
    if raw_rows:
        try:
            from engine.stats import summarize_cell as _summarize_cell
            summarize_cell = _summarize_cell
            for r in raw_rows:
                # The cycle-level raw rows use {pnl, cost} (cycle math).
                # summarize_cell expects {realized_pnl_usd, cost_usd}.
                # Adapt rather than duplicate the cycle-aggregation code.
                row = {
                    "cost_usd":         float(r.get("cost") or 0.0),
                    "realized_pnl_usd": float(r.get("pnl")  or 0.0),
                }
                global_rows.append(row)
                a = r.get("archetype") or "other"
                arch_to_rows.setdefault(a, []).append(row)
        except Exception as exc:
            print(f"[review_report] CI augment failed: {exc}", file=sys.stderr)
            summarize_cell = None

    shaped: list[dict] = []
    for r in pnl_rows:
        key = str(r.get("archetype") or "").lower()
        out_row = {
            "archetype": r.get("archetype"),
            "n":         int(r.get("n") or 0),
            "pnl_usd":   float(r.get("pnl") or 0.0),
            "roi":       r.get("roi"),
            "win_rate":  r.get("win_rate"),
            "brier":     brier_map.get(key),
        }
        if summarize_cell is not None and global_rows:
            try:
                cell = arch_to_rows.get(r.get("archetype"), [])
                summary = summarize_cell(cell, global_rows)
                out_row["ci_lo_pct"]      = summary["ci_lo_pct"]
                out_row["ci_hi_pct"]      = summary["ci_hi_pct"]
                out_row["min_n_required"] = summary["min_n_required"]
                out_row["block_reason"]   = summary["block_reason"]
                out_row["verdict"]        = summary["verdict"]
            except Exception:
                pass
        shaped.append(out_row)
    shaped.sort(key=lambda x: x.get("pnl_usd") or 0.0, reverse=True)
    return shaped


def _shape_calibration(cal: dict) -> list[dict]:
    """Reshape `engine.diagnostics.calibration_curve` output for rendering.

    The diagnostic returns bins keyed `lo`, `hi`, `n`, `mean_pred`,
    `mean_actual`, `usable`. The renderer (`render_data_tables` and
    the API consumers) expect `bucket`, `n`, `avg_p`, `observed`.
    Synthesise `bucket` as "lo-hi" (e.g. "0.5-0.6"), and accept the
    earlier `bucket` / `label` / `avg_p` / `observed` / `observed_rate`
    field names too in case any caller reshapes upstream first.
    """
    bins = cal.get("bins") or []
    out: list[dict] = []
    for b in bins:
        bucket = b.get("bucket") or b.get("label")
        if bucket is None:
            lo = b.get("lo")
            hi = b.get("hi")
            if lo is not None and hi is not None:
                bucket = f"{float(lo):.1f}-{float(hi):.1f}"
        avg_p = b.get("avg_p")
        if avg_p is None:
            avg_p = b.get("mean_pred")
        observed = b.get("observed")
        if observed is None:
            observed = b.get("observed_rate")
        if observed is None:
            observed = b.get("mean_actual")
        out.append({
            "bucket":   bucket,
            "n":        int(b.get("n") or 0),
            "avg_p":    avg_p,
            "observed": observed,
        })
    return out


def _shape_position(r: dict, with_reasoning: bool) -> dict:
    out = {
        "id":          r["id"],
        "question":    r["question"],
        "archetype":   r["archetype"],
        "side":        r["side"],
        "cost_usd":    round(r["cost"], 2),
        "pnl_usd":     round(r["pnl"], 2),
        "outcome":     r["outcome"],
        "p_win":       r.get("p_win"),
    }
    if with_reasoning:
        excerpt = (r.get("reasoning") or "")[:_REASONING_EXCERPT_CHARS]
        out["reasoning_excerpt"] = excerpt
    return out


def _verdict(roi: float, brier: Optional[float], n: int) -> str:
    """Three buckets: profitable / breakeven / unprofitable.

    Earlier versions split ROI into six tiers plus a `mis_calibrated`
    branch off Brier; the user found that overgranular and confusing.
    Three labels match the way profit is read end-to-end on every other
    surface (Performance page, daily/weekly summaries).
    """
    if roi > 0.01:
        return "profitable"
    if roi < -0.01:
        return "unprofitable"
    return "breakeven"


# ── Thesis generation ────────────────────────────────────────────────────────
def generate_thesis(data: dict) -> str:
    """2-3 sentence narration. Delegates to the forecaster connection
    (any provider) when one is wired; otherwise returns a deterministic
    fallback. Either path is passed through `_sanitise_thesis` so banned
    terminology can never leak to the user."""
    try:
        raw = _generate_thesis_via_model(data)
    except Exception as exc:
        print(f"[review_report] thesis model call failed: {exc}",
              file=sys.stderr)
        raw = _fallback_thesis(data)
    return _sanitise_thesis(raw or _fallback_thesis(data))


def _call_model_blocking(system: str, user: str, max_tokens: int) -> Optional[str]:
    """Run the async LLM client to completion from this sync function.

    compose_report runs in a worker thread (run_in_executor from
    local_api) or as a plain sync call from pm_executor, so there is
    normally no event loop running on this thread and asyncio.run is
    safe. If a loop does happen to be running here, offload to a
    dedicated thread so we never block it.
    """
    from engine.llm_client import get_llm

    async def _coro():
        return await get_llm().call(
            system      = system,
            user        = user,
            max_tokens  = max_tokens,
            temperature = 1.0,
            use_case    = "forecaster",
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_coro())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(_coro())).result()


def _generate_thesis_via_model(data: dict) -> str:
    """Narrate via the forecaster connection (any provider). Falls back
    to the deterministic thesis when no forecaster is wired or the call
    returns nothing."""
    from engine.user_config import has_forecaster_connection
    if not has_forecaster_connection():
        return _fallback_thesis(data)
    user_block = _thesis_user_block(data)
    text = _call_model_blocking(_THESIS_SYSTEM, user_block, 400)
    return (text or "").strip() or _fallback_thesis(data)


def _thesis_user_block(data: dict) -> str:
    headline = data.get("headline") or {}
    per_arch = data.get("per_archetype") or []
    top_arch = per_arch[:3]
    worst_arch = [r for r in per_arch if (r.get("pnl_usd") or 0.0) < 0][-3:]
    cv = data.get("cost_validation") or {}

    lines = [
        "Last 50 settled trades:",
        f"- Net PnL: ${headline.get('pnl_usd'):.2f} on "
        f"${headline.get('cost_usd'):.2f} staked "
        f"(ROI {_pct(headline.get('roi'))}).",
        f"- Win rate: {_pct(headline.get('win_rate'))} over "
        f"{headline.get('n')} trades.",
        f"- Average Brier: {_fmt_brier(headline.get('brier'))}.",
    ]
    if top_arch:
        items = ", ".join(
            f"{r['archetype']} (${r['pnl_usd']:.0f} on n={r['n']})"
            for r in top_arch if r.get("archetype")
        )
        lines.append(f"- Best archetypes by PnL: {items}.")
    if worst_arch:
        items = ", ".join(
            f"{r['archetype']} (${r['pnl_usd']:.0f} on n={r['n']})"
            for r in worst_arch if r.get("archetype")
        )
        lines.append(f"- Worst archetypes by PnL: {items}.")
    if cv.get("implied_cost") is not None and cv.get("assumed_cost") is not None:
        lines.append(
            f"- Implied cost {_pct(cv.get('implied_cost'))} vs assumed "
            f"{_pct(cv.get('assumed_cost'))} over n={cv.get('n') or 0}."
        )
    # Exit-policy hint to the thesis writer: if there were early
    # exits and we have backfilled counterfactual P&L for some of
    # them, surface the "saved vs hold" number so the prose can say
    # whether the policy added or destroyed value this cycle.
    exit_policy = data.get("exit_policy") or {}
    early_n = int(exit_policy.get("early_n") or 0)
    if early_n > 0:
        early_pnl = float(exit_policy.get("early_pnl_usd") or 0.0)
        cf_n      = int(exit_policy.get("counterfactual_n") or 0)
        saved     = exit_policy.get("saved_vs_hold_usd")
        by_reason = exit_policy.get("by_reason") or []
        parts = [f"{r['n']} {r['reason']}" for r in by_reason]
        breakdown = ", ".join(parts) if parts else f"{early_n} early exits"
        line = (
            f"- Exit policy: {early_n} positions closed early "
            f"(${early_pnl:+.2f} realized; {breakdown})"
        )
        if cf_n > 0 and saved is not None:
            line += (
                f". Counterfactual on {cf_n} resolved markets: "
                f"${saved:+.2f} saved vs holding to natural resolution"
            )
        lines.append(line + ".")
    lines.append(f"Verdict tier: {data.get('verdict')}.")
    lines.append(
        "Write a 2-3 sentence thesis for the user that names the biggest "
        "driver of profit or loss, the biggest calibration issue if any, "
        "and what Delfi will focus on next. If the exit-policy 'saved vs "
        "hold' figure is meaningful, comment on whether the policy added "
        "or destroyed value this cycle. Plain prose only."
    )
    return "\n".join(lines)


def _fallback_thesis(data: dict) -> str:
    """Deterministic narrator. Coherent across all verdict tiers.

    The earlier version wrote "biggest single driver was X (loss of $32)"
    while also saying "running profitably". When the largest absolute
    archetype p&l happens to be a loss but the cycle was net positive
    overall, that reads as a contradiction. The fix is to surface the
    biggest WIN and biggest LOSS separately and label each correctly.
    """
    headline = data.get("headline") or {}
    roi = headline.get("roi") or 0.0
    pnl = headline.get("pnl_usd") or 0.0
    n   = headline.get("n") or 0
    brier = headline.get("brier")
    verdict = data.get("verdict")

    if n == 0:
        return (
            "No settled trades in the last cycle window. Delfi will "
            "resume narrating once markets resolve."
        )

    # Opener language scales with the 3-tier verdict so the prose
    # never reads "profitable" while the pill says "breakeven" (or
    # vice versa). One template per tier. No hedging.
    pnl_signed = f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}"
    headline_phrase = (
        f"{pnl_signed} on {n} settled trades (ROI {_pct(roi)})"
    )
    if verdict == "profitable":
        opener = f"Delfi was profitable this cycle: {headline_phrase}."
    elif verdict == "unprofitable":
        opener = f"Delfi was unprofitable this cycle: {headline_phrase}."
    else:  # breakeven
        opener = f"Delfi finished roughly breakeven this cycle: {headline_phrase}."

    per_arch = data.get("per_archetype") or []
    biggest_win = None
    biggest_loss = None
    if per_arch:
        sorted_by_pnl = sorted(
            per_arch,
            key=lambda r: float(r.get("pnl_usd") or 0.0),
            reverse=True,
        )
        if sorted_by_pnl:
            top = sorted_by_pnl[0]
            if (top.get("pnl_usd") or 0.0) > 0 and top.get("archetype"):
                biggest_win = top
            bot = sorted_by_pnl[-1]
            if (bot.get("pnl_usd") or 0.0) < 0 and bot.get("archetype"):
                biggest_loss = bot

    driver_parts: list[str] = []
    if biggest_win:
        driver_parts.append(
            f"Best archetype: {biggest_win['archetype']} "
            f"(+${biggest_win['pnl_usd']:.0f} on n={biggest_win.get('n')})"
        )
    if biggest_loss:
        verb = "Biggest drag" if pnl >= 0 else "Biggest loss"
        driver_parts.append(
            f"{verb}: {biggest_loss['archetype']} "
            f"(-${abs(biggest_loss['pnl_usd']):.0f} on n={biggest_loss.get('n')})"
        )
    drivers = ""
    if driver_parts:
        drivers = " " + ". ".join(driver_parts) + "."

    brier_phrase = ""
    if brier is not None:
        brier_phrase = f" Average Brier {_fmt_brier(brier)}."

    if verdict == "mis_calibrated":
        focus = (
            " Next cycle will tighten the forecast filter on archetypes "
            "where Brier exceeds the uninformed baseline."
        )
    elif verdict in ("deeply_unprofitable", "mildly_unprofitable"):
        focus = (
            " Next cycle will trim sizing on losing archetypes and "
            "review the skip list."
        )
    else:
        focus = (
            " Next cycle continues the V1 follow-market filter; sizing "
            "tweaks will queue for any archetype crossing thresholds."
        )

    return opener + drivers + brier_phrase + focus


def _sanitise_thesis(text_val: str) -> str:
    if not text_val:
        return ""
    cleaned = text_val.replace("—", " - ").replace("–", " - ")
    for word in _BANNED_WORDS:
        pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
        cleaned = pattern.sub(_BANNED_REPLACEMENT, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > _THESIS_MAX_CHARS:
        cleaned = cleaned[:_THESIS_MAX_CHARS].rstrip()
        if not cleaned.endswith("."):
            cleaned += "."
    return cleaned


# ── Deterministic tables ─────────────────────────────────────────────────────
def render_data_tables(data: dict) -> str:
    headline = data.get("headline") or {}
    lifetime = data.get("lifetime") or {}
    lines: list[str] = []

    # Lifetime block matches the dashboard's Performance page exactly so
    # the two surfaces reconcile. ROI here is `(equity - start)/start`.
    if lifetime.get("settled_total") or lifetime.get("starting_cash"):
        lines.append("LIFETIME (matches dashboard)")
        lines.append(
            f"  Trades settled:   {int(lifetime.get('settled_total') or 0)}"
        )
        if lifetime.get("starting_cash") is not None:
            lines.append(
                f"  Starting cash:    "
                f"${float(lifetime.get('starting_cash') or 0.0):.2f}"
            )
        if lifetime.get("equity") is not None:
            lines.append(
                f"  Equity:           "
                f"${float(lifetime.get('equity') or 0.0):.2f}"
            )
        lines.append(
            f"  Realized PnL:     "
            f"${float(lifetime.get('realized_pnl') or 0.0):.2f}"
        )
        lines.append(
            f"  ROI (lifetime):   {_pct(lifetime.get('roi'))}"
        )
        lines.append(
            f"  Win rate:         {_pct(lifetime.get('win_rate'))}"
        )
        lines.append("")

    cycle_n = int(headline.get('n') or 0)
    cycle_label = (
        f"THIS CYCLE (last {cycle_n} settled trades)"
        if cycle_n
        else "THIS CYCLE"
    )
    lines.append(cycle_label)
    lines.append(
        f"  Trades settled:   {cycle_n}"
    )
    lines.append(
        f"  Net PnL:          ${float(headline.get('pnl_usd') or 0.0):.2f}"
    )
    lines.append(
        f"  Capital staked:   ${float(headline.get('cost_usd') or 0.0):.2f}"
    )
    lines.append(
        f"  ROI on staked:    {_pct(headline.get('roi'))}"
    )
    lines.append(
        f"  Win rate:         {_pct(headline.get('win_rate'))}"
    )
    lines.append(
        f"  Avg Brier:        {_fmt_brier(headline.get('brier'))}"
    )
    lines.append(f"  Verdict:          {data.get('verdict')}")

    per_arch = data.get("per_archetype") or []
    if per_arch:
        lines.append("")
        lines.append("PER-ARCHETYPE")
        lines.append(
            f"  {'archetype':<18}{'n':>5}{'roi':>10}{'pnl':>10}{'brier':>10}"
        )
        for row in per_arch[:8]:
            lines.append(
                f"  {str(row.get('archetype') or '-'):<18}"
                f"{int(row.get('n') or 0):>5}"
                f"{_pct(row.get('roi')):>10}"
                f"${float(row.get('pnl_usd') or 0.0):>8.2f}"
                f"{_fmt_brier(row.get('brier')):>10}"
            )

    wins = data.get("top_wins") or []
    if wins:
        lines.append("")
        lines.append("TOP WINS")
        for w in wins:
            lines.append(
                f"  +${float(w.get('pnl_usd') or 0.0):>7.2f}  "
                f"{(w.get('question') or '')[:64]}"
            )

    losses = data.get("top_losses") or []
    if losses:
        lines.append("")
        lines.append("TOP LOSSES")
        for l in losses:
            lines.append(
                f"  -${abs(float(l.get('pnl_usd') or 0.0)):>7.2f}  "
                f"{(l.get('question') or '')[:64]}"
            )

    cal = data.get("calibration") or []
    if cal:
        lines.append("")
        lines.append("CALIBRATION")
        lines.append(
            f"  {'bucket':<14}{'n':>5}{'avg_p':>10}{'observed':>12}"
        )
        for c in cal:
            lines.append(
                f"  {str(c.get('bucket') or '-'):<14}"
                f"{int(c.get('n') or 0):>5}"
                f"{_pct(c.get('avg_p')):>10}"
                f"{_pct(c.get('observed')):>12}"
            )

    cv = data.get("cost_validation") or {}
    if cv and cv.get("n"):
        lines.append("")
        lines.append("COST VALIDATION")
        lines.append(
            f"  n={int(cv.get('n') or 0)}   "
            f"assumed={_pct(cv.get('assumed_cost'))}   "
            f"implied={_pct(cv.get('implied_cost'))}"
        )

    exit_policy = data.get("exit_policy") or {}
    if int(exit_policy.get("early_n") or 0) > 0:
        lines.append("")
        lines.append("EXIT POLICY")
        lines.append(
            f"  Positions closed early: {int(exit_policy.get('early_n') or 0)}"
        )
        lines.append(
            f"  Realized PnL on exits:  "
            f"${float(exit_policy.get('early_pnl_usd') or 0.0):+.2f}"
        )
        saved = exit_policy.get("saved_vs_hold_usd")
        cf_n  = int(exit_policy.get("counterfactual_n") or 0)
        if saved is not None and cf_n > 0:
            lines.append(
                f"  Saved vs hold ({cf_n} resolved): ${float(saved):+.2f}"
            )
        by_reason = exit_policy.get("by_reason") or []
        if by_reason:
            lines.append(
                f"  {'reason':<14}{'n':>5}{'exit_pnl':>12}"
                f"{'hold_pnl':>12}{'saved':>10}"
            )
            for r in by_reason:
                hold = f"${float(r.get('hold_pnl') or 0.0):+.2f}" if r.get("cf_n") else "n/a"
                saved_cell = f"${float(r.get('saved') or 0.0):+.2f}" if r.get("cf_n") else "n/a"
                lines.append(
                    f"  {str(r.get('reason') or '-'):<14}"
                    f"{int(r.get('n') or 0):>5}"
                    f"${float(r.get('pnl') or 0.0):>10.2f}"
                    f"{hold:>12}"
                    f"{saved_cell:>10}"
                )

    proposals = data.get("proposals") or []
    if proposals:
        lines.append("")
        lines.append("PROPOSALS QUEUED")
        for p in proposals[:6]:
            lines.append(
                f"  - {p.get('param_name')}: "
                f"{p.get('current_value')} -> {p.get('proposed_value')}"
            )

    return "\n".join(lines)


def render_admin_excerpts(data: dict) -> str:
    wins = data.get("top_wins_admin") or []
    losses = data.get("top_losses_admin") or []
    if not wins and not losses:
        return ""

    lines: list[str] = ["MODEL REASONING (admin only)"]
    for w in wins:
        ex = (w.get("reasoning_excerpt") or "").strip()
        if not ex:
            continue
        lines.append(
            f"\nWIN +${float(w.get('pnl_usd') or 0.0):.2f}  "
            f"{(w.get('question') or '')[:80]}"
        )
        lines.append(f"  {ex}")
    for l in losses:
        ex = (l.get("reasoning_excerpt") or "").strip()
        if not ex:
            continue
        lines.append(
            f"\nLOSS -${abs(float(l.get('pnl_usd') or 0.0)):.2f}  "
            f"{(l.get('question') or '')[:80]}"
        )
        lines.append(f"  {ex}")
    return "\n".join(lines)


# ── Formatting helpers ───────────────────────────────────────────────────────
def _pct(v) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_brier(v) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return "-"


def _jsonable(obj: Any) -> Any:
    """Recursively coerce objects (datetimes, Decimals, tuples) into
    JSON-safe primitives before sending to JSONB."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(x) for x in obj]
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)
