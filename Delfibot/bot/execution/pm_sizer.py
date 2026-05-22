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
    Delfi direction agreement: `delfi_p` and `market_p_yes` must lie on
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


# Absolute floor for a tradeable stake in USD. The sizer no longer
# enforces this — the user's configured base_stake_pct + max_stake_pct
# are the authoritative risk controls. The constant is kept (set to 0
# so it's a no-op) only for back-compat with `engine/diagnostics.py`
# and any other reader that imports it. Removing it entirely would
# break that import; setting it to 0 makes every `max(0, x)` collapse
# to `x` and every `cap < 0` guard fall through.
#
# 2026-05-17: dropped the previous $2 hard floor after small-bankroll
# users (~$8 live capital with 2.4% max_stake_pct = $0.19 cap) saw
# every trade SKIPPED with "max stake cap $0.19 below $2 floor". User
# instruction was explicit: "Sizer should just follow the risk
# controls settings".
_MIN_ABSOLUTE_STAKE_USD = 0.0

# Polymarket V2 CLOB platform minimums for marketable BUY orders. Both
# floors are HARD PLATFORM CONSTRAINTS — caught live 2026-05-17:
#   {'error': 'invalid amount for a marketable BUY order ($0.17), min size: $1'}
#   {'error': 'order ... is invalid. Size (1.73) lower than the minimum: 5'}
# The effective minimum stake is therefore max($1, 5 * price). At a
# $0.50 ask that's $2.50; at $0.99 that's $4.95. Polymarket refuses
# anything below this. Applies to live orders only; simulation has
# no equivalent floor.
POLYMARKET_MIN_ORDER_USD = 1.0
POLYMARKET_MIN_SHARES    = 5.0

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
    delfi_p:    float,
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
    cp  = _clamp01(delfi_p)
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
            f"(delfi_p={cp:.2f}, market_p_yes={market_p_yes:.2f})",
            side=side, entry=entry, p_win=p_win,
        )

    # ─────────────────────────────────────────────────────────────────
    # GATE: per-archetype disabled market-price bands
    # ─────────────────────────────────────────────────────────────────
    # INPUT  : market_p_yes (= ay above), range [0, 1]
    # AXIS   : RAW market_p_yes - NOT favourite price.
    #          favourite price = max(p, 1-p), range [0.5, 1] - DIFFERENT.
    # BANDS  : list of (lo, hi) pairs in raw market_p_yes space, per
    #          archetype. Half-open intervals [lo, hi).
    # ACTION : skip if ANY band on the trade's archetype contains
    #          market_p_yes.
    #
    # Why raw, not favourite: the user wants asymmetric control. Disabling
    # 90-100 (YES extremes) without 0-10 (NO extremes) is meaningful;
    # bucketing on favourite price would force them symmetric.
    #
    # Concrete check for archetype tennis with bands [[0.40, 0.60]]:
    #   market_p_yes = 0.30 (NO favourite, fav=0.70) -> ALLOWED
    #   market_p_yes = 0.55 (weak YES,    fav=0.55) -> SKIPPED
    #   market_p_yes = 0.85 (strong YES,  fav=0.85) -> ALLOWED
    #
    # Replaces the V0 `min_market_favourite_price` floor and the brief
    # intermediate global `skip_market_price_bands`. No global filter
    # under V1 - per-archetype only.
    arch_band_map = (
        getattr(user_config, "archetype_skip_market_price_bands", None) or {}
    )
    arch_bands = arch_band_map.get(archetype, ()) if archetype else ()
    for lo, hi in arch_bands:
        # Half-open interval [lo, hi). User-defined bands always have
        # hi <= 1.0; a market_price_yes of exactly 1.0 (only possible
        # post-resolution, never at entry) is allowed through.
        if float(lo) <= market_p_yes < float(hi):
            return _skip(
                cp, cf,
                f"market price {market_p_yes:.2f} inside disabled "
                f"{float(lo):.2f}-{float(hi):.2f} band on "
                f"{archetype}",
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
    # falls back to the V1 doctrine defaults (basketball 1.5, tennis 0.5,
    # everything else 1.0) so the sizer matches what the Risk Control UI
    # advertises. Earlier versions fell back to 1.0 unconditionally;
    # legacy installs whose `archetype_stake_multipliers` was never
    # seeded (e.g. tour completed before 2026-04-27 V1 lock) saw the UI
    # showing "tennis 0.5x / basketball 1.5x" but actually traded at
    # 1.0x. Fix: import the V1 defaults so sizer and UI agree.
    arch_mult = 1.0
    if archetype is not None:
        from engine.user_config import V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS
        multipliers = getattr(user_config, "archetype_stake_multipliers", None) or {}
        default_for_arch = V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS.get(archetype, 1.0)
        try:
            arch_mult = float(multipliers.get(archetype, default_for_arch))
        except (TypeError, ValueError):
            arch_mult = default_for_arch
        arch_mult = max(0.1, min(10.0, arch_mult))

    # ── Stake sizing (flat) ─────────────────────────────────────────────────
    base_pct = float(user_config.base_stake_pct)
    max_pct  = float(user_config.max_stake_pct)
    # User instruction 2026-05-18: max_stake_pct is OPT-IN for users at
    # $1000+ bankrolls who want a hard cap; it's OFF by default so
    # small-bankroll users (whose configured cap would sit below
    # Polymarket's $1-and-5-share platform floor) can still trade.
    # When disabled, the sizer uses `base_pct * arch_mult` as the
    # target and bumps up to the platform minimum (max($1, 5 * ask))
    # when the target falls short — even if that exceeds the
    # configured cap.
    cap_enabled = bool(getattr(user_config, "max_stake_pct_enabled", False))

    stake_pct = base_pct * arch_mult
    if cap_enabled:
        stake_pct = min(stake_pct, max_pct)

    if bankroll <= 0:
        return _skip(
            cp, cf, f"non-positive bankroll (${bankroll:.2f})",
            side=side, entry=entry, p_win=p_win,
        )

    target_stake = bankroll * stake_pct
    if cap_enabled:
        cap = bankroll * max_pct
        if cap < _MIN_ABSOLUTE_STAKE_USD:
            return _skip(
                cp, cf,
                f"max stake cap ${cap:.2f} below "
                f"${_MIN_ABSOLUTE_STAKE_USD:.2f} floor (bankroll "
                f"${bankroll:.2f} * max_stake_pct {max_pct*100:.1f}%)",
                side=side, entry=entry, p_win=p_win,
            )
        target_stake = min(target_stake, cap)
    stake = max(_MIN_ABSOLUTE_STAKE_USD, target_stake)

    # Polymarket V2 platform floors: BOTH $1 notional AND 5 shares.
    # Branch on the cap toggle:
    #
    #  cap ENABLED: respect max_stake_pct. Bump to $1 only (per the
    #     2026-05-17 user rule); if 5 shares > stake at this ask,
    #     SKIP with a message that points at the cap setting.
    #
    #  cap DISABLED (default): bump the order to whatever Polymarket
    #     actually accepts (max($1, 5 * ask)) so the bot keeps
    #     trading at small bankrolls. Only skip when the bumped
    #     stake would exceed the user's WALLET, not the cap.
    is_live = getattr(user_config, "mode", None) == "live"
    if is_live:
        ask = float(entry) if float(entry) > 0 else 0.0
        platform_min = max(
            POLYMARKET_MIN_ORDER_USD,
            POLYMARKET_MIN_SHARES * ask,
        )

        if cap_enabled:
            if stake < POLYMARKET_MIN_ORDER_USD:
                stake = POLYMARKET_MIN_ORDER_USD
            shares_at_stake = (stake / ask) if ask > 0 else 0.0
            if shares_at_stake < POLYMARKET_MIN_SHARES:
                return _skip(
                    cp, cf,
                    f"max_stake_pct cap blocks this trade. Polymarket "
                    f"needs {POLYMARKET_MIN_SHARES:.0f} shares "
                    f"(~${POLYMARKET_MIN_SHARES * ask:.2f}) at this "
                    f"${ask:.2f} ask, but the cap allows only "
                    f"${stake:.2f}. Disable the cap in Risk settings "
                    f"or raise it above "
                    f"{(POLYMARKET_MIN_SHARES * ask / bankroll) * 100:.1f}% "
                    f"to trade this market.",
                    side=side, entry=entry, p_win=p_win,
                )
        else:
            if stake < platform_min:
                stake = platform_min
            if stake > bankroll:
                return _skip(
                    cp, cf,
                    f"platform minimum ${platform_min:.2f} exceeds "
                    f"bankroll ${bankroll:.2f}. Deposit more or wait "
                    f"for a lower-priced market.",
                    side=side, entry=entry, p_win=p_win,
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
