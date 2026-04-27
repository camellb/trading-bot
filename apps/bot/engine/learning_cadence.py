"""
Trade-volume-based learning cadence.

Every 50 settled trades the bot runs a full analysis pass, proposes
user_config tweaks with backtester evidence, and stores them in the
pending_suggestions table for the user to Apply, Skip, or Snooze on the
dashboard.

Calendar-based tuning (weekly / monthly) proposes changes on whatever
sample happens to have accumulated in that window, which is too often
too small and noisy. Trade-volume gating guarantees every suggestion is
backed by a meaningful sample. An active bot might hit 50 trades in days;
a quiet one might take weeks - either way, suggestions arrive when the
data justifies them.

Proposers are deterministic heuristics for now. Each proposer follows the
same rule: never emit a suggestion for a bucket with n < 20.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from dataclasses import dataclass
from typing import Optional

from engine.user_config import (
    ARCHETYPE_MULTIPLIER_BOUNDS,
    DEFAULT_USER_ID,
    USER_CONFIG_BOUNDS,
    UserConfig,
    get_user_config,
    update_user_config,
)

# Minimum new settled trades required before a learning cycle runs.
LEARNING_CYCLE_TRADE_INTERVAL = 50

# Minimum sample size per bucket before a proposer may emit a suggestion.
MIN_BUCKET_N = 20

# Stricter per-diagnostic gates (in trade-count) for proposers where the
# downside of acting on a noisy bucket is higher.
STRICT_BUCKET_N = 30          # archetype-level decisions.
COST_CORRECTION_MIN_N = 50    # cost assumption correction.

# Proposals whose target field is list- or dict-shaped. `_attach_backtest_delta`
# still builds a modified config and runs the simulation, but these proposals
# surface without a numeric backtest_delta in the dashboard for now.
ADVISORY_PARAMS = {
    "archetype_skip_list",
    "archetype_stake_multipliers",
}

# Minimum settled-trade count per archetype before the multiplier proposer
# will emit a suggestion. 25 is higher than MIN_BUCKET_N (20) because a
# bad multiplier compounds with every trade on that archetype.
ARCHETYPE_MULTIPLIER_MIN_N = 25

# ROI bands → proposed multiplier. Anything inside the neutral band
# (-3% to +5%) is treated as noise and produces no proposal. Outside the
# band, the bot halves / upsizes stake progressively.
ARCHETYPE_MULTIPLIER_TIERS: list[tuple[float, float, float]] = [
    #   (roi_lo,     roi_hi,    multiplier)
    (float("-inf"), -0.10,     0.5),   # deeply unprofitable: half size
    (-0.10,         -0.03,     0.75),  # mildly unprofitable: 3/4 size
    ( 0.05,          0.15,     1.25),  # reliably profitable: 1.25x
    ( 0.15,  float("inf"),     1.5),   # strongly profitable: 1.5x
]

# Don't re-emit a proposal whose proposed multiplier differs from the
# currently applied one by less than this amount. Prevents flickering
# between adjacent tiers when ROI drifts across a boundary.
ARCHETYPE_MULTIPLIER_HYSTERESIS = 0.1

# 0.25 is the Brier score of uninformed forecasts on binary outcomes; an
# archetype scoring worse than that is demonstrably mis-forecast.
ARCHETYPE_BRIER_THRESHOLD = 0.25

# Realised-vs-assumed cost gap meaningful enough to act on.
COST_DELTA_THRESHOLD = 0.005


@dataclass
class Proposal:
    param_name:     str
    current_value:  Optional[float]
    proposed_value: Optional[float]
    evidence:       str
    backtest_delta: Optional[float] = None
    backtest_trades: Optional[int]   = None
    # In-memory only: carries the specific item (archetype, etc.) and a
    # suggested new tuple/value that `_attach_backtest_delta` can use to
    # construct the modified UserConfig for the simulation.
    proposal_metadata: Optional[dict] = None


# ── Public entry points ──────────────────────────────────────────────────────
def maybe_run_learning_cycle(user_id: str = DEFAULT_USER_ID,
                             mode: str = "simulation") -> dict:
    """
    Called after every settlement. Runs the pipeline iff the trade-volume
    gate has been crossed since the last cycle. Returns a status dict.

    Per-user advisory lock (`pg_advisory_xact_lock(hashtext(user_id))`)
    serialises concurrent cycles for the same user so two settlements
    landing near-simultaneously cannot both pass the gate and emit duplicate
    proposals. Cycles for different users stay fully parallel.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine

        # Hold a dedicated transaction only to own the advisory lock. All
        # sub-helpers (_count_settled_trades, _store_pending_suggestion, ...)
        # open their own short-lived connections, so this lock-holding
        # transaction never touches application tables.
        with get_engine().begin() as lock_conn:
            lock_conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:uid))"),
                {"uid": user_id},
            )

            settled_now = _count_settled_trades(user_id, mode)
            last_cycle = _last_cycle_settled_count(user_id, mode)
            delta = settled_now - last_cycle
            if delta < LEARNING_CYCLE_TRADE_INTERVAL:
                return {
                    "status":      "gate_not_crossed",
                    "settled_now": settled_now,
                    "since_last":  delta,
                    "threshold":   LEARNING_CYCLE_TRADE_INTERVAL,
                }

            stats = _gather_stats(user_id, mode,
                                  limit=LEARNING_CYCLE_TRADE_INTERVAL)
            current_cfg = get_user_config(user_id)
            proposals = propose_suggestions(stats, current_cfg, user_id=user_id)

            # Enrich each with the backtester delta.
            for prop in proposals:
                _attach_backtest_delta(prop, current_cfg)

            stored = 0
            for prop in proposals:
                if _store_pending_suggestion(prop, user_id=user_id,
                                              settled_count=settled_now):
                    stored += 1

            report_id = _compose_and_deliver_report(
                user_id=user_id, mode=mode,
                cycle_size=LEARNING_CYCLE_TRADE_INTERVAL,
            )

            return {
                "status":       "ran",
                "settled_now":  settled_now,
                "since_last":   delta,
                "proposals":    len(proposals),
                "stored":       stored,
                "report_id":    report_id,
            }
    except Exception as exc:
        print(f"[learning_cadence] maybe_run failed: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc)}


def propose_suggestions(stats: dict, current: UserConfig,
                        diag: Optional[dict] = None,
                        user_id: Optional[str] = None) -> list[Proposal]:
    """
    Deterministic heuristic proposers.

    `stats`   - recent-window aggregate produced by `_gather_stats`.
    `diag`    - optional dict of diagnostic slices (see `_collect_diagnostics`).
                Tests may pass synthetic slices; in production it is fetched
                lazily if omitted.
    `user_id` - scopes the diagnostic pull to this tenant's positions so the
                archetype/cost proposers see only this user's trading history.

    No single rule emits a suggestion unless its underlying bucket meets its
    own sample-size gate.
    """
    out: list[Proposal] = []

    recent = stats.get("recent_window") or {}
    n = int(recent.get("n", 0))
    if n < MIN_BUCKET_N:
        return out

    # Drawdown pressure → cut max stake.
    peak_dd = float(recent.get("peak_drawdown_pct", 0.0))
    if peak_dd >= 0.25 and current.max_stake_pct > 0.02:
        lo, hi = USER_CONFIG_BOUNDS["max_stake_pct"]
        proposed = max(round(current.max_stake_pct * 0.7, 4), lo)
        if proposed < current.max_stake_pct - 0.005:
            out.append(Proposal(
                param_name="max_stake_pct",
                current_value=current.max_stake_pct,
                proposed_value=proposed,
                evidence=(
                    f"Peak drawdown over last {n} trades: {peak_dd*100:.1f}%. "
                    f"Reducing max_stake_pct from {current.max_stake_pct*100:.1f}% "
                    f"to {proposed*100:.1f}% limits single-trade risk."
                ),
                proposal_metadata={
                    "operation": "scalar_set",
                    "field":     "max_stake_pct",
                    "value":     proposed,
                },
            ))

    # ── Diagnostic-driven proposers ────────────────────────────────────────
    # Lazy-load on first access so unit tests can stub a synthetic diag.
    if diag is None:
        diag = _collect_diagnostics(user_id=user_id)

    out.extend(_propose_archetype_threshold(diag, current))
    out.extend(_propose_cost_correction(diag, current))
    out.extend(_propose_archetype_stake_multiplier(diag, current))

    return out


def _collect_diagnostics(user_id: Optional[str] = None) -> dict:
    """Pull the diagnostic slices needed by the new proposers. Isolated
    behind a helper so tests can pass a synthetic dict directly.

    `user_id` scopes sizer-level slices (cost_validation, archetype_pnl) to
    this tenant's positions. Forecaster slices (brier_by_archetype) read from
    the shared predictions table and remain global.
    """
    try:
        from engine import diagnostics as D
        return {
            "brier_by_archetype":  D.brier_by_archetype("all"),
            "cost_validation":     D.cost_validation(user_id=user_id),
            "archetype_pnl":       D.archetype_pnl_attribution(user_id=user_id),
        }
    except Exception as exc:
        print(f"[learning_cadence] diag collection failed: {exc}",
              file=sys.stderr)
        return {}


# ── Diagnostic proposers ─────────────────────────────────────────────────────
def _propose_archetype_threshold(diag: dict,
                                 current: UserConfig) -> list[Proposal]:
    """
    Flag archetypes whose Brier score exceeds the uninformed baseline with
    a reliable sample. Proposal: add the archetype to archetype_skip_list so
    the sizer stops trading markets the forecaster demonstrably mis-calibrates.
    """
    out: list[Proposal] = []
    rows = diag.get("brier_by_archetype") or []
    for r in rows:
        n = int(r.get("n", 0) or 0)
        brier = r.get("brier")
        archetype = r.get("archetype")
        if n < STRICT_BUCKET_N or brier is None or not archetype:
            continue
        if brier <= ARCHETYPE_BRIER_THRESHOLD:
            continue
        out.append(Proposal(
            param_name="archetype_skip_list",
            current_value=None,
            proposed_value=None,
            evidence=(
                f"Archetype '{archetype}' Brier {brier:.3f} over {n} resolved "
                f"predictions is worse than the 0.25 uninformed baseline. "
                f"Proposal: add '{archetype}' to the archetype skip list so "
                f"the forecaster stops betting markets it demonstrably "
                f"mis-calibrates."
            ),
            proposal_metadata={
                "operation":    "list_append",
                "target_field": "archetype_skip_list",
                "items":        [archetype],
            },
        ))
    return out


def _propose_cost_correction(diag: dict,
                             current: UserConfig) -> list[Proposal]:
    """
    If the realised implied cost exceeds the sizer's assumed cost by more
    than COST_DELTA_THRESHOLD over a meaningful sample, propose raising the
    assumed cost so the expected-return gate reflects reality.
    """
    out: list[Proposal] = []
    cv = diag.get("cost_validation") or {}
    n = int(cv.get("n", 0) or 0)
    implied = cv.get("implied_cost")
    assumed = cv.get("assumed_cost")
    if n < COST_CORRECTION_MIN_N or implied is None or assumed is None:
        return out
    delta = float(implied) - float(assumed)
    if delta < COST_DELTA_THRESHOLD:
        return out
    proposed = round(float(implied), 4)
    out.append(Proposal(
        param_name="cost_assumption_override",
        current_value=float(assumed),
        proposed_value=proposed,
        evidence=(
            f"Realised implied cost {implied*100:.2f}% exceeds assumed cost "
            f"{assumed*100:.2f}% by {delta*100:.2f}pp across n={n} settled "
            f"positions. Gate 3 (min expected return) currently over-estimates "
            f"by that amount. Proposal: set cost_assumption_override={proposed}."
        ),
        proposal_metadata={
            "operation": "scalar_set",
            "field":     "cost_assumption_override",
            "value":     proposed,
        },
    ))
    return out


def _propose_archetype_stake_multiplier(diag: dict,
                                        current: UserConfig) -> list[Proposal]:
    """
    Read ROI-by-archetype. For each archetype with >=25 settled trades, if
    the ROI falls into one of the discrete tiers, propose setting that
    archetype's stake multiplier accordingly. Skip archetypes already on
    the skip list (no point multiplying a zero), and skip when the proposed
    multiplier is within hysteresis of the current one.
    """
    out: list[Proposal] = []
    rows = diag.get("archetype_pnl") or []
    current_map = dict(getattr(current, "archetype_stake_multipliers", {}) or {})
    skip_set = set(getattr(current, "archetype_skip_list", ()) or ())
    lo, hi = ARCHETYPE_MULTIPLIER_BOUNDS

    for r in rows:
        archetype = r.get("archetype")
        n = int(r.get("n", 0) or 0)
        roi = r.get("roi")
        if not archetype or archetype in skip_set:
            continue
        if n < ARCHETYPE_MULTIPLIER_MIN_N or roi is None:
            continue
        tier = _pick_multiplier_tier(float(roi))
        if tier is None:
            continue
        proposed = max(lo, min(hi, float(tier)))
        currently = float(current_map.get(archetype, 1.0))
        if abs(proposed - currently) < ARCHETYPE_MULTIPLIER_HYSTERESIS:
            continue
        verb = "upsizing" if proposed > 1.0 else "downsizing"
        out.append(Proposal(
            param_name="archetype_stake_multipliers",
            current_value=currently,
            proposed_value=proposed,
            evidence=(
                f"Archetype '{archetype}' ROI {roi*100:.1f}% over {n} settled "
                f"trades. Proposal: {verb} stake by setting the multiplier to "
                f"{proposed:.2f}x (currently {currently:.2f}x). The multiplier "
                f"is applied after the confidence softener."
            ),
            proposal_metadata={
                "operation":    "dict_set",
                "target_field": "archetype_stake_multipliers",
                "key":          archetype,
                "value":        proposed,
            },
        ))
    return out


def _pick_multiplier_tier(roi: float) -> Optional[float]:
    for lo, hi, mult in ARCHETYPE_MULTIPLIER_TIERS:
        if lo <= roi < hi:
            return mult
    return None


# ── Review-report composition + delivery ─────────────────────────────────────
def _compose_and_deliver_report(user_id: str, mode: str,
                                cycle_size: int) -> Optional[int]:
    """
    Compose the 50-trade review report, persist it to `learning_reports`,
    and fire the user-facing version through Telegram. Every step is
    wrapped in its own try/except so a flaky downstream doesn't sink
    the learning cycle.

    Telegram send is gated on `save_report` returning a non-None id. The
    unique constraint on `learning_reports(user_id, mode, settled_count)`
    causes the INSERT to no-op (returning None) when a row for the same
    bookmark already exists. That makes the constraint a hard gate
    against duplicate Telegram sends, even if the bookmark logic ever
    regresses again.
    """
    try:
        from engine import review_report
        report = review_report.compose_report(
            user_id=user_id, mode=mode, cycle_size=cycle_size,
        )
    except Exception as exc:
        print(f"[learning_cadence] compose_report failed: {exc}", file=sys.stderr)
        return None

    report_id: Optional[int] = None
    try:
        report_id = review_report.save_report(
            user_id=user_id, mode=mode, report=report,
        )
    except Exception as exc:
        print(f"[learning_cadence] save_report failed: {exc}", file=sys.stderr)

    if report_id is None:
        # Either save failed or a duplicate row was rejected by the unique
        # constraint. Either way, do NOT fire Telegram - this is the path
        # that prevents the user from seeing two review reports in a row.
        print(
            "[learning_cadence] skipping telegram send: no fresh report row",
            file=sys.stderr,
        )
        return None

    try:
        _send_report_via_telegram(user_id=user_id, report=report)
    except Exception as exc:
        print(f"[learning_cadence] telegram send failed: {exc}",
              file=sys.stderr)

    return report_id


def _send_report_via_telegram(user_id: str, report: dict) -> None:
    """Wrap the plain-text body in <pre> so Telegram HTML mode preserves
    the column alignment of the deterministic tables.

    Telegram's hard limit per message is 4096 characters. Long reports
    (many archetypes, full calibration table, several proposals) blow
    past that and Telegram silently truncates the tail mid-character,
    which is what the user saw as "cut off mid-word". We split the body
    on line boundaries into chunks well under the limit and send each as
    its own `<pre>`-wrapped HTML message, with a "(part X/Y)" header on
    every chunk after the first.
    """
    try:
        from feeds.telegram_notifier import TelegramNotifier
    except Exception as exc:
        print(f"[learning_cadence] telegram import failed: {exc}",
              file=sys.stderr)
        return

    body = (report.get("user_text") or "").strip()
    if not body:
        return

    chunks = _chunk_for_telegram(body)
    notifier = TelegramNotifier()
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        # Minimal HTML-escape for the three characters that matter inside <pre>.
        safe = (
            chunk.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
        )
        if total == 1:
            header = "<b>Delfi 50-trade review</b>"
        else:
            header = f"<b>Delfi 50-trade review</b> (part {idx}/{total})"
        message = f"{header}\n<pre>{safe}</pre>"
        notifier.send_sync(user_id, message)


# Per-chunk plain-text budget. Telegram's hard limit is 4096 chars per
# message. We need headroom for the `<b>...</b>` header (up to ~50 chars
# including the optional "(part X/Y)" suffix), the `<pre>...</pre>`
# wrapper (11 chars), and worst-case HTML escaping (each `&`, `<`, `>`
# expands to 4-5 chars). Budget 3500 chars of source text per chunk so
# the post-escape post-wrap message fits comfortably inside 4096.
_TELEGRAM_CHUNK_BUDGET = 3500


def _chunk_for_telegram(body: str) -> list[str]:
    """Split `body` into sub-strings of at most `_TELEGRAM_CHUNK_BUDGET`
    characters, preferring line boundaries so multi-line tables stay
    intact across the split. Pure helper - no IO, fully unit-testable.
    """
    if len(body) <= _TELEGRAM_CHUNK_BUDGET:
        return [body]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in body.split("\n"):
        # +1 accounts for the newline we will rejoin with.
        line_len = len(line) + 1
        # An individual line longer than the budget is rare (only the
        # thesis paragraph could approach it, and it caps at 480 chars
        # via _THESIS_MAX_CHARS) but handle defensively by hard-splitting
        # on character boundaries.
        if line_len > _TELEGRAM_CHUNK_BUDGET:
            if current:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            for i in range(0, len(line), _TELEGRAM_CHUNK_BUDGET):
                chunks.append(line[i:i + _TELEGRAM_CHUNK_BUDGET])
            continue
        if current_len + line_len > _TELEGRAM_CHUNK_BUDGET and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


# ── DB helpers ───────────────────────────────────────────────────────────────
def _count_settled_trades(user_id: str, mode: str) -> int:
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            return int(conn.execute(text(
                "SELECT COUNT(*) FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND status IN ('settled', 'invalid')"
            ), {"uid": user_id, "m": mode}).scalar() or 0)
    except Exception as exc:
        print(f"[learning_cadence] count_settled failed: {exc}", file=sys.stderr)
        return 0


def _last_cycle_settled_count(user_id: str, mode: str) -> int:
    """High-water mark of settled_count from any past learning cycle.

    Read from BOTH `learning_reports` and `pending_suggestions` and take
    the MAX. This bookmark advances whenever a cycle leaves a trace,
    which fixes the duplicate-fire bug:

    - When a cycle ran but produced ZERO proposals, no row gets inserted
      into `pending_suggestions`. The old query returned the previous
      MAX, `delta = settled_now - last_cycle` stayed >= the threshold,
      and the next single settlement re-triggered the cycle - the user
      received two review reports back to back.

    - `learning_reports` gets a row every cycle regardless of proposal
      count (compose_and_deliver_report always writes one), so it is the
      authoritative bookmark going forward. We keep `pending_suggestions`
      in the MAX as a fallback for historical rows that pre-date the
      report-bookmark fix and for the rare case where the report INSERT
      itself fails but suggestion INSERTs succeed.

    Scoped by mode for `learning_reports` (the table tracks it).
    `pending_suggestions` does not, so it stays user-scoped only - that
    is acceptable because the bookmark only ever moves forward.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            from_reports = conn.execute(text(
                "SELECT MAX(settled_count) "
                "FROM learning_reports "
                "WHERE user_id = :uid AND mode = :m"
            ), {"uid": user_id, "m": mode}).scalar()
            from_pending = conn.execute(text(
                "SELECT MAX(settled_count_at_creation) "
                "FROM pending_suggestions "
                "WHERE user_id = :uid"
            ), {"uid": user_id}).scalar()
            return max(int(from_reports or 0), int(from_pending or 0))
    except Exception as exc:
        print(f"[learning_cadence] last_cycle_count failed: {exc}", file=sys.stderr)
        return 0


def _gather_stats(user_id: str, mode: str, limit: int) -> dict:
    """Pull last `limit` settled trades and summarise for the proposers."""
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT cost_usd, realized_pnl_usd, category "
                "FROM pm_positions "
                "WHERE user_id = :uid AND mode = :m "
                "  AND status IN ('settled', 'invalid') "
                "ORDER BY settled_at DESC "
                "LIMIT :lim"
            ), {"uid": user_id, "m": mode, "lim": limit}).fetchall()
    except Exception as exc:
        print(f"[learning_cadence] gather_stats failed: {exc}", file=sys.stderr)
        return {"recent_window": {"n": 0}}

    costs  = [float(r[0] or 0.0) for r in rows]
    pnls   = [float(r[1] or 0.0) for r in rows]
    cats   = [r[2] for r in rows]

    n = len(rows)
    total_cost = sum(costs) or 1.0
    total_pnl  = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    roi = total_pnl / total_cost
    win_rate = wins / n if n else 0.0

    # Peak drawdown over the window - running max vs current equity.
    equity = 0.0
    peak = 0.0
    peak_dd = 0.0
    # Iterate in chronological order.
    for p in reversed(pnls):
        equity += p
        if equity > peak:
            peak = equity
        dd = (peak - equity) / (peak + 1e-9) if peak > 0 else 0.0
        if dd > peak_dd:
            peak_dd = dd

    by_category: dict[str, dict] = {}
    for cat, pnl, cost in zip(cats, pnls, costs):
        key = cat or "other"
        b = by_category.setdefault(key, {"n": 0, "pnl": 0.0, "cost": 0.0, "wins": 0})
        b["n"]    += 1
        b["pnl"]  += pnl
        b["cost"] += cost
        b["wins"] += 1 if pnl > 0 else 0

    for cat, b in by_category.items():
        b["roi"] = b["pnl"] / b["cost"] if b["cost"] else 0.0
        b["win_rate"] = b["wins"] / b["n"] if b["n"] else 0.0

    return {
        "recent_window": {
            "n":                  n,
            "roi":                roi,
            "win_rate":           win_rate,
            "total_pnl":          total_pnl,
            "total_cost":         total_cost,
            "peak_drawdown_pct":  peak_dd,
            "by_category":        by_category,
        },
    }


def _attach_backtest_delta(prop: Proposal, current: UserConfig) -> None:
    """Populate backtest_delta on a proposal using the backtester.

    Scalar proposals (max_stake_pct, cost_assumption_override) are applied
    via dataclass replace. List proposals (archetype_skip_list) read
    proposal_metadata['items'] and append to the existing tuple."""
    try:
        modified = _build_modified_config(prop, current)
        if modified is None:
            return

        from backtester.forecast_backtester import load_evaluations, simulate_with_config
        evals = load_evaluations(since_days=90)
        if not evals:
            return

        baseline = simulate_with_config(evals, current)
        candidate = simulate_with_config(evals, modified)

        base_roi = baseline.get("roi") or 0.0
        cand_roi = candidate.get("roi") or 0.0
        prop.backtest_delta = float(cand_roi - base_roi)
        prop.backtest_trades = int(candidate.get("trades_resolved") or 0)
    except Exception as exc:
        print(f"[learning_cadence] backtest delta failed for "
              f"{prop.param_name}: {exc}", file=sys.stderr)


def _build_modified_config(prop: Proposal,
                           current: UserConfig) -> Optional[UserConfig]:
    """
    Derive the simulated UserConfig for this proposal. Returns None when the
    proposal lacks enough information to simulate.

    Reads `proposal_metadata['operation']` to pick the merge strategy. Falls
    back to "scalar_set" when metadata is missing, matching the apply path.
    """
    meta = prop.proposal_metadata or {}
    operation = meta.get("operation", "scalar_set")

    if operation == "list_append":
        target = meta.get("target_field") or prop.param_name
        items = meta.get("items") or []
        if not items:
            return None
        existing = tuple(getattr(current, target, ()) or ())
        merged = list(existing)
        for raw in items:
            s = str(raw)
            if s and s not in merged:
                merged.append(s)
        if tuple(merged) == existing:
            return None
        return dataclasses.replace(current, **{target: tuple(merged)})

    if operation == "dict_set":
        target = meta.get("target_field") or prop.param_name
        key = meta.get("key")
        value = meta.get("value")
        if value is None:
            value = prop.proposed_value
        if not key or value is None:
            return None
        existing = dict(getattr(current, target, {}) or {})
        existing[str(key)] = float(value)
        return dataclasses.replace(current, **{target: existing})

    # Scalar path: prefer proposed_value; fall back to metadata['value'].
    target = meta.get("field") or prop.param_name
    value = prop.proposed_value
    if value is None:
        value = meta.get("value")
    if value is None:
        return None
    return dataclasses.replace(current, **{target: value})


def _store_pending_suggestion(prop: Proposal, user_id: str,
                              settled_count: int) -> bool:
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        meta_json = json.dumps(prop.proposal_metadata) \
            if prop.proposal_metadata else None
        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO pending_suggestions "
                "(user_id, param_name, current_value, proposed_value, "
                " evidence, backtest_delta, backtest_trades, "
                " settled_count_at_creation, metadata) "
                "VALUES (:uid, :k, :cur, :prop, :ev, :bd, :bt, :sc, "
                "        CAST(:meta AS JSONB))"
            ), {
                "uid":  user_id,
                "k":    prop.param_name,
                "cur":  prop.current_value,
                "prop": prop.proposed_value,
                "ev":   prop.evidence[:4000],
                "bd":   prop.backtest_delta,
                "bt":   prop.backtest_trades,
                "sc":   settled_count,
                "meta": meta_json,
            })
        return True
    except Exception as exc:
        print(f"[learning_cadence] store_pending failed: {exc}", file=sys.stderr)
        return False


def _decode_metadata(raw) -> Optional[dict]:
    """Coerce a JSONB/JSON/TEXT column value into a dict or None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else None
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ── Suggestion lifecycle (called from the dashboard API) ─────────────────────
def list_pending_suggestions(user_id: str = DEFAULT_USER_ID,
                             include_snoozed: bool = True) -> list[dict]:
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        statuses = ("pending", "snoozed") if include_snoozed else ("pending",)
        placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
        params = {"uid": user_id}
        for i, s in enumerate(statuses):
            params[f"s{i}"] = s
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT id, created_at, param_name, current_value, "
                "       proposed_value, evidence, backtest_delta, "
                "       backtest_trades, status, settled_count_at_creation, "
                "       metadata "
                "FROM pending_suggestions "
                f"WHERE user_id = :uid AND status IN ({placeholders}) "
                "ORDER BY created_at DESC"
            ), params).fetchall()
    except Exception as exc:
        print(f"[learning_cadence] list_pending failed: {exc}", file=sys.stderr)
        return []

    out = []
    for r in rows:
        out.append({
            "id":             int(r[0]),
            "created_at":     r[1].isoformat() if r[1] else None,
            "param_name":     r[2],
            "current_value":  float(r[3]) if r[3] is not None else None,
            "proposed_value": float(r[4]) if r[4] is not None else None,
            "evidence":       r[5],
            "backtest_delta": float(r[6]) if r[6] is not None else None,
            "backtest_trades": int(r[7]) if r[7] is not None else None,
            "status":         r[8],
            "settled_count":  int(r[9]) if r[9] is not None else None,
            "metadata":       _decode_metadata(r[10]),
        })
    return out


