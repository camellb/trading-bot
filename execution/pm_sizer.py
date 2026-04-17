"""
Polymarket position sizer — quarter-Kelly with guardrails.

Kelly formula for binary prediction markets:

    If claude_p > market_p:  buy YES at market_p, win $1 if YES resolves
        kelly_fraction = (claude_p - market_p) / (1 - market_p)

    If claude_p < market_p:  buy NO at (1 - market_p), win $1 if NO resolves
        kelly_fraction = (market_p - claude_p) / market_p

Both reduce to: edge / payoff_if_won.

We apply the following guardrails on top of quarter-Kelly:

    1. Minimum edge threshold — skip if edge_bps < MIN_EDGE_BPS
    2. Lockup penalty — minimum edge scales with sqrt(days_to_end / 7).
       A 2-month lockup needs ~3x the edge of a 1-week trade.
    3. Minimum confidence — skip if confidence < MIN_CONFIDENCE
    4. Confidence scaling — size *= confidence (0..1)
    5. Max position pct — cap at MAX_POSITION_PCT of bankroll
    6. Absolute min/max — PM_MIN_TRADE_USD / PM_MAX_TRADE_USD
    7. Price sanity — refuse prices outside [0.02, 0.98] (no edge near
       certainty; round-lot friction dominates)

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


@dataclass
class SizingDecision:
    side:         str            # 'YES' | 'NO'
    entry_price:  float          # market price of the side we're buying (0..1)
    edge:         float          # absolute edge (0..1)
    kelly_full:   float          # uncapped Kelly fraction (0..1)
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
) -> SizingDecision:
    """
    Compute the staked USD + shares for a single market.
    Returns a SizingDecision; check .should_trade before acting.
    """
    # ── Sanity clamps ────────────────────────────────────────────────────────
    mp = float(max(0.0, min(1.0, market_price_yes)))
    cp = float(max(0.0, min(1.0, claude_probability)))
    cf = float(max(0.0, min(1.0, confidence)))

    # Which side?
    if cp > mp:
        side         = "YES"
        entry_price  = mp
        win_payoff   = 1.0 - mp     # what you gain per share on a win
    else:
        side         = "NO"
        entry_price  = 1.0 - mp
        win_payoff   = mp

    edge = abs(cp - mp)

    # ── Gate: min edge (with horizon scaling) ─────────────────────────────
    if mode is None:
        mode = getattr(config, "PM_MODE", "shadow")
    if mode == "live":
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
    if edge * 10_000.0 < min_edge_bps:
        return SizingDecision(
            side=side, entry_price=entry_price, edge=edge,
            kelly_full=0.0, kelly_frac=0.0, confidence=cf,
            stake_usd=0.0, shares=0.0,
            skip_reason=(f"edge {edge*10000:.0f}bps < min {min_edge_bps:.0f}bps"
                         f" (base {base_min_edge_bps:.0f} × {horizon_mult:.1f}x horizon)"),
        )

    # ── Gate: min confidence ─────────────────────────────────────────────────
    if mode == "live":
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
    # kelly_full = edge / win_payoff (equivalent to the formulas in the docstring)
    # Guard against divide-by-zero when win_payoff is tiny (shouldn't happen
    # after the 0.02/0.98 gate but defensive coding is cheap).
    kelly_full = edge / win_payoff if win_payoff > 1e-6 else 0.0
    kelly_full = max(0.0, min(1.0, kelly_full))

    kelly_frac = float(getattr(config, "PM_KELLY_FRACTION", 0.25))
    confidence_scale = cf  # 0..1 linear — low confidence => smaller bet

    fraction = kelly_full * kelly_frac * confidence_scale

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
