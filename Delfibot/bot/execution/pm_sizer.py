"""
Polymarket position sizer - V2 doctrine: ALWAYS follow the market
favourite. The forecaster informs sizing per-archetype over time but
does NOT gate entry.

V2 locked 2026-05-30. Replaces V1 ("follow the market, use the
forecast as a filter") after the Versus Market analysis showed the
V1 disagreement veto cost +$80 across 115 settled disagreement bets
on the user's live install ($3 synthetic stake). Strategy comparison
across 381 settled live evaluations:
    A. V1 status quo (skip on Delfi disagreement):   +$10.52
    B. V2 (drop veto, keep archetype/budget gates):  +$91.11
    C. Follow market on every settled eval:         +$104.47
    D. Back the forecast (V0, rejected 2026-04-27): -$236.52
V2 = strategy B. Adds the 115 forecast-vetoed bets, keeps every other
guard intact.

Why not back the forecast? It's still anti-signal on disagreements
(-$76.18 at $1 notional). V0 stays rejected. The forecaster has signal
in aggregate calibration, just not enough to override the market on
any specific trade. Per-archetype multipliers continue to encode the
forecaster's archetype-level signal.

Side selection
    Side is the market favourite, period. If `ask_yes >= 0.50` we buy
    YES at `ask_yes`; otherwise we buy NO at `ask_no` (which equals
    `1 - ask_yes` to within Polymarket's rounding).

Gates that remain
    - archetype_skip_list: hard skip per user config (default skips
      sports_other, hockey, cricket).
    - archetype price bands: per-archetype `(lo, hi)` bands in
      market_p_yes space; skip if the favourite price is inside any
      band.
    - max_stake_pct cap (when max_stake_pct_enabled = True).
    - Polymarket platform minimum: max($1, 5 * price).

Sizing
    Flat. `stake = bankroll * base_stake_pct * archetype_multiplier *
                   volume_tier_multiplier`.
    Confidence is not a sizing input - on the V0 sample, high-
    confidence Delfi picks won 52.9% while low-confidence picks won
    67.6%, so the softener was loading more dollars onto worse trades.

Archetype overrides (preserved from V0 and V1)
    archetype_skip_list: hard skip; the trade never opens.
    archetype_stake_multipliers[archetype]: multiplied into the stake,
    clamped [0.1, 10.0]. 1.0 (or missing key) = no adjustment.

What was deliberately removed
    V2 dropped:
    - Delfi direction-agreement veto. See in-line comment at the
      ex-gate site for the audit-trail evidence.
    V1 dropped (preserved here):
    - min_p_win gate, confidence softener, high-confidence override,
      mean rule, min-expected-return gate.
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


def _classify_volume_tier(volume_usd: Optional[float]) -> str:
    """Map a 24h CLOB volume number to a stake-multiplier bucket.

    Thresholds match `engine.user_config.VOLUME_TIER_LOW_THRESHOLD`
    / `VOLUME_TIER_HIGH_THRESHOLD`. None / missing volume falls to
    "mid" (neutral 1.0x default) - we don't want a stale-data row
    to silently get the low-multiplier penalty.
    """
    if volume_usd is None:
        return "mid"
    try:
        v = float(volume_usd)
    except (TypeError, ValueError):
        return "mid"
    if v < 1_000.0:
        return "low"
    if v < 10_000.0:
        return "mid"
    return "high"


def size_position(
    delfi_p:    float,
    confidence:  float,
    ask_yes:     float,
    ask_no:      float,
    bankroll:    float,
    user_config,
    archetype:   Optional[str] = None,
    volume_usd:  Optional[float] = None,
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

    # ── V1 direction-agreement gate: REMOVED in V2 doctrine (2026-05-30) ────
    # V1 doctrine vetoed trades when delfi_p and market_p_yes landed on
    # opposite sides of 0.50. The Versus Market analysis on the user's live
    # install showed that veto was costing money: 115 settled disagreements
    # the V1 filter declined would have netted +$26.86 at $1 notional per
    # bet (and +$80.59 at the bot's actual $3 stake) had we just taken the
    # market favourite. The forecaster IS anti-signal on disagreements
    # (backing it lost more), but the right reaction is to ignore the
    # forecaster on those rows, not to skip the market. V2 doctrine:
    # "always follow the market favourite; the forecaster does not gate
    # entry." force_skip from polymarket_evaluator (research-mismatch case)
    # still applies as an independent safety. See CLAUDE.md "Settled
    # lessons" + v1.5.32 commit.

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

    # ── Volume-tier stake multiplier ────────────────────────────────────────
    # Polymarket's Brier-vs-volume chart (2026-05-28 research) shows
    # higher-volume markets are more accurate. We absorb that as a
    # stake dial: tilt slightly toward high-volume markets and away
    # from thin ones. Default tier multipliers: low=0.8, mid=1.0,
    # high=1.1 (set in V1_DEFAULT_VOLUME_TIER_MULTIPLIERS).
    #
    # `volume_usd` is the market's 24h CLOB volume in USD, passed by
    # pm_analyst from `market.volume_24h_clob`. None falls back to
    # the "mid" bucket so a missing-volume row doesn't get the low
    # penalty.
    from engine.user_config import V1_DEFAULT_VOLUME_TIER_MULTIPLIERS
    vol_tier = _classify_volume_tier(volume_usd)
    vol_mults = getattr(user_config, "volume_tier_multipliers", None) or {}
    default_for_tier = V1_DEFAULT_VOLUME_TIER_MULTIPLIERS.get(vol_tier, 1.0)
    try:
        vol_mult = float(vol_mults.get(vol_tier, default_for_tier))
    except (TypeError, ValueError):
        vol_mult = default_for_tier
    vol_mult = max(0.1, min(10.0, vol_mult))

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

    stake_pct = base_pct * arch_mult * vol_mult
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
            # Same wallet ceiling as the cap-disabled branch: the $1
            # bump must not submit an order the bankroll can't fund
            # (the executor's wallet pre-check would shrink it back
            # below the platform minimum and the CLOB would reject in
            # a loop instead of a clean skip).
            if stake > bankroll:
                return _skip(
                    cp, cf,
                    f"platform minimum ${stake:.2f} exceeds bankroll "
                    f"${bankroll:.2f}. Deposit more or wait for a "
                    f"lower-priced market.",
                    side=side, entry=entry, p_win=p_win,
                )
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
