"""
Polymarket position sizer — positive-EV with flat, confidence-scaled stakes.

For every market, the sizer estimates expected value on each side at its
current ask and takes the better side when it clears the user's minimum EV
threshold after costs:

    EV_side = p_win × (1 / ask_price) − 1 − cost_assumption

Stake scales with Claude's confidence only. Not with EV, not with edge,
not with disagreement. Flat sizing keeps variance per trade low so the
portfolio learns fast about what actually works.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

# Cost model — Polymarket spread + fees + slippage, conservative.
COST_ASSUMPTION = 0.015

# Genuinely uncertain markets are skipped — no information, no bet.
_UNCERTAIN_LO = 0.45
_UNCERTAIN_HI = 0.55

# Floor for an absolute tradeable stake in USD.
_MIN_ABSOLUTE_STAKE_USD = 2.0

# EV-bucket labels used for the skip-list check. Must match the labels
# emitted by `engine.diagnostics.roi_by_ev_bucket` and the dashboard.
_EV_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<0%",    -float("inf"), 0.0),
    ("0-2%",   0.0,  0.02),
    ("2-5%",   0.02, 0.05),
    ("5-10%",  0.05, 0.10),
    ("10-20%", 0.10, 0.20),
    ("20%+",   0.20, float("inf")),
)


def _ev_bucket_label(ev: float) -> Optional[str]:
    """Return the bucket label containing the given EV fraction, or None."""
    for label, lo, hi in _EV_BUCKETS:
        if lo <= ev < hi:
            return label
    return None


@dataclass
class SizingDecision:
    side:         str             # 'YES' | 'NO' | '' (when skipped pre-sidechoice)
    entry_price:  float           # ask price of the chosen side (0..1)
    ev:           float           # expected value at chosen side (fractional, e.g. 0.08 = +8%)
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
    Decide whether to take YES or NO (or skip), and size the stake.

    Returns a SizingDecision. Check .should_trade before acting; .skip_reason
    explains the skip for logging / dashboard.
    """
    cp  = _clamp01(claude_p)
    cf  = _clamp01(confidence)
    ay  = _clamp_price(ask_yes)
    an  = _clamp_price(ask_no)

    # Archetype skip-list: the learning cadence has flagged this category as
    # mis-forecast. Refuse to trade it regardless of EV.
    archetype_skip = tuple(getattr(user_config, "archetype_skip_list", ()) or ())
    if archetype is not None and archetype in archetype_skip:
        return SizingDecision(
            side="", entry_price=0.0, ev=0.0, p_win=cp,
            confidence=cf, stake_usd=0.0, shares=0.0,
            skip_reason=f"archetype '{archetype}' is on the skip list",
        )

    # Probability cap: when calibration shows overconfidence at high p, the
    # learning cadence may cap emitted probabilities. Apply symmetrically on
    # both tails so sizing doesn't amplify the observed overshoot.
    cap = getattr(user_config, "probability_cap", None)
    if cap is not None:
        cap = float(cap)
        cp = min(max(cp, 1.0 - cap), cap)

    # Skip genuinely uncertain markets — no defensible side. Note: cap can
    # collapse a high-p call into the uncertain band; that's intentional.
    if _UNCERTAIN_LO < cp < _UNCERTAIN_HI:
        return SizingDecision(
            side="", entry_price=0.0, ev=0.0, p_win=cp,
            confidence=cf, stake_usd=0.0, shares=0.0,
            skip_reason=f"uncertain estimate {cp:.2f} in "
                        f"[{_UNCERTAIN_LO:.2f}, {_UNCERTAIN_HI:.2f}]",
        )

    # Cost assumption: user may override the sizer default when realised cost
    # has drifted above assumed (diagnostic-driven proposal).
    cost_override = getattr(user_config, "cost_assumption_override", None)
    cost_assumption = float(cost_override) if cost_override is not None else COST_ASSUMPTION

    # Compute EV for each side.
    ev_yes = cp * (1.0 / ay) - 1.0 - cost_assumption
    ev_no  = (1.0 - cp) * (1.0 / an) - 1.0 - cost_assumption

    if ev_yes >= ev_no:
        side, entry, ev, p_win = "YES", ay, ev_yes, cp
    else:
        side, entry, ev, p_win = "NO",  an, ev_no,  1.0 - cp

    # Does the better side clear the user's minimum EV threshold?
    min_ev = float(user_config.min_ev_threshold)
    if ev < min_ev:
        return SizingDecision(
            side=side, entry_price=entry, ev=ev, p_win=p_win,
            confidence=cf, stake_usd=0.0, shares=0.0,
            skip_reason=(f"ev {ev*100:.2f}% < min {min_ev*100:.2f}% "
                         f"(YES {ev_yes*100:+.2f}%, NO {ev_no*100:+.2f}%)"),
        )

    # EV-bucket skip-list: even above the min threshold, the user may have
    # disabled specific EV buckets whose realised ROI is persistently negative.
    bucket_skip = tuple(getattr(user_config, "ev_bucket_skip_list", ()) or ())
    bucket = _ev_bucket_label(ev)
    if bucket is not None and bucket in bucket_skip:
        return SizingDecision(
            side=side, entry_price=entry, ev=ev, p_win=p_win,
            confidence=cf, stake_usd=0.0, shares=0.0,
            skip_reason=f"ev bucket '{bucket}' is on the skip list",
        )

    # Flat, confidence-scaled stake.
    base_pct = float(user_config.base_stake_pct)
    max_pct  = float(user_config.max_stake_pct)

    if cf >= 0.8:
        stake_pct = min(base_pct * 1.5, max_pct)
    elif cf >= 0.5:
        stake_pct = base_pct
    else:
        stake_pct = base_pct * 0.5

    # Hard cap at max_stake_pct regardless of the branch above.
    stake_pct = min(stake_pct, max_pct)

    stake = max(
        _MIN_ABSOLUTE_STAKE_USD,
        min(bankroll * stake_pct, bankroll * max_pct),
    )

    if bankroll <= 0 or stake <= 0:
        return SizingDecision(
            side=side, entry_price=entry, ev=ev, p_win=p_win,
            confidence=cf, stake_usd=0.0, shares=0.0,
            skip_reason=f"non-positive stake (bankroll=${bankroll:.2f})",
        )

    shares = stake / entry if entry > 0 else 0.0

    return SizingDecision(
        side=side, entry_price=entry, ev=ev, p_win=p_win,
        confidence=cf, stake_usd=stake, shares=shares,
        skip_reason=None,
    )


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _clamp_price(x: float) -> float:
    # Ask prices must be strictly positive for 1/ask. Cap to 1.0 upper.
    return max(1e-6, min(1.0, float(x)))