def apply_suggestion(suggestion_id: int,
                     user_id: str = DEFAULT_USER_ID,
                     resolved_by: str = "user") -> dict:
    """
    Apply a pending suggestion to user_config and mark the row applied.

    Dispatches on `metadata['operation']`:
      - "scalar_set" (default): write `proposed_value` to `param_name`.
      - "list_append": union `metadata['items']` onto the existing list at
        `metadata['target_field']`, preserving order of existing items and
        not re-adding duplicates.

    Validation of bounds still happens inside `update_user_config`.
    Rows written before the metadata column existed (metadata=NULL) are
    treated as "scalar_set" for backward compatibility.
    """
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        row = conn.execute(text(
            "SELECT param_name, proposed_value, status, metadata "
            "FROM pending_suggestions WHERE id = :id AND user_id = :uid"
        ), {"id": suggestion_id, "uid": user_id}).fetchone()
        if row is None:
            return {"status": "not_found"}
        if row[2] not in ("pending", "snoozed"):
            return {"status": "already_resolved", "current_status": row[2]}

        param_name = str(row[0])
        proposed_value = row[1]
        metadata = _decode_metadata(row[3]) or {}

    operation = metadata.get("operation", "scalar_set")

    if operation == "scalar_set":
        result = _apply_scalar(user_id, param_name, proposed_value)
    elif operation == "list_append":
        result = _apply_list_append(user_id, metadata)
    elif operation == "dict_set":
        result = _apply_dict_set(user_id, metadata)
    else:
        raise ValueError(f"unknown proposal operation: {operation!r}")

    with get_engine().begin() as conn:
        conn.execute(text(
            "UPDATE pending_suggestions SET status = 'applied', "
            "resolved_at = NOW(), resolved_by = :rb "
            "WHERE id = :id AND user_id = :uid"
        ), {"id": suggestion_id, "uid": user_id, "rb": resolved_by})

    result["status"] = "applied"
    return result


