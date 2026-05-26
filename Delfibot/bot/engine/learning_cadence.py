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

from db.engine import iso_utc
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

    Single-user local SQLite app: APScheduler jobs and the aiohttp API
    share one process, and only one settlement-driven cycle is ever in
    flight at a time. The Postgres advisory-lock dance from the SaaS
    codebase is unnecessary here, so we just open a short transaction
    around the gate check and the proposal store.
    """
    try:
        from db.engine import get_engine

        with get_engine().begin() as _lock_conn:
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
                settled_count_bookmark=settled_now,
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

    # V1.5 expansion: comprehensive risk-knob proposers. Each reads
    # one diagnostic slice from engine.learning_diagnostics and only
    # emits a proposal when its CI gate passes. User instruction
    # (2026-05-26): reviews and suggestions should be very
    # comprehensive and cover early exits / take profit / etc., not
    # just archetype performance.
    out.extend(_propose_exit_policy_toggle(diag, current))
    out.extend(_propose_exit_threshold(diag, current))
    out.extend(_propose_horizon_window(diag, current))
    out.extend(_propose_base_stake(diag, current))
    out.extend(_propose_daily_loss_limit(diag, current))
    out.extend(_propose_weekly_loss_limit(diag, current))
    out.extend(_propose_drawdown_halt(diag, current))
    out.extend(_propose_streak_cooldown(diag, current))
    out.extend(_propose_archetype_price_band(diag, current))

    return out


def _collect_diagnostics(user_id: Optional[str] = None) -> dict:
    """Pull the diagnostic slices needed by the new proposers. Isolated
    behind a helper so tests can pass a synthetic dict directly.

    `user_id` scopes sizer-level slices (cost_validation, archetype_pnl) to
    this tenant's positions. Forecaster slices (brier_by_archetype) read from
    the shared predictions table and remain global.

    Also returns the raw `settled_rows` so proposers can run bootstrap
    CI checks per cell. Without raw rows the proposers can only see
    aggregates, which hides the variance that drives whether a finding
    is publishable.
    """
    try:
        from engine import diagnostics as D
        from engine import learning_diagnostics as LD
        # Resolve mode for the mode-scoped slices. Fall back to "live"
        # when no user_id is passed (admin / test path); fall back to
        # the user's configured mode otherwise. Sim and live cannot
        # mix (CLAUDE.md hard rule).
        try:
            cfg = get_user_config(user_id) if user_id else None
            mode = (cfg.mode if cfg else None) or "live"
        except Exception:
            mode = "live"
        uid = user_id or DEFAULT_USER_ID
        return {
            "brier_by_archetype":  D.brier_by_archetype("all"),
            "cost_validation":     D.cost_validation(user_id=user_id),
            "archetype_pnl":       D.archetype_pnl_attribution(user_id=user_id),
            "settled_rows":        _load_settled_rows(user_id=user_id),
            # V1.5 risk-knob slices.
            "exit_policy":         LD.exit_policy_attribution(uid, mode),
            "exit_threshold_sweep": LD.exit_threshold_backtest(uid, mode),
            "horizon_pnl":         LD.horizon_pnl_attribution(uid, mode),
            "loss_day_recovery":   LD.loss_day_recovery(uid, mode),
            "loss_week_recovery":  LD.loss_week_recovery(uid, mode),
            "loss_streak":         LD.loss_streak_analysis(uid, mode),
            "archetype_price_band": LD.archetype_price_band_pnl(uid, mode),
            "aggregate_roi":       LD.aggregate_roi_and_drawdown(uid, mode),
        }
    except Exception as exc:
        print(f"[learning_cadence] diag collection failed: {exc}",
              file=sys.stderr)
        return {}


def _load_settled_rows(user_id: Optional[str] = None) -> list[dict]:
    """Settled-and-resolved pm_positions for this user, plain dicts.

    Used by proposers that need to run bootstrap CI checks. Returns the
    fields the bootstrap and CI helpers consume (cost, pnl, archetype,
    side, entry_price, delfi_probability) plus enough context to slice
    the data into cells in any way a future proposer might need.
    """
    from db.engine import get_engine
    from sqlalchemy import text as _sa_text
    eng = get_engine()
    uid = user_id or DEFAULT_USER_ID
    try:
        with eng.connect() as conn:
            rs = conn.execute(_sa_text(
                "SELECT id, market_archetype, side, entry_price, "
                "       delfi_probability, cost_usd, realized_pnl_usd, "
                "       mode, status, settled_at "
                "FROM pm_positions "
                "WHERE user_id = :uid AND status='settled' "
                "  AND cost_usd IS NOT NULL "
                "  AND realized_pnl_usd IS NOT NULL"
            ), {"uid": uid})
            return [dict(r._mapping) for r in rs]
    except Exception as exc:
        print(f"[learning_cadence] settled-rows load failed: {exc}",
              file=sys.stderr)
        return []


# ── Diagnostic proposers ─────────────────────────────────────────────────────
def _propose_archetype_threshold(diag: dict,
                                 current: UserConfig) -> list[Proposal]:
    """
    Flag archetypes whose Brier score exceeds the uninformed baseline with
    a reliable sample. Proposal: add the archetype to archetype_skip_list
    so the sizer stops trading markets the forecaster demonstrably
    mis-calibrates.

    Skips archetypes already on the user's skip list -- proposing to add
    something that's already there is a no-op. The earlier version
    didn't filter, so users with an existing skip list got "Add tennis to
    skip list" suggestions that did nothing when Applied.
    """
    from engine.stats import (
        MIN_N_ARCHETYPE_LEVEL,
        cell_passes_ci_gate,
    )

    out: list[Proposal] = []
    rows = diag.get("brier_by_archetype") or []
    settled = diag.get("settled_rows") or []
    skip_set = {
        str(x) for x in (getattr(current, "archetype_skip_list", ()) or ())
    }
    for r in rows:
        n = int(r.get("n", 0) or 0)
        brier = r.get("brier")
        archetype = r.get("archetype")
        if n < MIN_N_ARCHETYPE_LEVEL or brier is None or not archetype:
            continue
        if brier <= ARCHETYPE_BRIER_THRESHOLD:
            continue
        if archetype in skip_set:
            continue

        # Brier is a forecaster-quality metric, not a P&L metric -
        # but the bot only loses money on a high-Brier archetype if
        # the calibration error translates to consistently losing
        # trades. Add a CI gate on the archetype's PnL so we don't
        # propose skipping based on Brier alone when the trades on
        # that archetype are statistically break-even.
        cell_rows = [s for s in settled
                     if s.get("market_archetype") == archetype]
        if cell_rows and settled:
            passes, cell_ci, g_roi = cell_passes_ci_gate(cell_rows, settled)
            if not passes or not cell_ci.is_losing():
                # Brier is bad but PnL CI either overlaps the global
                # mean or straddles zero. Mis-calibration without
                # statistically distinguishable losses isn't an
                # actionable skip - it's a forecaster-improvement
                # opportunity.
                continue
            evidence_ci = (
                f" PnL CI [{cell_ci.lo_pct:+.1f}%, {cell_ci.hi_pct:+.1f}%] "
                f"vs global {g_roi:+.1f}% confirms statistically "
                f"distinguishable losses, not just calibration error."
            )
        else:
            evidence_ci = ""

        out.append(Proposal(
            param_name="archetype_skip_list",
            current_value=None,
            proposed_value=None,
            evidence=(
                f"Archetype '{archetype}' Brier {brier:.3f} over {n} resolved "
                f"predictions is worse than the 0.25 uninformed baseline."
                + evidence_ci +
                f" Proposal: add '{archetype}' to the archetype skip list so "
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
    assumed cost so per-trade P&L diagnostics reflect reality. The V1 sizer
    has no expected-return gate; this knob feeds reporting only.
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
            f"positions. Diagnostics overstate expected ROI by that amount. "
            f"Proposal: set cost_assumption_override={proposed}."
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
    Read ROI-by-archetype. For each archetype, if the bootstrap 95% CI
    on its ROI lies entirely on one side of the GLOBAL ROI, AND the
    sample size meets MIN_N_ARCHETYPE_LEVEL, propose setting the
    multiplier per the discrete tier table.

    Why CI-gating instead of point-estimate gating: a 25-trade cell
    with a +5% ROI but a CI of [-30%, +40%] is statistical noise
    around the global mean; acting on it would amplify variance, not
    edge. Pre-2026-05-06 the proposer fired on point estimates and
    proposed multipliers based on 8-trade buckets where every cell's
    CI overlapped break-even. The bootstrap gate kills 80% of those
    spurious proposals. The remaining ~20% are findings the data
    actually supports.

    Always emits sample/ci/power metadata so the dashboard can show
    "n=36 / required for +5% lift = 1571" alongside the proposal,
    even when the proposer ITSELF declined to fire (we still surface
    the cell so users see what's being measured).
    """
    # Fallback for "what is the user's CURRENT multiplier on this
    # archetype" must use the same V1 doctrine defaults the sizer
    # falls back to. Otherwise on legacy installs (empty
    # archetype_stake_multipliers dict) the proposer says
    # "currently 1.0x" while the sizer trades at 1.5x basketball /
    # 0.5x tennis — and Apply silently corrupts the V1 default.
    from engine.user_config import V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS
    from engine.stats import (
        MIN_N_ARCHETYPE_LEVEL,
        bootstrap_roi_ci,
        cell_passes_ci_gate,
        min_n_for_detection,
    )

    out: list[Proposal] = []
    rows = diag.get("archetype_pnl") or []
    settled = diag.get("settled_rows") or []
    current_map = dict(getattr(current, "archetype_stake_multipliers", {}) or {})
    skip_set = set(getattr(current, "archetype_skip_list", ()) or ())
    lo, hi = ARCHETYPE_MULTIPLIER_BOUNDS

    # Pre-compute global stats so each per-archetype loop is cheap.
    if not settled:
        return out
    from engine.stats import _roi_pct as _roi  # noqa: PLC2701
    global_roi_pct = _roi(settled)
    n_required_5pct = min_n_for_detection(
        baseline_roi=global_roi_pct / 100.0, target_lift=0.05,
    )

    for r in rows:
        archetype = r.get("archetype")
        n = int(r.get("n", 0) or 0)
        roi = r.get("roi")
        if not archetype or archetype in skip_set:
            continue
        if roi is None:
            continue

        # CI gate: pull the per-archetype rows out of the settled set
        # and compute a bootstrap CI. Block the proposal if the CI
        # overlaps the global mean.
        cell_rows = [s for s in settled
                     if s.get("market_archetype") == archetype]
        passes, cell_ci, _ = cell_passes_ci_gate(cell_rows, settled)
        if n < MIN_N_ARCHETYPE_LEVEL:
            continue  # would surface at sufficient sample
        if not passes:
            # Statistically indistinguishable from the global mean;
            # acting on this cell amplifies variance.
            continue

        tier = _pick_multiplier_tier(float(roi))
        if tier is None:
            continue
        proposed = max(lo, min(hi, float(tier)))
        default_for_arch = V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS.get(archetype, 1.0)
        currently = float(current_map.get(archetype, default_for_arch))
        if abs(proposed - currently) < ARCHETYPE_MULTIPLIER_HYSTERESIS:
            continue
        verb = "upsizing" if proposed > currently else "downsizing"
        out.append(Proposal(
            param_name="archetype_stake_multipliers",
            current_value=currently,
            proposed_value=proposed,
            evidence=(
                f"Archetype '{archetype}' ROI {roi*100:+.1f}% over {n} settled "
                f"trades, 95% CI [{cell_ci.lo_pct:+.1f}%, {cell_ci.hi_pct:+.1f}%] "
                f"vs global {global_roi_pct:+.1f}%. CI excludes the global mean, "
                f"so this is real signal not sample variance. Proposal: "
                f"{verb} stake to {proposed:.2f}x (currently {currently:.2f}x). "
                f"Detecting a +5% per-trade lift at p<0.05 would need "
                f"n={n_required_5pct} per arm; we have {n} - directional "
                f"confidence is good but magnitude is approximate."
            ),
            proposal_metadata={
                "operation":     "dict_set",
                "target_field":  "archetype_stake_multipliers",
                "key":           archetype,
                "value":         proposed,
                "stats": {
                    "n":              n,
                    "roi_pct":        round(float(roi) * 100, 2),
                    "ci_lo_pct":      round(cell_ci.lo_pct, 2),
                    "ci_hi_pct":      round(cell_ci.hi_pct, 2),
                    "global_roi_pct": round(global_roi_pct, 2),
                    "min_n_required": n_required_5pct,
                },
            },
        ))
    return out


