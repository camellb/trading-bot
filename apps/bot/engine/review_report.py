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

import json
import os
import re
import sys
from typing import Any, Optional

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
    """INSERT one row into learning_reports. Returns the new id or None
    on failure (the learning cycle must not crash because persistence
    was flaky)."""
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        data_json = json.dumps(_jsonable(report.get("data") or {}))
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "INSERT INTO learning_reports "
                "(user_id, mode, settled_count, thesis, summary_user, "
                " summary_admin, data) "
                "VALUES (:uid, :m, :sc, :th, :su, :sa, CAST(:d AS JSONB)) "
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
        print(f"[review_report] save_report failed: {exc}", file=sys.stderr)
        return None


def list_learning_reports(user_id: str,
                          limit: int = 10,
                          include_admin: bool = False) -> list[dict]:
    """Newest-first. `include_admin=True` exposes the reasoning-bearing
    variant and is gated by the caller (admin dashboard only)."""
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        cols = [
            "id", "created_at", "mode", "settled_count",
            "thesis", "summary_user",
        ]
        if include_admin:
            cols += ["summary_admin", "data"]
        sql = (
            f"SELECT {', '.join(cols)} FROM learning_reports "
            "WHERE user_id = :uid ORDER BY created_at DESC LIMIT :lim"
        )
        with get_engine().begin() as conn:
            rows = conn.execute(
                text(sql), {"uid": user_id, "lim": int(limit)},
            ).fetchall()
    except Exception as exc:
        print(f"[review_report] list_reports failed: {exc}", file=sys.stderr)
        return []

    out: list[dict] = []
    for r in rows:
        item = {
            "id":            int(r[0]),
            "created_at":    r[1].isoformat() if r[1] else None,
            "mode":          r[2],
            "settled_count": int(r[3] or 0),
            "thesis":        r[4],
            "summary_user":  r[5],
        }
        if include_admin:
            item["summary_admin"] = r[6]
            raw = r[7]
            if isinstance(raw, (str, bytes)):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    raw = None
            item["data"] = raw
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
    data: dict[str, Any] = {
        "mode":               mode,
        "cycle_size":         cycle_size,
        "settled_count":      0,
        "headline":           headline,
        "per_archetype":      [],
        "calibration":        [],
        "cost_validation":    None,
        "top_wins":           [],
        "top_losses":         [],
        "top_wins_admin":     [],
        "top_losses_admin":   [],
        "proposals":          [],
        "verdict":            "insufficient_data",
    }

    rows = _fetch_settled_rows(user_id=user_id, mode=mode,
                               cycle_size=cycle_size)
    if not rows:
        return data

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

    # Per-archetype breakdown from diagnostics (canonical numbers) with the
    # cycle-window PnL attribution inlined for display.
    try:
        per_arch = _diag.archetype_pnl_attribution() or []
    except Exception as exc:
        print(f"[review_report] archetype_pnl failed: {exc}", file=sys.stderr)
        per_arch = []
    try:
        brier_by_arch = _diag.brier_by_archetype("all") or []
    except Exception as exc:
        print(f"[review_report] brier_by_arch failed: {exc}", file=sys.stderr)
        brier_by_arch = []
    data["per_archetype"] = _shape_per_archetype(per_arch, brier_by_arch)

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
        data["cost_validation"] = _diag.cost_validation() or None
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

    data["settled_count"] = n
    data["verdict"] = _verdict(roi=roi, brier=brier_avg, n=n)
    return data


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
                "       p.claude_probability, p.confidence, p.settled_at, "
                "       p.reasoning AS pos_reasoning, "
                "       e.reasoning AS eval_reasoning "
                "FROM pm_positions p "
                "LEFT JOIN market_evaluations e ON e.pm_position_id = p.id "
                "WHERE p.user_id = :uid AND p.mode = :m "
                "  AND p.status IN ('settled', 'invalid') "
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
            claude_p=r[10], side=r[4],
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
            "settled_at":  r[12].isoformat() if r[12] else None,
            "reasoning":   (r[14] or r[13] or "").strip(),
            "brier":       brier,
        })
    return out


def _chosen_side_probability(claude_p, side) -> Optional[float]:
    """pm_positions stores claude_probability as p(YES). Convert to the
    probability we assigned to the side we actually bought."""
    if claude_p is None or side is None:
        return None
    p = float(claude_p)
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
                         brier_rows: list[dict]) -> list[dict]:
    brier_map = {
        str(r.get("archetype") or "").lower(): r.get("brier")
        for r in brier_rows
    }
    shaped: list[dict] = []
    for r in pnl_rows:
        key = str(r.get("archetype") or "").lower()
        shaped.append({
            "archetype": r.get("archetype"),
            "n":         int(r.get("n") or 0),
            "pnl_usd":   float(r.get("pnl_usd") or 0.0),
            "roi":       r.get("roi"),
            "win_rate":  r.get("win_rate"),
            "brier":     brier_map.get(key),
        })
    shaped.sort(key=lambda x: x.get("pnl_usd") or 0.0, reverse=True)
    return shaped