def _apply_scalar(user_id: str, param_name: str, proposed_value) -> dict:
    if proposed_value is None:
        raise ValueError(
            f"scalar_set proposal for {param_name!r} has no proposed_value"
        )
    value = float(proposed_value)
    update_user_config(user_id, **{param_name: value})
    return {"param_name": param_name, "value": value, "operation": "scalar_set"}


def _apply_dict_set(user_id: str, metadata: dict) -> dict:
    """Merge a single {key: value} into an existing dict-valued user_config
    field (e.g. archetype_stake_multipliers). Preserves every other key."""
    target_field = metadata.get("target_field")
    key = metadata.get("key")
    value = metadata.get("value")
    if not target_field or not key or value is None:
        raise ValueError(
            "dict_set proposal requires 'target_field', 'key', and 'value'"
        )
    current_cfg = get_user_config(user_id)
    current_map = dict(getattr(current_cfg, target_field, {}) or {})
    current_map[str(key)] = float(value)
    update_user_config(user_id, **{target_field: current_map})
    return {
        "param_name":   target_field,
        "operation":    "dict_set",
        "key":          str(key),
        "value":        float(value),
    }


def _apply_list_append(user_id: str, metadata: dict) -> dict:
    target_field = metadata.get("target_field")
    items_raw = metadata.get("items")
    if not target_field or not isinstance(items_raw, (list, tuple)):
        raise ValueError(
            "list_append proposal requires 'target_field' and 'items'"
        )
    new_items = [str(x) for x in items_raw if x is not None and str(x) != ""]
    if not new_items:
        raise ValueError("list_append proposal has no items to add")

    current_cfg = get_user_config(user_id)
    current_list = getattr(current_cfg, target_field, None) or ()
    merged = list(current_list)
    added: list[str] = []
    for item in new_items:
        if item not in merged:
            merged.append(item)
            added.append(item)

    update_user_config(user_id, **{target_field: tuple(merged)})
    return {
        "param_name":   target_field,
        "operation":    "list_append",
        "added":        added,
        "skipped_dups": [x for x in new_items if x not in added],
        "value":        list(merged),
    }