def _pick_multiplier_tier(roi: float) -> Optional[float]:
    for lo, hi, mult in ARCHETYPE_MULTIPLIER_TIERS:
        if lo <= roi < hi:
            return mult
    return None


# ══════════════════════════════════════════════════════════════════════════════
# V1.5 RISK-KNOB PROPOSERS
# ══════════════════════════════════════════════════════════════════════════════
# Each function reads ONE slice from engine.learning_diagnostics and emits 0+
# proposals. Every proposer uses a CI gate: no proposal fires unless the
# bootstrap interval excludes the null (zero saved-vs-hold for exit policy,
# the global ROI for horizon bucket, etc.). The CI gate is what stops the
# system from emitting noisy suggestions on tiny samples - the exact bug
# the V0->V1 pivot fixed.
# ══════════════════════════════════════════════════════════════════════════════

# Bucket gate for the per-reason exit-policy proposer. Same shape as
# the archetype-multiplier proposer's MIN_N: a number large enough
# that a CI exclusion is unlikely to be sample-size noise. 8 is
# tight; we relax archetype-level gates to 25 because they compound
# more on the bot's behaviour.
EXIT_POLICY_MIN_N = 8


def _propose_exit_policy_toggle(diag: dict,
                                current: UserConfig) -> list[Proposal]:
    """Disable take_profit / stop_loss / time_decay when the
    counterfactual analysis says the policy is statistically harmful.

    `policy_is_harmful` from exit_policy_attribution() means: at least
    EXIT_POLICY_MIN_N backfilled rows AND the bootstrap CI on "saved
    vs hold" lies entirely below zero. That's the strong signal -
    the policy is reliably costing money vs just holding.
    """
    out: list[Proposal] = []
    ep = diag.get("exit_policy") or {}
    rows = ep.get("by_reason") or []
    field_map = {
        "take_profit": "take_profit_enabled",
        "stop_loss":   "stop_loss_enabled",
        "time_decay":  "time_decay_enabled",
    }
    for r in rows:
        reason = r.get("reason")
        if reason not in field_map:
            continue
        if not r.get("policy_is_harmful"):
            continue
        field = field_map[reason]
        # Skip if already disabled.
        if not bool(getattr(current, field, True)):
            continue
        n = int(r.get("n") or 0)
        mean_saved = float(r.get("mean_saved_vs_hold") or 0.0)
        ci_lo = float(r.get("ci_lo") or 0.0)
        ci_hi = float(r.get("ci_hi") or 0.0)
        out.append(Proposal(
            param_name=field,
            current_value=1.0,  # truthy placeholder for the diff renderer
            proposed_value=0.0,
            evidence=(
                f"Over {n} early-exits backfilled with counterfactual P&L, "
                f"the '{reason}' policy cost an average of "
                f"${mean_saved:+.2f} per position vs just holding to "
                f"settlement. 95% CI [${ci_lo:+.2f}, ${ci_hi:+.2f}] lies "
                f"entirely below zero, so the loss is statistically "
                f"distinguishable from noise. Proposal: disable "
                f"'{field}' so the bot holds these positions instead."
            ),
            proposal_metadata={
                "operation": "scalar_set",
                "field":     field,
                "value":     False,
                "stats": {
                    "n":              n,
                    "mean_saved_usd": round(mean_saved, 4),
                    "ci_lo_usd":      round(ci_lo, 4),
                    "ci_hi_usd":      round(ci_hi, 4),
                },
            },
        ))
    return out


