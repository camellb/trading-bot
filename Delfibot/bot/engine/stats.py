"""
Statistical utilities for the learning cadence + backtest.

Why this module exists
======================
Every "proposer" in learning_cadence.py was previously gated on a raw
sample-size threshold (e.g. "need 25 trades in this archetype"). That
gate doesn't reject obvious noise: 25 trades with a +5% sample ROI but
a 95% bootstrap CI of [-30%, +40%] is still indistinguishable from the
global mean.

These helpers add real statistical gating:

    bootstrap_roi_ci(rows)
        95% bootstrap CI on ROI for a list of settled-trade rows.

    cell_passes_ci_gate(cell_rows, global_rows)
        True iff the cell's bootstrap CI lies entirely on one side of
        the global mean. This is the "would publishing this finding be
        defensible" test that gates every proposer.

    min_n_for_detection(baseline_roi, target_lift, alpha=0.05, power=0.80)
        Power calculation: how many trades per arm to detect a lift of
        `target_lift` at the given alpha and power. Used to surface
        "n / required_n" in every review-report row so users can see
        when a cell is too thin to act on.

The bootstrap is deliberately stdlib-only (random + statistics). We
avoid pulling scipy/numpy into the sidecar bundle for a 30-line piece
of math; PyInstaller builds stay lean and the runtime cost is fine
(5000 resamples on 200 rows is ~30ms).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence


# Default minima used by the learning cadence. Derived from rough
# power-calc rule of thumb: a +5% ROI lift over a -1.4% baseline at
# alpha=0.05 / power=0.80 needs roughly 50 trades per arm assuming
# trade-PnL stdev ~ 0.5x cost (typical for our archetype-multiplier
# bands). Sub-cell decisions (archetype x price-band) demand 2x because
# the underlying variance is higher when you're slicing finer.
MIN_N_ARCHETYPE_LEVEL = 50
MIN_N_SUBCELL_LEVEL = 100


@dataclass
class CIResult:
    """Bootstrap CI on a sample of settled trades.

    `roi_pct` is the point estimate (cumulative PnL / cumulative cost,
    expressed as a percent). `lo_pct` / `hi_pct` are the 2.5% / 97.5%
    bootstrap quantiles - the 95% confidence interval. `n` is the
    sample size that produced the estimate.

    A cell is considered statistically distinguishable from a target
    value `t` when `t` lies outside [lo_pct, hi_pct]. The
    `cell_passes_ci_gate` helper formalises that test against the
    global ROI.
    """
    n: int
    roi_pct: float
    lo_pct: float
    hi_pct: float

    def excludes(self, target_pct: float) -> bool:
        """True iff `target_pct` falls outside the [lo, hi] CI."""
        return target_pct < self.lo_pct or target_pct > self.hi_pct

    def is_winning(self) -> bool:
        """True iff the entire CI is positive (cell beats break-even)."""
        return self.lo_pct > 0.0

    def is_losing(self) -> bool:
        """True iff the entire CI is negative (cell loses vs break-even)."""
        return self.hi_pct < 0.0


def _roi_pct(rows: Sequence[dict]) -> float:
    """ROI of a sample, expressed as a percentage. Empty sample = 0.0."""
    cost = 0.0
    pnl = 0.0
    for r in rows:
        cost += float(r.get("cost_usd") or 0.0)
        pnl += float(r.get("realized_pnl_usd") or 0.0)
    return (pnl / cost * 100.0) if cost > 0 else 0.0


def bootstrap_roi_ci(
    rows: Sequence[dict],
    *,
    n_iter: int = 5000,
    alpha: float = 0.05,
    seed: int = 42,
) -> CIResult:
    """Bootstrap 95% CI on cumulative ROI for a list of trade rows.

    Each row must have `cost_usd` and `realized_pnl_usd` numeric
    fields. ROI is `sum(pnl) / sum(cost) * 100`. We resample the trades
    with replacement `n_iter` times, recompute ROI on each resample,
    and return the 2.5% / 97.5% quantiles.

    Deterministic by default (seed=42) so two runs over the same data
    return the same CI - important for the learning cadence since we
    don't want suggestion proposals flickering between runs.

    On samples below 2 we return a degenerate CI (lo=hi=roi).
    """
    n = len(rows)
    if n < 2:
        roi = _roi_pct(rows)
        return CIResult(n=n, roi_pct=roi, lo_pct=roi, hi_pct=roi)

    rng = random.Random(seed)
    rows_list = list(rows)
    samples = []
    for _ in range(n_iter):
        sample = [rows_list[rng.randint(0, n - 1)] for _ in range(n)]
        samples.append(_roi_pct(sample))
    samples.sort()
    lo = samples[int(n_iter * alpha / 2.0)]
    hi = samples[int(n_iter * (1.0 - alpha / 2.0))]
    return CIResult(n=n, roi_pct=_roi_pct(rows_list), lo_pct=lo, hi_pct=hi)


def cell_passes_ci_gate(
    cell_rows: Sequence[dict],
    global_rows: Sequence[dict],
    *,
    n_iter: int = 5000,
) -> tuple[bool, CIResult, float]:
    """Decide whether a per-cell finding is publishable.

    A cell passes if its bootstrap 95% CI lies entirely on one side of
    the global ROI. Otherwise the cell's apparent edge is consistent
    with sample variance around the global mean and we should NOT make
    a config change based on it.

    Returns (passes, cell_ci, global_roi_pct).
    """
    cell_ci = bootstrap_roi_ci(cell_rows, n_iter=n_iter)
    g_roi = _roi_pct(global_rows)
    passes = cell_ci.excludes(g_roi)
    return passes, cell_ci, g_roi


def min_n_for_detection(
    baseline_roi: float,
    target_lift: float,
    *,
    alpha: float = 0.05,
    power: float = 0.80,
    sigma: float = 0.5,
) -> int:
    """Power calc: trades per arm to detect a lift at given alpha + power.

    Uses the standard two-sample-z normal-approximation formula:

        n >= 2 * (z_alpha/2 + z_power)^2 * sigma^2 / lift^2

    Where:
        baseline_roi: per-trade ROI under V1 (decimal, e.g. -0.014).
            Currently unused in the formula (the variance term sigma
            captures most of it) but kept on the signature so callers
            can pass it for documentation; future versions may use it
            to refine sigma.
        target_lift: how big a lift we want to detect (decimal,
            e.g. 0.05 for "+5% ROI").
        sigma: per-trade ROI standard deviation. Default 0.5 reflects
            roughly what you see when settled-trade PnL is in the
            range -1x to +5x the cost (Polymarket binaries with
            entry near 0.5).
        alpha: type-I error rate.
        power: 1 - type-II error.

    Returns the per-arm sample size, ceiled to the next integer. Use
    2x this for the total sample (treated + control).

    Caveats:
      - Assumes the trade-PnL distribution is roughly normal at scale.
        Polymarket payoffs are bimodal (-cost on loss, +(1-entry)/entry*cost
        on win), so the normal approximation is a rough lower bound.
        Real n-required is typically 1.5-2x the formula output.
      - Doesn't include a multiple-testing correction for the tens of
        cells the review report inspects.
    """
    _ = baseline_roi  # intentionally unused; see docstring
    # Hard-coded z values to avoid pulling scipy.stats.norm.ppf in.
    Z = {0.05: 1.96, 0.01: 2.575, 0.10: 1.645}
    POW = {0.80: 0.842, 0.90: 1.282, 0.95: 1.645}
    z_alpha = Z.get(alpha, 1.96)
    z_pow = POW.get(power, 0.842)
    if target_lift == 0:
        return 0
    n = 2.0 * (z_alpha + z_pow) ** 2 * sigma ** 2 / (target_lift ** 2)
    return int(math.ceil(n))


def proposal_block_reason(
    cell_rows: Sequence[dict],
    global_rows: Sequence[dict],
    *,
    min_n: int = MIN_N_ARCHETYPE_LEVEL,
) -> str | None:
    """Return None if a proposer should fire, else a human-readable
    reason string explaining why it's blocked.

    Three-step gate:
      1. Cell sample is at least `min_n`.
      2. Cell bootstrap CI excludes the global ROI.
      3. CI is entirely on one side of zero (we know the SIGN of the
         effect, not just that it's different from baseline).

    Used by every learning_cadence proposer to decide whether the
    finding it computed has enough statistical backing to surface as
    an actionable suggestion. Returning None = ship the proposal.
    """
    n = len(cell_rows)
    if n < min_n:
        return f"insufficient sample (n={n}, min={min_n})"
    passes, cell_ci, g_roi = cell_passes_ci_gate(cell_rows, global_rows)
    if not passes:
        return (
            f"CI overlaps global ROI: cell ROI={cell_ci.roi_pct:+.1f}% "
            f"CI=[{cell_ci.lo_pct:+.1f}%, {cell_ci.hi_pct:+.1f}%] "
            f"contains global={g_roi:+.1f}%"
        )
    if not (cell_ci.is_winning() or cell_ci.is_losing()):
        return (
            f"CI straddles zero: [{cell_ci.lo_pct:+.1f}%, "
            f"{cell_ci.hi_pct:+.1f}%]"
        )
    return None


def summarize_cell(
    cell_rows: Sequence[dict],
    global_rows: Sequence[dict],
    *,
    min_n: int = MIN_N_ARCHETYPE_LEVEL,
) -> dict:
    """Compute the standard cell-summary dict the review report shows.

    Includes n, ROI, CI, blocking reason (if any), and the power-calc
    sample-size required for a +5% lift detection.

    Returned dict shape (consumed by review_report._shape_per_archetype):
        {
          "n": int,
          "roi_pct": float,
          "ci_lo_pct": float,
          "ci_hi_pct": float,
          "min_n_required": int,
          "block_reason": str | None,  # None = actionable
          "verdict": "WINNING" | "LOSING" | "noise",
        }
    """
    ci = bootstrap_roi_ci(cell_rows)
    block = proposal_block_reason(cell_rows, global_rows, min_n=min_n)

    if ci.is_winning():
        verdict = "WINNING"
    elif ci.is_losing():
        verdict = "LOSING"
    else:
        verdict = "noise"

    g_roi_decimal = _roi_pct(global_rows) / 100.0
    n_required = min_n_for_detection(
        baseline_roi=g_roi_decimal, target_lift=0.05,
    )

    return {
        "n":               ci.n,
        "roi_pct":         round(ci.roi_pct, 2),
        "ci_lo_pct":       round(ci.lo_pct, 2),
        "ci_hi_pct":       round(ci.hi_pct, 2),
        "min_n_required":  n_required,
        "block_reason":    block,
        "verdict":         verdict,
    }


__all__ = [
    "CIResult",
    "MIN_N_ARCHETYPE_LEVEL",
    "MIN_N_SUBCELL_LEVEL",
    "bootstrap_roi_ci",
    "cell_passes_ci_gate",
    "min_n_for_detection",
    "proposal_block_reason",
    "summarize_cell",
]