def _shape_calibration(cal: dict) -> list[dict]:
    bins = cal.get("bins") or []
    out: list[dict] = []
    for b in bins:
        out.append({
            "bucket":       b.get("bucket") or b.get("label"),
            "n":            int(b.get("n") or 0),
            "avg_p":        b.get("avg_p"),
            "observed":     b.get("observed") or b.get("observed_rate"),
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
    if n < 20:
        return "insufficient_data"
    if brier is not None and brier > 0.25:
        return "mis_calibrated"
    if roi >= 0.15:
        return "strongly_profitable"
    if roi >= 0.05:
        return "profitable"
    if roi <= -0.10:
        return "deeply_unprofitable"
    if roi <= -0.03:
        return "mildly_unprofitable"
    return "neutral"


# ── Thesis generation ────────────────────────────────────────────────────────
def generate_thesis(data: dict) -> str:
    """2-3 sentence narration. Delegates to the configured model when an
    anthropic client is available; otherwise returns a deterministic
    fallback. Either path is passed through `_sanitise_thesis` so banned
    terminology can never leak to the user."""
    try:
        raw = _generate_thesis_via_model(data)
    except Exception as exc:
        print(f"[review_report] thesis model call failed: {exc}",
              file=sys.stderr)
        raw = _fallback_thesis(data)
    return _sanitise_thesis(raw or _fallback_thesis(data))


def _generate_thesis_via_model(data: dict) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _fallback_thesis(data)
    try:
        import anthropic
    except Exception:
        return _fallback_thesis(data)

    from config import CLAUDE_MODEL
    client = anthropic.Anthropic()
    user_block = _thesis_user_block(data)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=400,
        system=_THESIS_SYSTEM,
        messages=[{"role": "user", "content": user_block}],
    )
    parts = []
    for block in getattr(resp, "content", []) or []:
        text_val = getattr(block, "text", None)
        if text_val:
            parts.append(text_val)
    return ("".join(parts)).strip()


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
    lines.append(f"Verdict tier: {data.get('verdict')}.")
    lines.append(
        "Write a 2-3 sentence thesis for the user that names the biggest "
        "driver of profit or loss, the biggest calibration issue if any, "
        "and what Delfi will focus on next. Plain prose only."
    )
    return "\n".join(lines)


def _fallback_thesis(data: dict) -> str:
    headline = data.get("headline") or {}
    roi = headline.get("roi") or 0.0
    pnl = headline.get("pnl_usd") or 0.0
    n   = headline.get("n") or 0
    brier = headline.get("brier")

    if n == 0:
        return (
            "No settled trades in the last cycle window. Delfi will "
            "resume narrating once markets resolve."
        )

    pnl_phrase = (
        f"returned ${pnl:.2f} on {n} settled trades "
        f"(ROI {_pct(roi)})"
    )

    verdict = data.get("verdict")
    if verdict == "strongly_profitable":
        tone = "Delfi is trading well above its own baseline. "
    elif verdict == "profitable":
        tone = "Delfi is running profitably. "
    elif verdict == "neutral":
        tone = "Delfi is hovering near break-even. "
    elif verdict == "mildly_unprofitable":
        tone = "Delfi is giving up a small edge to the market. "
    elif verdict == "deeply_unprofitable":
        tone = "Delfi is losing materially. "
    elif verdict == "mis_calibrated":
        tone = "Delfi's probabilities are mis-calibrated across the window. "
    else:
        tone = "Delfi has limited data this cycle. "

    per_arch = data.get("per_archetype") or []
    biggest = max(
        per_arch,
        key=lambda r: abs(r.get("pnl_usd") or 0.0),
        default=None,
    ) if per_arch else None
    biggest_phrase = ""
    if biggest and biggest.get("archetype"):
        direction = "profit" if (biggest.get("pnl_usd") or 0.0) >= 0 else "loss"
        biggest_phrase = (
            f" The biggest single driver was the '{biggest['archetype']}' "
            f"archetype ({direction} of ${abs(biggest['pnl_usd']):.0f} on "
            f"n={biggest.get('n')})."
        )

    brier_phrase = ""
    if brier is not None:
        brier_phrase = f" Average Brier came in at {_fmt_brier(brier)}."

    focus = (
        " Next cycle will refine sizing on profitable archetypes and "
        "prune any archetype whose Brier stays above the uninformed "
        "baseline."
    )
    return (tone + "Delfi " + pnl_phrase + "." + biggest_phrase
            + brier_phrase + focus)


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
    lines: list[str] = []

    lines.append("CYCLE SUMMARY")
    lines.append(
        f"  Trades settled:   {headline.get('n', 0)}"
    )
    lines.append(
        f"  Net PnL:          ${float(headline.get('pnl_usd') or 0.0):.2f}"
    )
    lines.append(
        f"  Capital staked:   ${float(headline.get('cost_usd') or 0.0):.2f}"
    )
    lines.append(
        f"  ROI:              {_pct(headline.get('roi'))}"
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