def _propose_exit_threshold(diag: dict,
                            current: UserConfig) -> list[Proposal]:
    """For each currently-enabled exit policy whose threshold sweep
    has identified a BETTER threshold (CI on per-position pnl
    strictly above baseline), propose adjusting the threshold.

    Only acts on policies that are still enabled - if the user has
    disabled take-profit, suggesting a take-profit threshold is
    moot. Pairs with _propose_exit_policy_toggle: disable comes
    first; threshold tweaks come once the policy stays on.
    """
    out: list[Proposal] = []
    sweep = diag.get("exit_threshold_sweep") or {}
    n_pos = int(sweep.get("n_positions") or 0)
    if n_pos < 12:
        return out  # not enough data for a meaningful sweep
    baseline = float(sweep.get("baseline_total_pnl") or 0.0)

    def _best_threshold(rows: list[dict]) -> Optional[dict]:
        # Pick the row with the highest mean_pnl_per_position whose
        # CI lower bound exceeds the per-position baseline mean.
        baseline_mean = (baseline / n_pos) if n_pos else 0.0
        candidates = [
            r for r in rows
            if float(r.get("ci_lo") or 0.0) > baseline_mean
            and int(r.get("n_would_trigger") or 0) >= 3
        ]
        if not candidates:
            return None
        return max(candidates,
                   key=lambda r: float(r.get("mean_pnl_per_position") or 0.0))

    # Take-profit threshold.
    if getattr(current, "take_profit_enabled", True):
        tp_rows = sweep.get("take_profit") or []
        best = _best_threshold(tp_rows)
        currently = float(getattr(current, "take_profit_threshold_pct", 0.50)
                          or 0.50)
        if best is not None:
            proposed = float(best["threshold_pct"])
            if abs(proposed - currently) >= 0.05:
                lo, hi = USER_CONFIG_BOUNDS["take_profit_threshold_pct"]
                proposed = max(lo, min(hi, proposed))
                if abs(proposed - currently) >= 0.05:
                    n_trig = int(best["n_would_trigger"])
                    mpp = float(best["mean_pnl_per_position"])
                    out.append(Proposal(
                        param_name="take_profit_threshold_pct",
                        current_value=currently,
                        proposed_value=proposed,
                        evidence=(
                            f"Backtest across {n_pos} settled positions: a "
                            f"take-profit threshold of {proposed*100:.0f}% "
                            f"would have triggered on {n_trig} rows, with a "
                            f"mean per-position P&L of ${mpp:+.2f} vs the "
                            f"baseline ${baseline / n_pos:+.2f}. CI lower "
                            f"bound ${float(best['ci_lo']):+.2f} stays "
                            f"above baseline."
                        ),
                        proposal_metadata={
                            "operation": "scalar_set",
                            "field":     "take_profit_threshold_pct",
                            "value":     proposed,
                            "stats": {
                                "n_positions":     n_pos,
                                "n_would_trigger": n_trig,
                                "mean_pnl_pp":     round(mpp, 4),
                                "ci_lo":           float(best["ci_lo"]),
                                "ci_hi":           float(best["ci_hi"]),
                            },
                        },
                    ))

    # Stop-loss threshold.
    if getattr(current, "stop_loss_enabled", True):
        sl_rows = sweep.get("stop_loss") or []
        best = _best_threshold(sl_rows)
        currently = float(getattr(current, "stop_loss_threshold_pct", 0.30)
                          or 0.30)
        if best is not None:
            proposed = float(best["threshold_pct"])
            if abs(proposed - currently) >= 0.05:
                lo, hi = USER_CONFIG_BOUNDS["stop_loss_threshold_pct"]
                proposed = max(lo, min(hi, proposed))
                if abs(proposed - currently) >= 0.05:
                    n_trig = int(best["n_would_trigger"])
                    mpp = float(best["mean_pnl_per_position"])
                    out.append(Proposal(
                        param_name="stop_loss_threshold_pct",
                        current_value=currently,
                        proposed_value=proposed,
                        evidence=(
                            f"Backtest across {n_pos} settled positions: a "
                            f"stop-loss threshold of {proposed*100:.0f}% "
                            f"would have triggered on {n_trig} rows, with a "
                            f"mean per-position P&L of ${mpp:+.2f} vs the "
                            f"baseline ${baseline / n_pos:+.2f}. CI lower "
                            f"bound ${float(best['ci_lo']):+.2f} stays "
                            f"above baseline."
                        ),
                        proposal_metadata={
                            "operation": "scalar_set",
                            "field":     "stop_loss_threshold_pct",
                            "value":     proposed,
                            "stats": {
                                "n_positions":     n_pos,
                                "n_would_trigger": n_trig,
                                "mean_pnl_pp":     round(mpp, 4),
                                "ci_lo":           float(best["ci_lo"]),
                                "ci_hi":           float(best["ci_hi"]),
                            },
                        },
                    ))
    return out