def skip_suggestion(suggestion_id: int,
                    user_id: str = DEFAULT_USER_ID,
                    resolved_by: str = "user") -> dict:
    return _update_status(suggestion_id, user_id, "skipped", resolved_by)


def snooze_suggestion(suggestion_id: int,
                      user_id: str = DEFAULT_USER_ID,
                      resolved_by: str = "user") -> dict:
    return _update_status(suggestion_id, user_id, "snoozed", resolved_by)


# ── Telegram-facing "next pending" helpers ───────────────────────────────────
# The Telegram /apply and /reject commands don't carry a suggestion id, so
# they work on the oldest currently-pending row for the caller. These helpers
# keep the telegram_notifier handler thin and let us stub a single entry
# point in tests.

def _oldest_pending_row(user_id: str) -> Optional[dict]:
    rows = list_pending_suggestions(user_id, include_snoozed=False)
    pending = [r for r in rows if r.get("status") == "pending"]
    if not pending:
        return None
    pending.sort(key=lambda r: r.get("created_at") or "")
    return pending[0]


def apply_next_pending_suggestion(user_id: str = DEFAULT_USER_ID,
                                  resolved_by: str = "telegram") -> dict:
    """Apply the oldest currently-pending suggestion for `user_id`.

    Returns ``{"status": "none"}`` when the user has no pending rows.
    Otherwise delegates to `apply_suggestion` and annotates the result with
    `display_key`, `display_previous`, and `display_value` so a Telegram
    handler can render one format for every operation type without caring
    about the underlying dispatch.
    """
    row = _oldest_pending_row(user_id)
    if row is None:
        return {"status": "none"}

    suggestion_id = int(row["id"])
    previous = row.get("current_value")
    result = apply_suggestion(suggestion_id, user_id=user_id,
                              resolved_by=resolved_by)
    if result.get("status") != "applied":
        return result

    operation = result.get("operation", "scalar_set")
    if operation == "dict_set":
        key_name = result.get("key")
        target = result.get("param_name") or "config"
        result["display_key"] = (
            f"{target}['{key_name}']" if key_name else target
        )
        # For a brand-new archetype key the stored current_value is 1.0
        # (the neutral-multiplier default) rather than None.
        result["display_previous"] = previous if previous is not None else 1.0
        result["display_value"] = result.get("value")
    elif operation == "list_append":
        added = result.get("added") or []
        result["display_key"] = result.get("param_name") or "config"
        result["display_previous"] = "-"
        result["display_value"] = (
            ", ".join(f"+{x}" for x in added) if added else "-"
        )
    else:  # scalar_set or unknown: fall back to the param_name view.
        result["display_key"] = result.get("param_name") or "config"
        result["display_previous"] = previous
        result["display_value"] = result.get("value")
    return result


