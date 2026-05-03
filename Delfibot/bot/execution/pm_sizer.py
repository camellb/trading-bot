"""
Polymarket position sizer - V1 doctrine: follow the market, use the
forecast as a filter.

Locked 2026-04-27. Replaces the V0 "two gates + confidence softener"
sizer after a 250-trade simulation backtest showed V0 lost 3.53% ROI
while a market-default baseline made 4.89%. Full evidence:
`memory/doctrine_back_the_forecast.md`.

Side selection
    Side is the market favourite, period. The forecaster does NOT pick
    the side. If `ask_yes >= 0.50` we buy YES at `ask_yes`; otherwise we
    buy NO at `ask_no` (which equals `1 - ask_yes` to within Polymarket's
    rounding).

Gate (single, only skip path)
    Delfi direction agreement: `claude_p` and `market_p_yes` must lie on
    the same side of 0.50. If they don't, skip. That is the entire skip
    logic in V1.

Sizing
    Flat. `stake = bankroll * base_stake_pct * archetype_multiplier`.
    Confidence is no longer a sizing input - on the V0 sample, high-
    confidence Delfi picks won 52.9% while low-confidence picks won
    67.6%, so the V0 softener was loading more dollars onto worse
    trades.

Archetype overrides (preserved from V0)
    archetype_skip_list: hard skip; the trade never opens.
    archetype_stake_multipliers[archetype]: multiplied into the stake,
    clamped [0.1, 10.0]. 1.0 (or missing key) = no adjustment.

Final stake = bankroll * base_stake_pct * archetype_mult, capped at
bankroll * max_stake_pct, with an absolute $2 minimum (same as V0).

What was deliberately removed
    - min_p_win gate (the market favourite is by definition >= 0.50, so a
      0.55 floor would just clip the 0.50-0.55 band, which was profitable
      on the backtest at +15.2% ROI).
    - Confidence softener (anti-correlated with hit rate on the V0
      sample).
    - High-confidence override and mean rule (V0's two-mode side
      selector). The market picks the side now.
    - Gate 3 (min expected return) was removed in V0 and stays removed
      under V1.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


# Absolute floor for a tradeable stake in USD.
_MIN_ABSOLUTE_STAKE_USD = 2.0

# Cost assumption: spread + fees + slippage, as a fraction of the payoff.
# Currently unused by the V1 sizer (no expected-return gate). Retained
# because `engine/diagnostics.py` reads it as a back-compat baseline for
# implied-cost analyses.
COST_ASSUMPTION = 0.015


@dataclass
class SizingDecision:
    side:         str             # 'YES' | 'NO' | '' (only on pre-side failure)
    entry_price:  float           # ask price of the chosen side (0..1)
    ev:           float           # retained for schema compatibility; always 0.0
    p_win:        float           # market_p on the chosen side (0..1)
    confidence:   float           # Delfi confidence (0..1) - logged, not used in sizing
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
    V1 sizer. Side = market favourite. Single gate = Delfi direction
    agreement. Flat archetype-multiplied sizing. Returns a SizingDecision
    whose `skip_reason` is None iff the bet is taken.
    """
    cp  = _clamp01(claude_p)
    cf  = _clamp01(confidence)
    ay  = _clamp_price(ask_yes)
    an  = _clamp_price(ask_no)

    # ── Hard skip: archetype on the user's skip list ────────────────────────
    archetype_skip = tuple(getattr(user_config, "archetype_skip_list", ()) or ())
    if archetype is not None and archetype in archetype_skip:
        return _skip(cp, cf, f"category '{archetype}' is on the skip list")

    # ── Side selection: the market favourite ────────────────────────────────
    market_p_yes = ay
    if market_p_yes >= 0.50:
        side, entry, p_win = "YES", ay, market_p_yes
    else:
        side, entry, p_win = "NO", an, 1.0 - market_p_yes

    # ── Gate: Delfi direction agreement ─────────────────────────────────────
    # The forecaster's only job in V1 is to veto trades where it disagrees
    # with the market's pick. Both probabilities must land on the same side
    # of 0.50.
    if (cp - 0.50) * (market_p_yes - 0.50) < 0:
        return _skip(
            cp, cf,
            f"Delfi disagrees with the market "
            f"(claude_p={cp:.2f}, market_p_yes={market_p_yes:.2f})",
            side=side, entry=entry, p_win=p_win,
        )

    # ── Gate: disabled market-price bands ───────────────────────────────────
    # Per-user list of (lo, hi) bands in market_price_yes space; the
    # bot skips any market whose price falls in any band. Replaces the
    # V0 single-floor `min_market_favourite_price` gate. Empty list =
    # no bands disabled. Bands are matched on raw market_price_yes
    # (not favourite price) so the user can disable e.g. just YES
    # extreme favourites without also blocking the symmetric NO side.
    skip_bands = getattr(user_config, "skip_market_price_bands", ()) or ()
    for lo, hi in skip_bands:
        # Half-open interval [lo, hi). User-defined bands always have
        # hi <= 1.0; a market_price_yes of exactly 1.0 (only possible
        # post-resolution, never at entry) is allowed through.
        if float(lo) <= market_p_yes < float(hi):
            return _skip(
                cp, cf,
                f"market price {market_p_yes:.2f} inside disabled "
                f"{float(lo):.2f}-{float(hi):.2f} band",
                side=side, entry=entry, p_win=p_win,
            )

    # Entry-price sanity - can't compute shares without a positive ask.
    if entry <= 0:
        return _skip(
            cp, cf, f"non-positive entry price ({entry})",
            side=side, entry=entry, p_win=p_win,
        )

    # ── Archetype stake multiplier ──────────────────────────────────────────
    # User-set multiplier per archetype, clamped [0.1, 10.0]. Missing key
    # or no archetype → 1.0 (no adjustment).
    arch_mult = 1.0
    if archetype is not None:
        multipliers = getattr(user_config, "archetype_stake_multipliers", None) or {}
        try:
            arch_mult = float(multipliers.get(archetype, 1.0))
        except (TypeError, ValueError):
            arch_mult = 1.0
        arch_mult = max(0.1, min(10.0, arch_mult))

    # ── Stake sizing (flat) ─────────────────────────────────────────────────
    base_pct = float(user_config.base_stake_pct)
    max_pct  = float(user_config.max_stake_pct)

    stake_pct = min(base_pct * arch_mult, max_pct)

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