def _propose_horizon_window(diag: dict,
                            current: UserConfig) -> list[Proposal]:
    """If a long-horizon bucket has a statistically negative ROI,
    propose tightening max_days_to_resolution so the bot stops
    trading those.
    """
    out: list[Proposal] = []
    rows = diag.get("horizon_pnl") or []
    # Walk buckets in order; the first bucket whose CI upper bound is
    # negative AND has usable n is the cutoff. Bucket-label -> max
    # days for that bucket.
    bucket_to_max_days = {
        "< 1d":  1,
        "1-3d":  3,
        "3-7d":  7,
        "7-14d": 14,
        "14d+":  30,
    }
    losing_cutoff: Optional[int] = None
    for r in rows:
        if not r.get("usable"):
            continue
        ci_hi = float(r.get("ci_hi") or 0.0)
        if ci_hi >= 0:
            continue
        label = r.get("bucket")
        if label in bucket_to_max_days:
            # Cutoff = lower edge of this bucket (everything from
            # this bucket onwards is bad).
            bucket_low = {
                "< 1d": 0, "1-3d": 1, "3-7d": 3,
                "7-14d": 7, "14d+": 14,
            }[label]
            losing_cutoff = bucket_low
            break
    if losing_cutoff is None or losing_cutoff <= 0:
        return out

    current_max = int(getattr(current, "max_days_to_resolution", 0) or 0)
    if current_max == losing_cutoff:
        return out
    if current_max != 0 and current_max <= losing_cutoff:
        return out  # already at least this tight
    out.append(Proposal(
        param_name="max_days_to_resolution",
        current_value=float(current_max) if current_max else None,
        proposed_value=float(losing_cutoff),
        evidence=(
            f"Horizon analysis flags the {losing_cutoff}-day+ bucket as "
            f"statistically losing (CI excludes zero on the upside). "
            f"Proposal: tighten max_days_to_resolution to {losing_cutoff} "
            f"days so the bot stops entering markets whose settlement is "
            f"further out than the calibration data supports."
        ),
        proposal_metadata={
            "operation": "scalar_set",
            "field":     "max_days_to_resolution",
            "value":     losing_cutoff,
        },
    ))
    return out


def _propose_base_stake(diag: dict,
                       current: UserConfig) -> list[Proposal]:
    """Resize base_stake_pct based on aggregate ROI + drawdown
    headroom. Only fires after >= 25 settled trades.
    """
    out: list[Proposal] = []
    agg = diag.get("aggregate_roi") or {}
    n = int(agg.get("n_settled") or 0)
    if n < 25:
        return out
    roi = agg.get("roi")
    if roi is None:
        return out
    roi = float(roi)
    peak_dd = float(agg.get("peak_drawdown") or 0.0)
    drawdown_halt = float(getattr(current, "drawdown_halt_pct", 0.40) or 0.40)
    currently = float(getattr(current, "base_stake_pct", 0.02) or 0.02)
    lo, hi = USER_CONFIG_BOUNDS["base_stake_pct"]

    proposed: Optional[float] = None
    rationale = ""
    if roi >= 0.10 and peak_dd <= drawdown_halt * 0.50:
        # Strong positive ROI + plenty of drawdown headroom -> size up.
        proposed = min(hi, round(currently * 1.5, 4))
        rationale = (
            f"Aggregate ROI {roi*100:+.1f}% over {n} settled trades and "
            f"peak drawdown {peak_dd*100:.1f}% is well below the halt at "
            f"{drawdown_halt*100:.0f}%. Size up base stake to compound "
            f"faster while keeping risk reserves intact."
        )
    elif roi <= -0.05 and peak_dd >= drawdown_halt * 0.60:
        # Negative ROI + heating drawdown -> size down.
        proposed = max(lo, round(currently * 0.6, 4))
        rationale = (
            f"Aggregate ROI {roi*100:+.1f}% over {n} settled trades and "
            f"peak drawdown {peak_dd*100:.1f}% is approaching the halt at "
            f"{drawdown_halt*100:.0f}%. Shrink base stake to slow the "
            f"bleed."
        )
    if proposed is None or abs(proposed - currently) < 0.002:
        return out
    out.append(Proposal(
        param_name="base_stake_pct",
        current_value=currently,
        proposed_value=proposed,
        evidence=(
            f"{rationale} Current {currently*100:.2f}% -> "
            f"{proposed*100:.2f}%."
        ),
        proposal_metadata={
            "operation": "scalar_set",
            "field":     "base_stake_pct",
            "value":     proposed,
            "stats": {
                "n":              n,
                "roi":            round(roi, 4),
                "peak_drawdown":  round(peak_dd, 4),
            },
        },
    ))
    return out