def skip_next_pending_suggestion(user_id: str = DEFAULT_USER_ID,
                                 resolved_by: str = "telegram") -> dict:
    """Mark the oldest currently-pending suggestion for `user_id` as skipped."""
    row = _oldest_pending_row(user_id)
    if row is None:
        return {"status": "none"}
    return skip_suggestion(int(row["id"]), user_id=user_id,
                           resolved_by=resolved_by)


def apply_all_pending_suggestions(user_id: str = DEFAULT_USER_ID,
                                  resolved_by: str = "telegram") -> dict:
    """Apply every currently-pending suggestion for `user_id` in one call.

    Walks the pending rows in `created_at` order and delegates each one
    through `apply_suggestion`, collecting per-row results. A failure on
    any single row is captured in `failed` but does not stop the loop -
    the caller (Telegram /apply handler) still applies as many rows as
    possible and reports the partial outcome to the user.

    Returns:
        {
            "status":  "applied" | "none" | "partial",
            "applied": [<per-row apply result with display fields>, ...],
            "failed":  [{"id": int, "error": str}, ...],
            "total":   int,    # rows attempted
        }

    `status == "none"` means the user had nothing pending. `"applied"`
    means every row succeeded. `"partial"` means at least one row failed
    while at least one succeeded - the Telegram handler renders the full
    list either way.
    """
    rows = list_pending_suggestions(user_id, include_snoozed=False) or []
    pending = [r for r in rows if r.get("status") == "pending"]
    if not pending:
        return {"status": "none", "applied": [], "failed": [], "total": 0}

    pending.sort(key=lambda r: r.get("created_at") or "")

    applied: list[dict] = []
    failed:  list[dict] = []
    for row in pending:
        suggestion_id = int(row["id"])
        previous = row.get("current_value")
        try:
            result = apply_suggestion(
                suggestion_id, user_id=user_id, resolved_by=resolved_by,
            )
        except Exception as exc:
            failed.append({"id": suggestion_id, "error": str(exc)})
            continue

        if result.get("status") != "applied":
            failed.append({
                "id":     suggestion_id,
                "error":  result.get("status") or "unknown",
            })
            continue

        # Mirror the display annotations that `apply_next_pending_suggestion`
        # sets so the Telegram handler can render dict / list / scalar
        # operations through one code path.
        operation = result.get("operation", "scalar_set")
        if operation == "dict_set":
            key_name = result.get("key")
            target = result.get("param_name") or "config"
            result["display_key"] = (
                f"{target}['{key_name}']" if key_name else target
            )
            result["display_previous"] = (
                previous if previous is not None else 1.0
            )
            result["display_value"] = result.get("value")
        elif operation == "list_append":
            added = result.get("added") or []
            result["display_key"] = result.get("param_name") or "config"
            result["display_previous"] = "-"
            result["display_value"] = (
                ", ".join(f"+{x}" for x in added) if added else "-"
            )
        else:
            result["display_key"] = result.get("param_name") or "config"
            result["display_previous"] = previous
            result["display_value"] = result.get("value")
        applied.append(result)

    if applied and not failed:
        status = "applied"
    elif applied and failed:
        status = "partial"
    else:
        # Every row failed - keep "none" semantics out of this branch by
        # reporting "partial" with empty applied so the handler shows the
        # error list.
        status = "partial"
    return {
        "status":  status,
        "applied": applied,
        "failed":  failed,
        "total":   len(pending),
    }


def _update_status(suggestion_id: int, user_id: str,
                   status: str, resolved_by: str) -> dict:
    from sqlalchemy import text
    from db.engine import get_engine
    try:
        with get_engine().begin() as conn:
            result = conn.execute(text(
                "UPDATE pending_suggestions SET status = :st, "
                "resolved_at = NOW(), resolved_by = :rb "
                "WHERE id = :id AND user_id = :uid "
                "  AND status IN ('pending', 'snoozed')"
            ), {"id": suggestion_id, "uid": user_id, "st": status, "rb": resolved_by})
            if result.rowcount == 0:
                return {"status": "not_found_or_resolved"}
        return {"status": status, "id": suggestion_id}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
