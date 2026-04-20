"""
Polymarket position sizer — variance-adjusted Kelly with guardrails.

Kelly formula for binary prediction markets:

    If claude_p > market_p:  buy YES at market_p, win $1 if YES resolves
        kelly_fraction = (claude_p - market_p) / (1 - market_p)

    If claude_p < market_p:  buy NO at (1 - market_p), win $1 if NO resolves
        kelly_fraction = (market_p - claude_p) / market_p

Both reduce to: edge / payoff_if_won.

We apply the following guardrails on top of quarter-Kelly:

    1. Bayesian shrinkage — shrink Claude's probability toward market
       price based on per-archetype trust factor. Sports markets are
       efficient (low trust), niche markets less so.
    2. Max edge ceiling — per-archetype ceiling replaces the old blanket
       PM_MAX_EDGE_BPS. Markets with verifiable data (weather, crypto)
       get higher ceilings. Research quality modulates the ceiling.
    3. Cheap NO protection — skip NO bets at entry < 5c (betting
       against 95%+ favorites has catastrophic loss profile)
    4. Minimum edge threshold — skip if edge_bps < MIN_EDGE_BPS
    5. Lockup penalty — minimum edge scales with sqrt(days_to_end / 7).
       A 2-month lockup needs ~3x the edge of a 1-week trade.
    6. Minimum confidence — skip if confidence < MIN_CONFIDENCE
    7. Combined variance-adjusted Kelly — the probability estimate has
       two sources of uncertainty: (a) archetype estimation error (how
       wrong Claude typically is for this market type) and (b) per-market
       confidence uncertainty. These combine into a single σ² term that
       feeds the standard variance-adjusted Kelly formula:
           f* = kelly_full - σ²_total / payoff²
       This replaces the old heuristic of variance_penalty + confidence
       multiplication. Low confidence adds variance, which naturally
       shrinks or eliminates the Kelly fraction when uncertainty
       swallows the edge. No separate confidence scaling needed.
    8. Resolution source risk — unreliable resolution sources get
       smaller positions.
    9. Max position pct — cap at MAX_POSITION_PCT of bankroll
   10. Absolute min/max — PM_MIN_TRADE_USD / PM_MAX_TRADE_USD
   11. Price sanity — refuse prices outside [0.02, 0.98]

Rationale for quarter-Kelly:
    Full Kelly maximises log-wealth but assumes perfectly calibrated
    probabilities. When our Brier score indicates overconfidence, full
    Kelly can produce catastrophic drawdowns. Quarter-Kelly gives up
    ~15% of log-wealth growth in exchange for ~4x lower drawdown risk
    and is the standard choice for real-money implementations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import config

LOCKUP_BASELINE_DAYS = 7.0

# Confidence → additional variance coefficient.
# At confidence=0, adds 0.015 to σ² (triples a typical archetype variance).
# At confidence=1, adds 0 (fully trust the point estimate).
# Calibrated so that moderate-confidence (0.5) moderate-edge (~400bp) trades
# still pass, but low-confidence (<0.3) small-edge trades are rejected.
CONFIDENCE_VARIANCE_COEFF = 0.015


@dataclass
class SizingDecision:
    side:         str            # 'YES' | 'NO'
    entry_price:  float          # market price of the side we're buying (0..1)
    edge:         float          # absolute edge (0..1)
    kelly_full:   float          # uncapped naive Kelly fraction (0..1)
    kelly_frac:   float          # applied Kelly multiplier (e.g. 0.25)
    confidence:   float          # Claude confidence (0..1)
    stake_usd:    float          # final stake in USD
    shares:       float          # shares purchased
    skip_reason:  Optional[str]  # non-None => don't trade

    @property
    def should_trade(self) -> bool:
        return self.skip_reason is None and self.stake_usd > 0

    def to_dict(self) -> dict:
        return {
            "side":        self.side,
            "entry_price": self.entry_price,
            "edge":        self.edge,
            "kelly_full":  self.kelly_full,
            "kelly_frac":  self.kelly_frac,
            "confidence":  self.confidence,
            "stake_usd":   self.stake_usd,
            "shares":      self.shares,
            "skip_reason": self.skip_reason,
        }


def size_position(
    market_price_yes: float,
    claude_probability: float,
    confidence: float,
    bankroll_usd: float,
    days_to_end: Optional[float] = None,
    size_multiplier: float = 1.0,
    mode: Optional[str] = None,
    archetype: Optional[str] = None,
    resolution_quality: Optional[float] = None,
    resolution_source_score: Optional[float] = None,
    research_quality: Optional[float] = None,
) -> SizingDecision:
    """
    Compute the staked USD + shares for a single market.
    Returns a SizingDecision; check .should_trade before acting.

    archetype: market archetype for per-archetype edge/confidence overrides.
    resolution_quality: 0..1 score for resolution clarity; low values penalized.
    research_quality: 0..1 score for research depth; modulates edge ceiling.
    """
    # ── Sanity clamps ────────────────────────────────────────────────────────
    mp = float(max(0.0, min(1.0, market_price_yes)))
    cp = float(max(0.0, min(1.0, claude_probability)))
    cf = float(max(0.0, min(1.0, confidence)))

    # ── Confidence dampening per archetype ──────────────────────────────────
    if archetype:
        dampen_map = getattr(config, "PM_CONFIDENCE_DAMPEN", {})
        if isinstance(dampen_map, dict) and archetype in dampen_map:
            dampen = float(max(0.1, min(1.0, dampen_map[archetype])))
            cf = cf * dampen

    # ── Bayesian shrinkage toward market price ─────────────────────────────
    # Trust the model vs market based on archetype track record.
    # Sports markets are efficient (low trust), price_threshold less so.
    trust_map = getattr(config, "PM_ARCHETYPE_MODEL_TRUST", {})
    if isinstance(trust_map, dict) and archetype and archetype in trust_map:
        trust = float(max(0.05, min(1.0, trust_map[archetype])))
    else:
        trust = 0.30  # conservative default
    cp = trust * cp + (1.0 - trust) * mp  # shrink toward market price

    # ── Resolution quality gate ──────────────────────────────────────────────
    min_res_quality = float(getattr(config, "PM_MIN_RESOLUTION_QUALITY", 0.3))
    if resolution_quality is not None and resolution_quality < min_res_quality:
        # Determine side for the skip reason
        side = "YES" if cp > mp else "NO"
        entry_price = mp if side == "YES" else 1.0 - mp
        return SizingDecision(
            side=side, entry_price=entry_price, edge=abs(cp - mp),
            kelly_full=0.0, kelly_frac=0.0, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=(f"resolution quality {resolution_quality:.2f} < "
                         f"min {min_res_quality:.2f}"),
        )

    # ── Execution realism: adjust entry price for spread + fees ──────────────
    spread = float(getattr(config, "PM_SHADOW_SPREAD_ESTIMATE", 0.0))
    fee_rate = float(getattr(config, "PM_SHADOW_FEE_RATE", 0.0))

    # Which side?
    if cp > mp:
        side         = "YES"
        # In reality we'd buy at ask = mid + half-spread
        entry_price  = min(0.99, mp + spread)
        win_payoff   = 1.0 - entry_price
    else:
        side         = "NO"
        # Buy NO at (1 - mid) + half-spread
        entry_price  = min(0.99, (1.0 - mp) + spread)
        win_payoff   = 1.0 - entry_price

    edge = abs(cp - mp)
    # Reduce effective edge by execution costs (spread already in entry_price,
    # but fees eat into the payout).
    effective_edge = max(0.0, edge - spread - fee_rate)

    # ── Gate: per-archetype edge ceiling ─────────────────────────────────────
    # Use per-archetype ceiling when available, fall back to blanket default.
    # Markets with verifiable data (weather, crypto) get higher ceilings.
    archetype_ceilings = getattr(config, "PM_ARCHETYPE_MAX_EDGE_BPS", {})
    if isinstance(archetype_ceilings, dict) and archetype and archetype in archetype_ceilings:
        max_edge_bps = float(archetype_ceilings[archetype])
    else:
        max_edge_bps = float(getattr(config, "PM_MAX_EDGE_BPS", 2500))

    # Research quality modulates the ceiling: high-quality research
    # (many sources, recent data) relaxes it; low-quality tightens it.
    if research_quality is not None:
        if research_quality > 0.7:
            max_edge_bps *= 1.3   # good research → relax 30%
        elif research_quality < 0.3:
            max_edge_bps *= 0.7   # poor research → tighten 30%

    if edge * 10_000.0 > max_edge_bps:
        rq_info = f", rq={research_quality:.2f}" if research_quality is not None else ""
        return SizingDecision(
            side=side, entry_price=entry_price, edge=edge,
            kelly_full=0.0, kelly_frac=0.0, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=(f"edge {edge*10000:.0f}bps > max {max_edge_bps:.0f}bps "
                         f"(archetype={archetype or 'unknown'}{rq_info})"),
        )

    # ── Gate: cheap NO contract protection ──────────────────────────────────
    # Buying NO at <5c means Claude thinks a 95%+ favorite will lose.
    # Only block the most extreme cases; allow more trades for simulation data.
    # In live mode, this should be tightened back to 0.12.
    if side == "NO" and entry_price < 0.05:
        return SizingDecision(
            side=side, entry_price=entry_price, edge=edge,
            kelly_full=0.0, kelly_frac=0.0, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=(f"NO entry {entry_price:.3f} < 0.05 "
                         f"(extreme disagreement with market — refusing cheap NO)"),
        )

    # ── Gate: min edge (with horizon scaling) ─────────────────────────────
    if mode is None:
        mode = getattr(config, "PM_MODE", "shadow")

    # Per-archetype edge override
    archetype_overrides = getattr(config, "PM_ARCHETYPE_EDGE_OVERRIDES", {})
    if isinstance(archetype_overrides, dict) and archetype and archetype in archetype_overrides:
        base_min_edge_bps = float(archetype_overrides[archetype])
    elif mode == "live":
        base_min_edge_bps = float(getattr(config, "PM_LIVE_MIN_EDGE_BPS", 500))
    else:
        base_min_edge_bps = float(getattr(config, "PM_SHADOW_MIN_EDGE_BPS", 300))

    horizon_mult = 1.0
    if days_to_end is not None and days_to_end > 0:
        if days_to_end < LOCKUP_BASELINE_DAYS:
            horizon_mult = 1.0
        else:
            horizon_mult = math.sqrt(days_to_end / LOCKUP_BASELINE_DAYS)
    min_edge_bps = base_min_edge_bps * horizon_mult
    if effective_edge * 10_000.0 < min_edge_bps:
        return SizingDecision(
            side=side, entry_price=entry_price, edge=edge,
            kelly_full=0.0, kelly_frac=0.0, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=(f"effective edge {effective_edge*10000:.0f}bps < min {min_edge_bps:.0f}bps"
                         f" (base {base_min_edge_bps:.0f} × {horizon_mult:.1f}x horizon"
                         f", spread={spread*10000:.0f}bps, fees={fee_rate*10000:.0f}bps)"),
        )

    # ── Gate: min confidence ─────────────────────────────────────────────────
    # Per-archetype confidence override
    conf_overrides = getattr(config, "PM_ARCHETYPE_CONFIDENCE_OVERRIDES", {})
    if isinstance(conf_overrides, dict) and archetype and archetype in conf_overrides:
        min_conf = float(conf_overrides[archetype])
    elif mode == "live":
        min_conf = float(getattr(config, "PM_LIVE_MIN_CONFIDENCE", 0.55))
    else:
        min_conf = float(getattr(config, "PM_SHADOW_MIN_CONFIDENCE", 0.30))
    if cf < min_conf:
        return SizingDecision(
            side=side, entry_price=entry_price, edge=edge,
            kelly_full=0.0, kelly_frac=0.0, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=f"confidence {cf:.2f} < min {min_conf:.2f}",
        )

    # ── Gate: price sanity ───────────────────────────────────────────────────
    if entry_price <= 0.02 or entry_price >= 0.98:
        return SizingDecision(
            side=side, entry_price=entry_price, edge=edge,
            kelly_full=0.0, kelly_frac=0.0, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=f"entry price {entry_price:.3f} outside [0.02, 0.98]",
        )

    # ── Kelly ────────────────────────────────────────────────────────────────
    # kelly_full = effective_edge / win_payoff (net of execution costs)
    # Guard against divide-by-zero when win_payoff is tiny (shouldn't happen
    # after the 0.02/0.98 gate but defensive coding is cheap).
    kelly_full = effective_edge / win_payoff if win_payoff > 1e-6 else 0.0
    kelly_full = max(0.0, min(1.0, kelly_full))

    kelly_frac = float(getattr(config, "PM_KELLY_FRACTION", 0.25))

    # ── Combined variance-adjusted Kelly ───────────────────────────────────
    # Two sources of uncertainty feed a single variance term:
    #
    #   σ²_total = σ²_archetype + σ²_confidence
    #
    # σ²_archetype: historical estimation error per archetype (from config).
    # σ²_confidence: per-market uncertainty derived from Claude's confidence.
    #   Low confidence → large additional variance → larger penalty → smaller
    #   or zero Kelly fraction. This replaces the old heuristic of multiplying
    #   stake by confidence, which was too aggressive at low confidence and
    #   had no theoretical basis.
    #
    # The variance-adjusted Kelly formula:
    #   f* = edge/payoff - σ²_total/payoff²
    #
    # When σ²_total is large enough to exceed edge*payoff, f* ≤ 0 and the
    # trade is correctly rejected: the uncertainty has swallowed the edge.
    variance_map = getattr(config, "PM_ARCHETYPE_ESTIMATOR_VARIANCE", {})
    sigma_p_sq_arch = 0.005  # default for unknown archetypes
    if isinstance(variance_map, dict) and archetype and archetype in variance_map:
        sigma_p_sq_arch = float(variance_map[archetype])

    # Confidence-derived variance: maps confidence to additional σ².
    # At cf=1.0: adds 0 (fully trust estimate)
    # At cf=0.5: adds 0.0075 (comparable to archetype variance)
    # At cf=0.2: adds 0.012 (dominates, making rejection likely)
    sigma_p_sq_conf = CONFIDENCE_VARIANCE_COEFF * (1.0 - cf)
    sigma_p_sq_total = sigma_p_sq_arch + sigma_p_sq_conf

    variance_penalty = sigma_p_sq_total / (win_payoff ** 2) if win_payoff > 1e-6 else 1.0
    kelly_adjusted = kelly_full - variance_penalty
    if kelly_adjusted <= 0:
        return SizingDecision(
            side=side, entry_price=entry_price, edge=edge,
            kelly_full=kelly_full, kelly_frac=kelly_frac, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=(f"variance-adjusted Kelly <= 0: kelly={kelly_full:.4f} "
                         f"- penalty={variance_penalty:.4f} "
                         f"(σ²_arch={sigma_p_sq_arch:.4f} + "
                         f"σ²_conf={sigma_p_sq_conf:.4f}, "
                         f"archetype={archetype})"),
        )

    # No separate confidence multiplication — confidence is already
    # encoded in σ²_conf which feeds the variance penalty above.
    fraction = kelly_adjusted * kelly_frac

    # ── Resolution source risk factor ──────────────────────────────────────
    # Unreliable resolution sources → smaller positions
    if resolution_source_score is not None and resolution_source_score < 1.0:
        fraction *= max(0.3, resolution_source_score)

    # ── Cap to max pct of bankroll ───────────────────────────────────────────
    max_pct = float(getattr(config, "PM_MAX_POSITION_PCT", 0.05))
    fraction = min(fraction, max_pct)

    stake_usd = bankroll_usd * fraction

    # ── Degraded-feed multiplier ─────────────────────────────────────────
    if 0 < size_multiplier < 1.0:
        stake_usd *= size_multiplier

    # ── Absolute min/max ─────────────────────────────────────────────────────
    min_trade = float(getattr(config, "PM_MIN_TRADE_USD", 2.0))
    max_trade = float(getattr(config, "PM_MAX_TRADE_USD", 25.0))

    if stake_usd < min_trade:
        return SizingDecision(
            side=side, entry_price=entry_price, edge=edge,
            kelly_full=kelly_full, kelly_frac=kelly_frac, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=f"computed stake ${stake_usd:.2f} < min ${min_trade:.2f}",
        )

    stake_usd = min(stake_usd, max_trade)
    shares    = stake_usd / entry_price if entry_price > 0 else 0.0

    return SizingDecision(
        side=side, entry_price=entry_price, edge=edge,
        kelly_full=kelly_full, kelly_frac=kelly_frac, confidence=cf,
        stake_usd=stake_usd, shares=shares,
        skip_reason=None,
    )