def _propose_daily_loss_limit(diag: dict,
                              current: UserConfig) -> list[Proposal]:
    """Tighten daily_loss_limit_pct when historical recovery rate
    on loss days is poor, loosen when consistently good.
    """
    out: list[Proposal] = []
    lr = diag.get("loss_day_recovery") or {}
    n_loss = int(lr.get("n_loss_days") or 0)
    if n_loss < 8:
        return out
    rec_rate = lr.get("recovery_rate")
    if rec_rate is None:
        return out
    rec_rate = float(rec_rate)
    currently = float(getattr(current, "daily_loss_limit_pct", 0.10) or 0.10)
    lo, hi = USER_CONFIG_BOUNDS["daily_loss_limit_pct"]
    proposed: Optional[float] = None
    rationale = ""
    if rec_rate <= 0.35:
        # Bot rarely bounces back the next day; halt sooner.
        proposed = max(lo, round(currently * 0.7, 4))
        rationale = (
            f"Recovery rate after loss days {rec_rate*100:.0f}% over "
            f"{n_loss} loss days is poor - tightening the daily loss "
            f"limit from {currently*100:.0f}% to {proposed*100:.0f}% "
            f"halts trading sooner before drawdown compounds."
        )
    elif rec_rate >= 0.70:
        # Bot reliably recovers; the current limit is leaving money
        # on the table by halting too quickly.
        proposed = min(hi, round(currently * 1.3, 4))
        rationale = (
            f"Recovery rate after loss days {rec_rate*100:.0f}% over "
            f"{n_loss} loss days is strong - loosening the daily loss "
            f"limit from {currently*100:.0f}% to {proposed*100:.0f}% "
            f"lets the bot keep trading through normal variance."
        )
    if proposed is None or abs(proposed - currently) < 0.01:
        return out
    out.append(Proposal(
        param_name="daily_loss_limit_pct",
        current_value=currently,
        proposed_value=proposed,
        evidence=rationale,
        proposal_metadata={
            "operation": "scalar_set",
            "field":     "daily_loss_limit_pct",
            "value":     proposed,
            "stats": {
                "n_loss_days":  n_loss,
                "recovery_rate": round(rec_rate, 4),
            },
        },
    ))
    return out


def _propose_archetype_price_band(diag: dict,
                                  current: UserConfig) -> list[Proposal]:
    """For each (archetype, 10pp price-band) cell whose ROI CI is
    statistically negative, propose adding that band to the
    archetype's price-band skip list.

    Reads `archetype_skip_market_price_bands` (a dict[archetype] ->
    list[[lo, hi]]) - operation is dict_set per archetype, where
    `value` is the full updated band list for that archetype.
    """
    out: list[Proposal] = []
    rows = diag.get("archetype_price_band") or []
    current_bands_map = dict(
        getattr(current, "archetype_skip_market_price_bands", {}) or {}
    )
    # Group flagged cells by archetype so a single archetype produces
    # one proposal even when multiple bands fail the CI gate.
    flagged: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        if not r.get("usable"):
            continue
        ci_hi = float(r.get("ci_hi") or 0.0)
        if ci_hi >= 0.0:
            continue  # CI doesn't exclude positive ROI; not statistically losing
        arch = r.get("archetype")
        lo_band = float(r.get("band_lo") or 0.0)
        hi_band = float(r.get("band_hi") or 0.0)
        if not arch or hi_band <= lo_band:
            continue
        flagged.setdefault(arch, []).append((lo_band, hi_band))
    for arch, new_bands in flagged.items():
        existing = current_bands_map.get(arch) or []
        existing_set = {
            (round(float(p[0]), 2), round(float(p[1]), 2))
            for p in existing
            if isinstance(p, (list, tuple)) and len(p) == 2
        }
        truly_new = [
            (lo, hi) for (lo, hi) in new_bands
            if (round(lo, 2), round(hi, 2)) not in existing_set
        ]
        if not truly_new:
            continue
        merged = sorted(
            existing_set | {(round(lo, 2), round(hi, 2)) for lo, hi in truly_new},
        )
        value = [[lo, hi] for lo, hi in merged]
        out.append(Proposal(
            param_name="archetype_skip_market_price_bands",
            current_value=None,
            proposed_value=None,
            evidence=(
                f"Archetype '{arch}': "
                + ", ".join(
                    f"band {int(lo*100)}-{int(hi*100)} has statistically "
                    f"negative ROI"
                    for lo, hi in truly_new
                )
                + f". Proposal: add {len(truly_new)} band(s) to the "
                f"per-archetype skip list so the bot stops entering "
                f"'{arch}' markets at those prices."
            ),
            proposal_metadata={
                "operation":    "dict_set",
                "target_field": "archetype_skip_market_price_bands",
                "key":          arch,
                "value":        value,
            },
        ))
    return out


def _propose_weekly_loss_limit(diag: dict,
                               current: UserConfig) -> list[Proposal]:
    """Same logic as _propose_daily_loss_limit but scoped to ISO
    weeks. Powers weekly_loss_limit_pct tuning.
    """
    out: list[Proposal] = []
    lr = diag.get("loss_week_recovery") or {}
    n_loss = int(lr.get("n_loss_weeks") or 0)
    if n_loss < 4:
        return out  # tighter sample gate: weeks are scarce
    rec_rate = lr.get("recovery_rate")
    if rec_rate is None:
        return out
    rec_rate = float(rec_rate)
    currently = float(getattr(current, "weekly_loss_limit_pct", 0.20) or 0.20)
    lo, hi = USER_CONFIG_BOUNDS["weekly_loss_limit_pct"]
    proposed: Optional[float] = None
    rationale = ""
    if rec_rate <= 0.35:
        proposed = max(lo, round(currently * 0.7, 4))
        rationale = (
            f"Weekly recovery rate after losing weeks "
            f"{rec_rate*100:.0f}% across {n_loss} weeks is poor - "
            f"tighten the weekly loss limit from "
            f"{currently*100:.0f}% to {proposed*100:.0f}% so a "
            f"bad week doesn't cascade into the next one."
        )
    elif rec_rate >= 0.70:
        proposed = min(hi, round(currently * 1.3, 4))
        rationale = (
            f"Weekly recovery rate after losing weeks "
            f"{rec_rate*100:.0f}% across {n_loss} weeks is strong - "
            f"loosen the weekly loss limit from "
            f"{currently*100:.0f}% to {proposed*100:.0f}% so the "
            f"bot keeps trading through normal weekly variance."
        )
    if proposed is None or abs(proposed - currently) < 0.01:
        return out
    out.append(Proposal(
        param_name="weekly_loss_limit_pct",
        current_value=currently,
        proposed_value=proposed,
        evidence=rationale,
        proposal_metadata={
            "operation": "scalar_set",
            "field":     "weekly_loss_limit_pct",
            "value":     proposed,
            "stats": {
                "n_loss_weeks": n_loss,
                "recovery_rate": round(rec_rate, 4),
            },
        },
    ))
    return out


