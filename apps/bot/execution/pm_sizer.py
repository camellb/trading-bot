"""
Polymarket position sizer - two gates plus a confidence softener.

Gate 1 - side selection (never skips)
    Two modes based on confidence.

    High-confidence override (confidence >= confidence_override_threshold,
    default 0.75): ignore the market, side is YES if claude_p >= 0.50 else NO.

    Low-confidence mean rule (confidence < confidence_override_threshold):
    compute mean = (claude_p + market_p_yes) / 2. Side is YES if mean >= 0.50
    else NO. When Claude and the market disagree, the mean pulls the chosen
    side toward whichever one is further from 0.50. Gate 2 then decides
    whether Claude's probability on that side is strong enough to fire.

Gate 2 - minimum p_win
    p_win = claude_p on YES, 1 - claude_p on NO. Must be >= min_p_win
    (default 0.50). Implicitly rejects trades where the mean rule picked
    a side Claude doesn't believe in.

Confidence softener (size only - never skips)
    multiplier = min(1.0, 0.01 + (confidence / confidence_full_stake) * 0.99)
    At confidence 0 the stake is 1% of base; at confidence_full_stake (default
    0.70) the stake is full; above that the multiplier is capped at 1.0.

Final stake = bankroll * base_stake_pct * multiplier, capped at
bankroll * max_stake_pct, with an absolute $2 minimum.

Why there is no Gate 3. A prior version of this sizer gated on minimum
expected return - (1/ask) - 1 - cost >= min_expected_return. It was removed
because it skipped heavy favourites where the math still favoured taking
the bet: if Claude's calibrated p_win is 0.95 and the market prices YES at
0.94, the EV is positive and we should trade it. Gate 3 muted exactly
those trades. Side selection + p_win floor is the full skip logic.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


# Absolute floor for a tradeable stake in USD.
_MIN_ABSOLUTE_STAKE_USD = 2.0

# Cost assumption: spread + fees + slippage, as a fraction of the payoff.
# Currently unused - retained for future P&L modelling and for call sites
# that pass a `cost_assumption_override` for forward-looking analyses.
COST_ASSUMPTION = 0.015


@dataclass
class SizingDecision:
    side:         str             # 'YES' | 'NO' | '' (only if pre-side failure)
    entry_price:  float           # ask price of the chosen side (0..1)
    ev:           float           # retained for schema compatibility; always 0.0
    p_win:        float           # probability chosen side wins (0..1)
    confidence:   float           # Claude confidence (0..1)
    stake_usd:    float           # final stake in USD
    shares:       float           # shares purchased
    skip_reason:  Optional[str]   # non-None → don't trade

    @property
    def should_trade(self) -> bool:
        return self.skip_reason is None and self.stake_usd > 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["should_trade"] = self.should_trade
        return d


def size_position(
    claude_p:    float,
    confidence:  float,
    ask_yes:     float,
    ask_no:      float,
    bankroll:    float,
    user_config,
    archetype:   Optional[str] = None,
) -> SizingDecision:
    """
    Apply side selection + Gate 2 + confidence softener and return
    a SizingDecision. `skip_reason` is None iff the bet is taken.
    """
    cp  = _clamp01(claude_p)
    cf  = _clamp01(confidence)
    ay  = _clamp_price(ask_yes)
    an  = _clamp_price(ask_no)

    # Thresholds come from user_config so the dashboard can edit them.
    min_p_win                       = float(getattr(user_config, "min_p_win", 0.50))
    confidence_full_stake           = float(getattr(user_config, "confidence_full_stake", 0.70))
    confidence_override_threshold   = float(getattr(user_config, "confidence_override_threshold", 0.75))

    # ── Safety gate: archetype skip list ────────────────────────────────────
    archetype_skip = tuple(getattr(user_config, "archetype_skip_list", ()) or ())
    if archetype is not None and archetype in archetype_skip:
        return _skip(cp, cf, f"category '{archetype}' is on the skip list")

    # ── Gate 1: side selection (never skips) ────────────────────────────────
    # High-confidence: follow Claude directly, ignore market.
    # Low-confidence: pick the side whose mean(Claude, market) is >= 0.50.
    market_p_yes = ay
    if cf >= confidence_override_threshold:
        side_is_yes = cp >= 0.50
    else:
        mean_p_yes = (cp + market_p_yes) / 2.0
        side_is_yes = mean_p_yes >= 0.50

    if side_is_yes:
        side, entry, p_win = "YES", ay, cp
    else:
        side, entry, p_win = "NO", an, 1.0 - cp

    # ── Gate 2: minimum p_win ───────────────────────────────────────────────
    if p_win < min_p_win:
        return _skip(
            cp, cf,
            f"p_win {p_win:.2f} below min_p_win {min_p_win:.2f}",
            side=side, entry=entry, p_win=p_win,
        )

    # Entry-price sanity - can't compute shares without a positive ask.
    if entry <= 0:
        return _skip(
            cp, cf, f"non-positive entry price ({entry})",
            side=side, entry=entry, p_win=p_win,
        )

    # ── Confidence softener (size only, never skip) ─────────────────────────
    multiplier = _confidence_multiplier(cf, full_stake=confidence_full_stake)

    # ── Stake sizing ────────────────────────────────────────────────────────
    base_pct = float(user_config.base_stake_pct)
    max_pct  = float(user_config.max_stake_pct)

    stake_pct = min(base_pct * multiplier, max_pct)

    if bankroll <= 0:
        return _skip(
            cp, cf, f"non-positive bankroll (${bankroll:.2f})",
            side=side, entry=entry, p_win=p_win,
        )

    stake = max(
        _MIN_ABSOLUTE_STAKE_USD,
        min(bankroll * stake_pct, bankroll * max_pct),
    )

    shares = stake / entry if entry > 0 else 0.0

    return SizingDecision(
        side=side, entry_price=entry, ev=0.0, p_win=p_win,
        confidence=cf, stake_usd=stake, shares=shares,
        skip_reason=None,
    )


def _confidence_multiplier(confidence: float, *, full_stake: float) -> float:
    """
    Map confidence to a stake multiplier. Never zero, never skip:
        confidence 0                      → 0.01
        confidence == full_stake          → 1.00
        confidence  > full_stake          → 1.00 (capped)
    """
    if full_stake <= 0:
        return 1.0
    raw = 0.01 + (confidence / full_stake) * 0.99
    return min(1.0, max(0.0, raw))


def _skip(
    cp: float, cf: float, reason: str,
    *,
    side: str = "",
    entry: float = 0.0,
    p_win: Optional[float] = None,
) -> SizingDecision:
    return SizingDecision(
        side=side, entry_price=entry, ev=0.0,
        p_win=p_win if p_win is not None else cp,
        confidence=cf, stake_usd=0.0, shares=0.0,
        skip_reason=reason,
    )


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _clamp_price(x: float) -> float:
    return max(1e-6, min(1.0, float(x)))