def _propose_drawdown_halt(diag: dict,
                          current: UserConfig) -> list[Proposal]:
    """Tighten drawdown_halt_pct when the observed peak drawdown
    repeatedly approaches the halt threshold (the brake never quite
    fired but came close); loosen when the realized drawdown over
    a large sample stays well below the current halt (the brake is
    over-conservative and clipping profitable variance).

    Only fires after >= 50 settled trades - drawdown is a tail metric
    and tiny samples mislead.
    """
    out: list[Proposal] = []
    agg = diag.get("aggregate_roi") or {}
    n = int(agg.get("n_settled") or 0)
    if n < 50:
        return out
    peak_dd = float(agg.get("peak_drawdown") or 0.0)
    currently = float(getattr(current, "drawdown_halt_pct", 0.40) or 0.40)
    lo, hi = USER_CONFIG_BOUNDS["drawdown_halt_pct"]
    proposed: Optional[float] = None
    rationale = ""
    if peak_dd >= currently * 0.85:
        # Peak drawdown is uncomfortably close to the halt threshold.
        # Tighten so the brake fires sooner next time.
        proposed = max(lo, round(currently * 0.75, 4))
        rationale = (
            f"Peak drawdown {peak_dd*100:.1f}% over {n} settled trades "
            f"sits at {peak_dd/currently*100:.0f}% of the halt at "
            f"{currently*100:.0f}%. Tighten the halt to {proposed*100:.0f}% "
            f"so the brake fires before drawdown gets this close again."
        )
    elif peak_dd <= currently * 0.40:
        # Drawdown rarely approaches the halt. Loosen to free up
        # tolerance for normal variance.
        proposed = min(hi, round(currently * 1.25, 4))
        rationale = (
            f"Peak drawdown {peak_dd*100:.1f}% over {n} settled trades "
            f"is only {peak_dd/currently*100:.0f}% of the halt at "
            f"{currently*100:.0f}%. Loosen the halt to {proposed*100:.0f}% "
            f"since the current setting clips variance the bot can absorb."
        )
    if proposed is None or abs(proposed - currently) < 0.02:
        return out
    out.append(Proposal(
        param_name="drawdown_halt_pct",
        current_value=currently,
        proposed_value=proposed,
        evidence=rationale,
        proposal_metadata={
            "operation": "scalar_set",
            "field":     "drawdown_halt_pct",
            "value":     proposed,
            "stats": {
                "n":             n,
                "peak_drawdown": round(peak_dd, 4),
            },
        },
    ))
    return out


def _propose_streak_cooldown(diag: dict,
                            current: UserConfig) -> list[Proposal]:
    """Read loss_streak_analysis and tune streak_cooldown_losses.

    Logic: pick the smallest streak length whose mean trade-after-
    streak P&L is materially worse than the baseline. That's where
    the bot starts mean-reverting AGAINST itself. Cooldown should
    kick in at one less (so the very-bad N+1 trade never gets
    placed).

    If no streak length shows clear mean-reversion damage AND the
    current cooldown is restrictive, loosen by 1.
    """
    out: list[Proposal] = []
    ls = diag.get("loss_streak") or {}
    by_len = ls.get("by_length") or {}
    baseline = float(ls.get("baseline_mean_pnl") or 0.0)
    currently = int(getattr(current, "streak_cooldown_losses", 3) or 3)
    lo, hi = USER_CONFIG_BOUNDS["streak_cooldown_losses"]

    # Find the smallest streak length whose next-trade mean P&L is
    # substantially worse than baseline (more than 50% drop, with at
    # least 4 samples in that bucket).
    bad_length: Optional[int] = None
    for length_key in ("2", "3", "4+"):
        bucket = by_len.get(length_key)
        if not bucket:
            continue
        n_b = int(bucket.get("n") or 0)
        mean_next = float(bucket.get("mean_next_pnl") or 0.0)
        if n_b < 4:
            continue
        if baseline > 0 and mean_next < baseline * 0.5:
            bad_length = 2 if length_key == "2" else (3 if length_key == "3" else 4)
            break
        if baseline <= 0 and mean_next < baseline - 0.5:
            bad_length = 2 if length_key == "2" else (3 if length_key == "3" else 4)
            break

    proposed: Optional[int] = None
    rationale = ""
    if bad_length is not None:
        # Cooldown at bad_length means: after `bad_length` losses,
        # pause. So we set streak_cooldown_losses = bad_length.
        proposed = max(lo, min(hi, bad_length))
        if proposed != currently:
            stats = by_len.get(
                f"{bad_length}" if bad_length < 4 else "4+", {},
            )
            rationale = (
                f"After streaks of {bad_length} losses, the next "
                f"trade's mean P&L is ${stats.get('mean_next_pnl', 0):+.2f} "
                f"vs baseline ${baseline:+.2f} - clear mean-reversion "
                f"damage. Propose tightening cooldown from {currently} "
                f"to {proposed} so the bot pauses before that next "
                f"trade fires."
            )
    elif currently > lo:
        # No bad-streak signal AND we have enough streaks to say
        # so confidently. Consider loosening by 1.
        n_streaks = int(ls.get("n_streaks") or 0)
        if n_streaks >= 6:
            proposed = max(lo, currently - 1)
            if proposed != currently:
                rationale = (
                    f"Across {n_streaks} loss streaks of >=2, no streak "
                    f"length shows materially worse trade-after P&L - "
                    f"the cooldown at {currently} is conservatively "
                    f"clipping recoveries. Loosen to {proposed}."
                )
    if proposed is None or proposed == currently:
        return out
    out.append(Proposal(
        param_name="streak_cooldown_losses",
        current_value=float(currently),
        proposed_value=float(proposed),
        evidence=rationale,
        proposal_metadata={
            "operation": "scalar_set",
            "field":     "streak_cooldown_losses",
            "value":     proposed,
            "stats": {
                "n_streaks":     int(ls.get("n_streaks") or 0),
                "baseline_pnl":  round(baseline, 4),
                "by_length":     by_len,
            },
        },
    ))
    return out


# ── Review-report composition + delivery ─────────────────────────────────────
def _compose_and_deliver_report(user_id: str, mode: str,
                                cycle_size: int,
                                settled_count_bookmark: Optional[int] = None,
                                ) -> Optional[int]:
    """
    Compose the 50-trade review report, persist it to `learning_reports`,
    and emit a single event_log row pointing the dashboard at the new
    report. Every step is wrapped in its own try/except so a flaky
    downstream doesn't sink the learning cycle.

    Delivery is gated on `save_report` returning a non-None id. The
    unique constraint on `learning_reports(user_id, mode, settled_count)`
    causes the INSERT to no-op (returning None) when a row for the same
    bookmark already exists. That makes the constraint a hard gate
    against duplicate event-log rows, even if the bookmark logic ever
    regresses again.

    `settled_count_bookmark` is the global settled-count at cycle-fire
    time. compose_report internally sets `report["settled_count"]` to
    the cycle window size (always cycle_size), which would collide on
    every cycle once the unique constraint exists. Override it here
    with the global bookmark before save so the deduplication works
    on actual cycle-fire boundaries (50, 100, 150, ...) rather than
    the constant window size.
    """
    try:
        from engine import review_report
        report = review_report.compose_report(
            user_id=user_id, mode=mode, cycle_size=cycle_size,
        )
    except Exception as exc:
        print(f"[learning_cadence] compose_report failed: {exc}", file=sys.stderr)
        return None

    if settled_count_bookmark is not None:
        report["settled_count"] = int(settled_count_bookmark)

    report_id: Optional[int] = None
    try:
        report_id = review_report.save_report(
            user_id=user_id, mode=mode, report=report,
        )
    except Exception as exc:
        print(f"[learning_cadence] save_report failed: {exc}", file=sys.stderr)

    if report_id is None:
        # Either save failed or a duplicate row was rejected by the unique
        # constraint. Either way, do NOT emit an event - this is the path
        # that prevents the user from seeing two review reports in a row.
        print(
            "[learning_cadence] skipping notification: no fresh report row",
            file=sys.stderr,
        )
        return None

    try:
        from db.logger import log_event
        # Rich Telegram-HTML version mirrors the Messages Spec style.
        # Falls back to the plain description if rendering fails so a
        # formatter bug never silently drops the user-visible event.
        try:
            from feeds.telegram_messages import review_report_ready as _fmt
            telegram_html = _fmt(report)
        except Exception as exc:
            print(f"[learning_cadence] tg format failed: {exc}",
                  file=sys.stderr)
            telegram_html = None

        log_event(
            event_type="learning_report_ready",
            severity=20,
            description=(
                f"50-trade review ready ({int(report.get('settled_count') or 0)} "
                f"settled trades, mode={mode}, report_id={report_id})"
            ),
            source="learning_cadence",
            telegram_html=telegram_html,
        )
    except Exception as exc:
        print(f"[learning_cadence] event log write failed: {exc}",
              file=sys.stderr)

    return report_id


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
            # Source the per-archetype slice from `market_archetype`
            # (classifier output), not the legacy `category` column
            # which collapses sports into 'sports'. See the docstring
            # on `_archetype_pnl_attribution_impl` in diagnostics.py
            # for the same fix in the diagnostic path.
            rows = conn.execute(text(
                "SELECT cost_usd, realized_pnl_usd, market_archetype "
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


def _suggestion_fingerprint(
    param_name: str, metadata: Optional[dict]
) -> tuple[str, str, str]:
    """Identity key for a logical suggestion.

    Two suggestions hit the same fingerprint iff they're "the same
    recommendation" from the user's perspective — e.g. both tweak
    max_stake_pct, both set the tennis multiplier, both add
    'esports' to the skip list. Used to dedupe (refresh the date
    when proposed value is identical) and supersede (mark older
    rows stale when the new proposal has a different value).

    Tuple shape (param_name, operation, secondary_key):
      scalar_set     → (param, 'scalar_set', '')
      dict_set       → (param, 'dict_set',  <key>)
      list_append    → (param, 'list_append', <first item>)
      unknown/None   → (param, '', '')
    """
    meta = metadata or {}
    op = str(meta.get("operation") or "")
    if op == "dict_set":
        secondary = str(meta.get("key") or "")
    elif op == "list_append":
        items = meta.get("items") or []
        secondary = str(items[0]) if items else ""
    else:
        secondary = ""
    return (param_name, op, secondary)


def _store_pending_suggestion(prop: Proposal, user_id: str,
                              settled_count: int) -> bool:
    """Persist a proposal, deduplicating against existing pending rows.

    The earlier behaviour was a blind INSERT, so the same proposal
    surfacing on two consecutive scan cycles created two rows in
    the Intelligence panel ("May 9 · max_stake 0.034 → 0.024" and
    "May 13 · max_stake 0.034 → 0.024" both pending at the same
    time, observed 2026-05-16). Now:

      • Same fingerprint AND same proposed_value: refresh the
        existing row's created_at and evidence — user sees ONE row
        with the latest date.
      • Same fingerprint, different proposed_value: mark the older
        row as 'superseded' (kept in the DB for audit but hidden
        from the Pending list) and insert the new one. The newer
        proposal has fresher data and wins.
      • New fingerprint: INSERT as before.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        meta_json = json.dumps(prop.proposal_metadata) \
            if prop.proposal_metadata else None
        new_fp = _suggestion_fingerprint(
            prop.param_name, prop.proposal_metadata,
        )
        with get_engine().begin() as conn:
            # All candidate rows for this user + param. Status filter
            # includes both 'pending' (default) and 'snoozed' (user
            # asked us to remind later). We never re-issue against a
            # row that's already been applied or skipped.
            candidates = conn.execute(text(
                "SELECT id, proposed_value, metadata "
                "FROM pending_suggestions "
                "WHERE user_id = :uid AND param_name = :p "
                "  AND status IN ('pending', 'snoozed')"
            ), {"uid": user_id, "p": prop.param_name}).fetchall()

            same_fp_ids: list[int] = []
            identical_id: Optional[int] = None
            for cid, cval, cmeta in candidates:
                cmeta_dict = _decode_metadata(cmeta)
                if _suggestion_fingerprint(prop.param_name, cmeta_dict) != new_fp:
                    continue
                same_fp_ids.append(int(cid))
                # Treat numeric proposed_value as equal within 1e-9;
                # round-trips through JSON / SQLite reals can wobble.
                if (
                    prop.proposed_value is not None
                    and cval is not None
                    and abs(float(cval) - float(prop.proposed_value)) < 1e-9
                ):
                    identical_id = int(cid)
                elif prop.proposed_value is None and cval is None:
                    # Both proposed_value None (e.g. list_append):
                    # the fingerprint alone identifies them, so any
                    # same-fingerprint row IS identical.
                    identical_id = int(cid)

            if identical_id is not None:
                # Identical recommendation already pending. Refresh
                # the date + evidence so the user sees the latest
                # justification (sample sizes grow over time).
                conn.execute(text(
                    "UPDATE pending_suggestions SET "
                    "  created_at = CURRENT_TIMESTAMP, "
                    "  evidence = :ev, "
                    "  settled_count_at_creation = :sc, "
                    "  metadata = :meta "
                    "WHERE id = :id"
                ), {
                    "ev":   prop.evidence[:4000],
                    "sc":   settled_count,
                    "meta": meta_json,
                    "id":   identical_id,
                })
                return True

            if same_fp_ids:
                # Same logical knob, different proposed value — the
                # older proposals were drawn from less data; supersede
                # them silently and insert the fresh one.
                placeholders = ", ".join(f":id{i}" for i in range(len(same_fp_ids)))
                up_params = {f"id{i}": v for i, v in enumerate(same_fp_ids)}
                conn.execute(text(
                    f"UPDATE pending_suggestions SET "
                    f"  status = 'superseded', "
                    f"  resolved_at = CURRENT_TIMESTAMP, "
                    f"  resolved_by = 'system:newer-proposal' "
                    f"WHERE id IN ({placeholders})"
                ), up_params)

            conn.execute(text(
                "INSERT INTO pending_suggestions "
                "(user_id, param_name, current_value, proposed_value, "
                " evidence, backtest_delta, backtest_trades, "
                " settled_count_at_creation, metadata) "
                "VALUES (:uid, :k, :cur, :prop, :ev, :bd, :bt, :sc, :meta)"
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
            "created_at":     iso_utc(r[1]),
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


def list_resolved_suggestions(user_id: str = DEFAULT_USER_ID,
                              limit: int = 20) -> list[dict]:
    """List historically resolved (applied or skipped) suggestions.

    Used by the Intelligence page to show that the user has previously
    received recommendations even when the pending/snoozed queues are
    empty. Without this, a user who applied or skipped every prior
    proposal sees the same "first review is on the way" empty state as
    a brand-new install, which contradicts what they actually saw.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT id, created_at, param_name, current_value, "
                "       proposed_value, evidence, backtest_delta, "
                "       backtest_trades, status, settled_count_at_creation, "
                "       metadata, resolved_at, resolved_by "
                "FROM pending_suggestions "
                "WHERE user_id = :uid AND status IN ('applied', 'skipped') "
                "ORDER BY COALESCE(resolved_at, created_at) DESC "
                "LIMIT :lim"
            ), {"uid": user_id, "lim": int(limit)}).fetchall()
    except Exception as exc:
        print(f"[learning_cadence] list_resolved failed: {exc}", file=sys.stderr)
        return []

    out = []
    for r in rows:
        out.append({
            "id":              int(r[0]),
            "created_at":      iso_utc(r[1]),
            "param_name":      r[2],
            "current_value":   float(r[3]) if r[3] is not None else None,
            "proposed_value":  float(r[4]) if r[4] is not None else None,
            "evidence":        r[5],
            "backtest_delta":  float(r[6]) if r[6] is not None else None,
            "backtest_trades": int(r[7]) if r[7] is not None else None,
            "status":          r[8],
            "settled_count":   int(r[9]) if r[9] is not None else None,
            "metadata":        _decode_metadata(r[10]),
            "resolved_at":     iso_utc(r[11]) if r[11] is not None else None,
            "resolved_by":     r[12],
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
            "resolved_at = CURRENT_TIMESTAMP, resolved_by = :rb "
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


# ── "Next pending" helpers (dashboard /apply, /reject) ───────────────────────
# The dashboard /apply and /reject controls don't carry a suggestion id, so
# they work on the oldest currently-pending row for the caller. These helpers
# keep the local-api handler thin and let us stub a single entry point in
# tests.

def _oldest_pending_row(user_id: str) -> Optional[dict]:
    rows = list_pending_suggestions(user_id, include_snoozed=False)
    pending = [r for r in rows if r.get("status") == "pending"]
    if not pending:
        return None
    pending.sort(key=lambda r: r.get("created_at") or "")
    return pending[0]


def apply_next_pending_suggestion(user_id: str = DEFAULT_USER_ID,
                                  resolved_by: str = "local") -> dict:
    """Apply the oldest currently-pending suggestion for `user_id`.

    Returns ``{"status": "none"}`` when the user has no pending rows.
    Otherwise delegates to `apply_suggestion` and annotates the result with
    `display_key`, `display_previous`, and `display_value` so the dashboard
    can render one format for every operation type without caring about the
    underlying dispatch.
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
                                 resolved_by: str = "local") -> dict:
    """Mark the oldest currently-pending suggestion for `user_id` as skipped."""
    row = _oldest_pending_row(user_id)
    if row is None:
        return {"status": "none"}
    return skip_suggestion(int(row["id"]), user_id=user_id,
                           resolved_by=resolved_by)


def apply_all_pending_suggestions(user_id: str = DEFAULT_USER_ID,
                                  resolved_by: str = "local") -> dict:
    """Apply every currently-pending suggestion for `user_id` in one call.

    Walks the pending rows in `created_at` order and delegates each one
    through `apply_suggestion`, collecting per-row results. A failure on
    any single row is captured in `failed` but does not stop the loop -
    the caller (dashboard /apply handler) still applies as many rows as
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
    while at least one succeeded - the dashboard handler renders the full
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
        # sets so the dashboard handler can render dict / list / scalar
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
                "resolved_at = CURRENT_TIMESTAMP, resolved_by = :rb "
                "WHERE id = :id AND user_id = :uid "
                "  AND status IN ('pending', 'snoozed')"
            ), {"id": suggestion_id, "uid": user_id, "st": status, "rb": resolved_by})
            if result.rowcount == 0:
                return {"status": "not_found_or_resolved"}
        return {"status": status, "id": suggestion_id}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
