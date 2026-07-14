"""
Local single-user configuration.

The Delfi desktop app runs as one user on one machine. Most multi-tenant
plumbing from the SaaS codebase is gone: there is no per-user user_id
plumbing here. `DEFAULT_USER_ID = "local"` exists only so the dozens of
engine modules that still pass `user_id=...` keep working without a
666-call refactor.

Secrets live in the OS keychain via `keyring`, never in the SQLite file:

    keyring service: 'delfi'
    keys:            'polymarket_private_key', 'anthropic_api_key'

The wallet address itself is public so it stays in the user_config row.

Public surface preserved for legacy callers: DEFAULT_USER_ID, UserConfig,
USER_CONFIG_BOUNDS, USER_CONFIG_LIST_FIELDS, USER_CONFIG_DICT_FIELDS,
USER_CONFIG_BOOL_DICT_FIELDS, USER_CONFIG_NULLABLE_FIELDS,
NOTIFICATION_CATEGORIES, ARCHETYPE_MULTIPLIER_BOUNDS,
USER_CONFIG_DESCRIPTIONS, cast_value, validate_user_config_value,
validated_update_payload, ensure_default_user_config, get_user_config,
update_user_config, should_notify, is_admin, complete_user_onboarding,
list_onboarded_user_ids, get_default_user_config, get_user_join_time,
get_active_polymarket_creds, get_user_polymarket_creds,
set_user_polymarket_creds.

Telegram helpers were removed entirely - notifications now flow through
the SQLite event_log table the dashboard reads. Polymarket-US (QCEX) is
not supported; v1 is offshore-only.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple, Union

# Leaf module (stdlib-only deps); safe to import at module load even
# while the `engine` package is mid-initialisation. Holds the provider
# catalogue + connection validation/normalisation used by the LLM
# connections store below.
from engine import llm_providers as _providers


DEFAULT_USER_ID = "local"

KEYRING_SERVICE = "delfi"
KEYRING_POLYMARKET_KEY = "polymarket_private_key"
# `anthropic_api_key` is the historical name; the UI now calls this the
# "LLM API key" since support for other providers is on the roadmap. The
# keychain entry name is preserved so existing installs don't lose their
# stored key on upgrade.
KEYRING_ANTHROPIC_KEY = "anthropic_api_key"           # primary LLM
KEYRING_LLM_BACKUP_KEY = "llm_backup_api_key"          # optional secondary
KEYRING_NEWSAPI_KEY = "newsapi_key"                    # optional, news headlines
KEYRING_CRYPTOPANIC_KEY = "cryptopanic_api_key"        # optional, crypto news
KEYRING_LICENSE_KEY = "license_key"                    # Lemon Squeezy license
KEYRING_LICENSE_META = "license_meta"                  # JSON: status + last_validated_at + instance_id
KEYRING_TELEGRAM_TOKEN = "telegram_bot_token"          # @BotFather token for outbound notifications

# Optional secondary LLM (Google Gemini) used by research/fetcher.py
# for fast keyword extraction and by feeds/news_feed.py for headline
# pre-filtering. Bot logs an explicit "GEMINI_API_KEY not set"
# warning every scan when missing.
KEYRING_GEMINI_KEY = "gemini_api_key"

# Optional MANUAL Polymarket CLOB api credentials. The SDK normally
# auto-derives these from the user's private key via
# create_or_derive_api_key(). But after V2 migration that auto-flow
# can return a stale key bound to the wrong (signer, funder) context,
# and orders get rejected with "the order signer address has to be
# the address of the API KEY". The user can bypass that by
# generating a fresh key via the Polymarket UI
# (Settings > Relayer API keys) and pasting all three values here.
# pm_executor / polymarket_wallet check for these first; if all
# three are set, the SDK is constructed with them directly instead
# of calling create_or_derive_api_key. Absent → auto-derive path
# (the legacy behavior).
KEYRING_POLYMARKET_API_KEY        = "polymarket_api_key"
KEYRING_POLYMARKET_API_SECRET     = "polymarket_api_secret"
KEYRING_POLYMARKET_API_PASSPHRASE = "polymarket_api_passphrase"

# ── Polymarket Relayer API Key ──────────────────────────────────────────────
# A SEPARATE key class from the Builder API tuple above. The user creates
# it on polymarket.com -> Settings -> Relayer API keys and pastes the
# single UUID into Delfi. Auth is just 2 headers (RELAYER_API_KEY +
# RELAYER_API_KEY_ADDRESS); no HMAC, no timestamp, no passphrase.
#
# That's enough for the Polymarket relayer at relayer-v2.polymarket.com
# to accept gasless DepositWallet batch redemptions. Verified 2026-05-18
# against position 317's real redeem (tx 0x10bb58d78f2c...).
KEYRING_POLYMARKET_RELAYER_API_KEY = "polymarket_relayer_api_key"


@dataclass
class UserConfig:
    # V1 sizer (locked 2026-04-27): side = market favourite, single
    # Delfi-disagreement skip gate, flat archetype-multiplied stake. The
    # V0 fields min_p_win / confidence_full_stake /
    # confidence_override_threshold were removed when V1 shipped.
    base_stake_pct:         float = 0.02
    max_stake_pct:          float = 0.05
    # When False (default), the sizer treats max_stake_pct as advisory
    # only and BUMPS the per-trade stake up to Polymarket's platform
    # minimum (max($1, 5 * ask)) when bankroll * base_stake_pct comes
    # in below it. This is what lets small live accounts (<$50) keep
    # trading despite Polymarket's $2.50-$4.75 per-order floor. When
    # True, the cap is enforced and the sizer SKIPS markets it can't
    # fund within bankroll * max_stake_pct. User instruction 2026-05-18.
    max_stake_pct_enabled:  bool  = False

    # Circuit breakers.
    daily_loss_limit_pct:   float = 0.10
    weekly_loss_limit_pct:  float = 0.20
    drawdown_halt_pct:      float = 0.40
    streak_cooldown_losses: int   = 3
    dry_powder_reserve_pct: float = 0.20

    # ── Exit policy (early close before natural settlement) ───────────────
    # Master switch. When False, none of the take-profit / stop-loss /
    # time-decay rules below are evaluated and positions only close on
    # natural settlement. Default OFF so existing users see no behavior
    # change until they opt in.
    exit_policy_enabled: bool = False
    # Take-profit: close the position when unrealized return >= threshold.
    # Computed against the CURRENT BID (the price we could actually sell at),
    # not the mid or last trade, so slippage doesn't trigger false exits.
    take_profit_enabled:   bool  = True
    take_profit_threshold_pct: float = 0.50   # +50% locks the win
    # Stop-loss: close when unrealized return <= -threshold. Gated by the
    # min-time-remaining rule below so a wick near resolution doesn't
    # cut the position right before it would have recovered.
    stop_loss_enabled:   bool  = True
    stop_loss_threshold_pct: float = 0.30     # -30% caps the loss
    # Don't trigger stop-loss if less than this fraction of the original
    # time-to-resolution is still left. Protects against fast-moving
    # markets in their last minutes; if you're 90% of the way to
    # settlement at -30% you've already paid for the wait, hold to learn.
    stop_loss_min_time_remaining_pct: float = 0.20
    # Time-decay: close positions that have been open too long without
    # moving — frees capital from stalled markets. Only fires when the
    # unrealized return is inside the flat band (don't kick a winner out
    # just because it's been open a while). Off by default since most
    # users care more about TP/SL than capital velocity.
    time_decay_enabled:    bool  = False
    # 120h (5 days) tuned to the bot's PM_MAX_DAYS_TO_END=7 horizon.
    # An earlier default of 72h was firing on multi-day markets that
    # were not actually stalled — three days into a seven-day market
    # is normal, not stale.
    time_decay_max_hours:  int   = 120
    # ±5% is the genuine "flat" band. ±10% (the prior default) caught
    # small winners and small losers that still had direction;
    # time-decay should only fire when the market truly hasn't moved.
    time_decay_flat_band_pct: float = 0.05
    # Universal safety: never exit if less than N minutes remain to the
    # market's natural resolution. Polymarket spread + per-trade fees
    # on a market this close to settlement reliably exceed the
    # time-value gain of selling early. 15 min gives the position room
    # to ride out final-minute noise (5 min was too tight; in practice
    # spreads widen and liquidity thins inside that window).
    exit_min_time_to_resolution_minutes: int = 15

    # Diagnostic-driven overrides.
    cost_assumption_override: Optional[float]   = None
    archetype_skip_list:      Tuple[str, ...]   = field(default_factory=tuple)
    archetype_stake_multipliers: Dict[str, float] = field(default_factory=dict)
    # Per-market-volume stake multiplier. Three buckets keyed on
    # market.volume_24h_clob: low (<$1k), mid ($1k-$10k), high (>=$10k).
    # Empty dict falls back to V1_DEFAULT_VOLUME_TIER_MULTIPLIERS at
    # sizer-read time. Added v1.5.21; the missing field declaration
    # was the v1.5.21-22 bug that made get_user_config raise on every
    # call and silently break onboarding state (caught 2026-05-28).
    volume_tier_multipliers: Dict[str, float] = field(default_factory=dict)

    # Per-user time-to-resolution filter (DAYS). Days match the
    # day-based "By horizon" buckets on the Performance page so
    # the user thinks in one unit. None = no constraint. Frontend
    # exposes 0 as the null sentinel (the user types 0 to clear
    # the limit). When both are set the validator enforces
    # max >= min.
    min_days_to_resolution:  Optional[int]     = None
    max_days_to_resolution:  Optional[int]     = None

    # Disabled market-price bands. Each element is a (lo, hi) pair in
    # market_price_yes space (0..1). The sizer skips any market whose
    # `market_price_yes` falls into any band. Empty tuple = no bands
    # disabled (default).
    #
    # The UI exposes 10 10pp toggles (0-10, 10-20, ..., 90-100), so
    # each disabled bucket adds one (lo, hi) pair like (0.40, 0.50).
    # The schema accepts arbitrary bands so a finer UI could go to 5pp
    # or arbitrary cuts later without a schema change.
    #
    # Supersedes the V0 single-floor `min_market_favourite_price`
    # field (still in the DB schema for back-compat, no longer read).
    # The 2026-05-03 audit's 0.55-0.60 underperformance is expressible
    # here as the bucket pair (0.40, 0.50) and (0.50, 0.60).
    # Per-archetype price-band overrides. Maps archetype id -> tuple
    # of (lo, hi) pairs in market_price_yes space. The sizer skips a
    # market when its market_price_yes falls inside any band on its
    # archetype's list. Empty dict = no skips.
    #
    # UI in the Risk page archetype matrix shows a per-card collapsible
    # 10-pill row so the user can decide e.g. "skip 90-100 only on
    # tennis" without touching anything else. The matching DB column
    # `archetype_skip_market_price_bands` stores the JSON-encoded dict.
    #
    # The legacy global `skip_market_price_bands` field is gone (locked
    # 2026-05-03). The DB column stays in the schema for back-compat
    # but is no longer read. ensure_default_user_config copies any
    # existing global bands to every archetype on first boot after
    # the upgrade.
    archetype_skip_market_price_bands: Dict[str, Tuple[Tuple[float, float], ...]] = field(default_factory=dict)

    # Execution state.
    mode:                  Optional[str]   = None    # 'simulation' | 'live'
    starting_cash:         Optional[float] = None
    wallet_address:        Optional[str]   = None
    bot_enabled:           bool            = False

    # Set the moment the user clicks "Done" on the last step of the
    # Onboarding wizard. Used by `is_onboarded` to decide whether to
    # show the wizard on launch. Note: mode + starting_cash both have
    # DB server defaults ('simulation' / 1000.0), so checking those
    # for "has the user been through onboarding" returns True the
    # instant the row is inserted - it's not a real signal. This
    # field is the explicit one.
    tour_completed_at:     Optional[str]   = None    # ISO 8601 string

    # v1 is offshore Polymarket only. The `venue` field is kept as a
    # constant so any legacy code that reads it (`getattr(cfg, "venue",
    # "polymarket")`) continues to resolve. The other polymarket_* and
    # polymarket_us_* SaaS-era credential fields were removed when the
    # local-first pivot moved all secrets into the OS keychain.
    venue:                    str           = "polymarket"

    # Per-category notification toggles. Keys are NOTIFICATION_CATEGORIES.
    # In-app notifications flow through the SQLite event_log table the
    # dashboard reads. Telegram outbound piggy-backs on log_event when
    # a chat_id + bot token are configured. Missing keys default to
    # True so a fresh install gets every notification until the user
    # opts out.
    notification_prefs:       Dict[str, bool] = field(default_factory=dict)

    # Telegram. The bot token (a secret) lives in the OS keychain at
    # KEYRING_TELEGRAM_TOKEN; this is just the recipient chat id (a
    # numeric string from @userinfobot or the user's own chat). Empty
    # string / None means "Telegram is not configured, suppress all
    # outbound pushes".
    telegram_chat_id:         Optional[str]   = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_onboarded(self) -> bool:
        # `tour_completed_at` is set explicitly by the React Onboarding
        # wizard on the final "Done" step. mode + starting_cash both
        # have DB server defaults so checking them returns True the
        # instant the row exists; the wizard never shows. Gate on the
        # explicit completion timestamp instead.
        return self.tour_completed_at is not None

    @property
    def can_trade_live(self) -> bool:
        if self.mode != "live":
            return False
        if not self.wallet_address:
            return False
        return _keyring_get(KEYRING_POLYMARKET_KEY) is not None

    @property
    def ready_to_trade(self) -> bool:
        if not self.is_onboarded:
            return False
        if not self.bot_enabled:
            return False
        if self.mode == "simulation":
            return True
        return self.can_trade_live


# ── Bounds / descriptions ───────────────────────────────────────────────────
USER_CONFIG_BOUNDS: dict[str, Tuple[float, float]] = {
    # base_stake_pct + max_stake_pct upper bounds widened 2026-05-18.
    # Original bounds (0.05 / 0.10) were calibrated for $1000+ bankrolls
    # where 5% of $1000 = $50 per trade comfortably clears Polymarket's
    # $1-and-5-share minimums at any favourite price. At small live
    # bankrolls (<$200) those bounds produced a per-trade cap below
    # the exchange minimum, so the sizer skipped every market —
    # user complaint 2026-05-18: "we have $8.47, why is the bot keep
    # skipping all markets now?". Widening to 100% lets the user
    # explicitly opt into "stake most of bankroll on one trade" when
    # their capital is small. Risk is the user's call; this just
    # removes the artificial floor on what they can configure.
    "base_stake_pct":                (0.005, 1.00),
    "max_stake_pct":                 (0.01,  1.00),
    "daily_loss_limit_pct":          (0.01, 1.00),
    "weekly_loss_limit_pct":         (0.01, 1.00),
    "drawdown_halt_pct":             (0.01, 1.00),
    "streak_cooldown_losses":        (2, 10),
    "dry_powder_reserve_pct":        (0.00, 0.40),
    "cost_assumption_override":      (0.0, 0.10),
    "starting_cash":                 (10.0, 100_000.0),
    # Time-to-resolution bounds in DAYS. Floor is 1 (a market
    # resolving in <1 day is essentially settled noise); ceiling
    # is 30 days - past that is multi-month and tied-up capital
    # most users don't want. 0 isn't in this range because the
    # caster pre-translates 0 -> None (no constraint) before the
    # validator runs.
    "min_days_to_resolution":        (1, 30),
    "max_days_to_resolution":        (1, 30),
    # Exit policy thresholds. Bools (exit_policy_enabled,
    # take_profit_enabled, stop_loss_enabled, time_decay_enabled) are
    # not in this dict — they're cast by _CASTERS and validated as
    # plain bools, not range-bounded.
    "take_profit_threshold_pct":        (0.05, 5.00),  # 5% to 500%
    "stop_loss_threshold_pct":          (0.05, 0.95),  # 5% to 95% loss
    "stop_loss_min_time_remaining_pct": (0.00, 0.95),  # 0% to 95%
    "time_decay_max_hours":             (1, 720),      # 1h to 30d
    "time_decay_flat_band_pct":         (0.00, 1.00),  # 0% to 100%
    "exit_min_time_to_resolution_minutes": (0, 1440),  # 0 to 24h
}

# Band-shaped fields. Stored as JSON-encoded list of [lo, hi] float
# pairs in [0, 1]. Values are validated piecewise (each band must
# satisfy 0 <= lo < hi <= 1) rather than via USER_CONFIG_BOUNDS.
# Flat band fields (none under V1 - the global list was dropped).
# Kept as an empty tuple to avoid touching the cast/validate branches
# that key off membership in this set.
USER_CONFIG_BAND_FIELDS: Tuple[str, ...] = ()

# Dict-of-bands fields. Stored as JSON-encoded dict mapping archetype id
# to a list of [lo, hi] pairs. Validated piecewise.
USER_CONFIG_DICT_BAND_FIELDS: Tuple[str, ...] = ("archetype_skip_market_price_bands",)

USER_CONFIG_LIST_FIELDS: Tuple[str, ...] = ("archetype_skip_list",)
USER_CONFIG_DICT_FIELDS: Tuple[str, ...] = (
    "archetype_stake_multipliers",
    "volume_tier_multipliers",
)
USER_CONFIG_BOOL_DICT_FIELDS: Tuple[str, ...] = ("notification_prefs",)
USER_CONFIG_NULLABLE_FIELDS: Tuple[str, ...] = (
    "cost_assumption_override",
    "min_days_to_resolution",
    "max_days_to_resolution",
)

NOTIFICATION_CATEGORIES: Tuple[str, ...] = (
    # Trade lifecycle.
    "position_opened",
    "position_settled",
    "position_closed_early",   # take-profit / stop-loss / time-decay exit
    "position_invalid",        # market resolved INVALID, stake refunded

    # Order-side problems.
    "order_error",             # rejected by Polymarket before fill
                               # (signer mismatch, insufficient
                               # collateral, etc.)
    "order_rejected",          # placed but didn't fill within timeout

    # Risk + bot state.
    "risk_event",              # circuit breaker trip
    "bot_status",              # paused or resumed
    "bankroll_pause",          # cash below platform minimum;
                               # trading paused until refunded
    "mode_switch",             # toggled SIMULATION <-> LIVE
    "connectivity",            # Polymarket reach state changed
                               # (unreachable / geo_blocked / restored)
    "trading_blocked",         # forecast provider or market scan failure

    # Periodic summaries + proposals.
    "learning_report_ready",   # 50-trade calibration proposal
    "daily_summary",
    "weekly_summary",

    # Legacy key, kept so existing stored prefs that toggled
    # "calibration" off retain intent until the user explicitly
    # toggles the renamed `learning_report_ready`. Hidden from the
    # UI by the Settings page (filtered out of the displayed list).
    "calibration",
)

# Categories shown in the Settings -> Notifications panel. Excludes
# the legacy `calibration` key (its successor `learning_report_ready`
# is the new canonical name; `calibration` is kept only so a user who
# previously disabled it doesn't have it silently re-enabled). New
# installs never see `calibration`; existing installs that flipped it
# off keep their preference effective via should_notify().
NOTIFICATION_CATEGORIES_VISIBLE: Tuple[str, ...] = tuple(
    c for c in NOTIFICATION_CATEGORIES if c != "calibration"
)

ARCHETYPE_MULTIPLIER_BOUNDS: Tuple[float, float] = (0.1, 10.0)

# V1 doctrine archetype defaults (locked 2026-04-27, see CLAUDE.md +
# memory/doctrine_back_the_forecast.md). Hard skips on sport categories
# the market itself prices well; partial-stake on tennis (the market is
# accurate, we still take half-size); over-stake on basketball where
# the forecaster has shown signal.
#
# Seeded into a fresh `user_config` row by `ensure_default_user_config`,
# and conservatively backfilled into existing rows that still have the
# pre-doctrine empty fingerprint (`archetype_skip_list IS NULL` AND
# `archetype_stake_multipliers IN ('{}','')`). Once the user has touched
# either field we never overwrite their choice.
V1_DEFAULT_ARCHETYPE_SKIP_LIST: Tuple[str, ...] = (
    "sports_other", "hockey", "cricket",
    # Crypto micro-window markets ("Bitcoin Up or Down 8:35-8:40 AM
    # ET"). 5-30 min direction calls settled on a single tick;
    # un-researchable and intrinsically efficient. Default-skipped
    # because every LLM evaluation produces either a coin-flip
    # forecast (no edge) or a same-event-verified-no rejection
    # (wasted tokens). User instruction 2026-05-18.
    "crypto_short",
    # activity_count (Musk tweet counts etc.) was added then
    # reverted 2026-05-23 on user instruction: "the bot could
    # still place small bets with the market and see how it goes
    # — we don't have data on performance, we shouldn't disqualify
    # only based on lack of research." Classifier fix from
    # 08d7bf7 stays (Musk-tweet markets now correctly classify
    # as activity_count instead of tech_release) so the per-
    # archetype stake multiplier + performance attribution work,
    # but no default skip — let the bot try them and learn from
    # settled outcomes.
)
V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS: Dict[str, float] = {
    "basketball": 1.5,
    "tennis":     0.5,
}

# Volume-tier multiplier defaults. Bucketed on 24h CLOB volume USD.
# Set 2026-05-28 from the Polymarket accuracy-page research (Brier-vs-
# Volume chart shows lower Brier on higher-volume markets - small,
# real-but-modest tilt toward more-liquid markets).
V1_DEFAULT_VOLUME_TIER_MULTIPLIERS: Dict[str, float] = {
    "low":  0.8,
    "mid":  1.0,
    "high": 1.1,
}

# Thresholds (USD, 24h CLOB volume) that map a market into one of the
# three buckets. Mutating these would invalidate the meaning of the
# user-configured multipliers, so we keep them in code, not config.
VOLUME_TIER_LOW_THRESHOLD:  float = 1_000.0
VOLUME_TIER_HIGH_THRESHOLD: float = 10_000.0

VALID_VOLUME_TIER_KEYS: Tuple[str, ...] = ("low", "mid", "high")

USER_CONFIG_DESCRIPTIONS: dict[str, str] = {
    "base_stake_pct":
        "Baseline stake as a fraction of bankroll, before per-archetype "
        "multipliers and the max_stake cap apply.",
    "max_stake_pct":
        "Hard cap per trade as a fraction of bankroll.",
    "daily_loss_limit_pct":
        "Halts new trades if today's realized loss exceeds this fraction "
        "of starting bankroll.",
    "weekly_loss_limit_pct":
        "Halts new trades if this week's realized loss exceeds this "
        "fraction of starting bankroll.",
    "drawdown_halt_pct":
        "Halts trading for manual review if current equity has fallen "
        "this fraction below the historical peak.",
    "streak_cooldown_losses":
        "After this many consecutive losses, the next 5 trades are "
        "half-sized while the streak is still active.",
    "dry_powder_reserve_pct":
        "Fraction of bankroll held in reserve and never deployed.",
    "cost_assumption_override":
        "Override the sizer's default cost assumption (spread + fees + "
        "slippage). Leave unset to use the built-in 1.5% estimate.",
    "archetype_skip_list":
        "Archetypes the sizer will refuse to trade. The skip is hard, the "
        "trade never opens. Each user maintains their own list.",
    "archetype_stake_multipliers":
        "Per-archetype stake multiplier applied to the flat base stake. "
        "1.0 = no adjustment, 2.0 = double-size, 0.5 = half-size. "
        "Clamped to [0.1, 10.0] per entry.",
    "volume_tier_multipliers":
        "Per-volume-tier stake multiplier (keys: 'low' < $1k, 'mid' "
        "$1k-$10k, 'high' >= $10k, based on 24h CLOB volume). "
        "Multiplied into the stake alongside the archetype "
        "multiplier. Clamped to [0.1, 10.0] per entry.",
    "exit_policy_enabled":
        "Master switch for early-exit logic. When off, positions only "
        "close at natural market settlement.",
    "take_profit_enabled":
        "Close positions when unrealized return crosses the take-profit "
        "threshold (computed against the current bid).",
    "take_profit_threshold_pct":
        "Unrealized return level at which a position is closed in profit. "
        "0.50 = +50%, 1.00 = +100%. Computed against the bid, not the mid.",
    "stop_loss_enabled":
        "Close positions when unrealized return falls below the negative "
        "stop-loss threshold. Gated by min-time-remaining.",
    "stop_loss_threshold_pct":
        "Unrealized loss level at which a position is closed. 0.30 = -30%.",
    "stop_loss_min_time_remaining_pct":
        "Skip stop-loss if less than this fraction of the original time-to-"
        "resolution remains. Prevents cutting losses on a wick near "
        "settlement that would have recovered.",
    "time_decay_enabled":
        "Close stalled positions that have been open beyond max_hours and "
        "are still inside the flat band. Frees capital from markets going "
        "nowhere.",
    "time_decay_max_hours":
        "Hours a position can remain open before time-decay considers it. "
        "Only fires alongside the flat-band check.",
    "time_decay_flat_band_pct":
        "Unrealized return range (±) considered 'flat enough' for time-"
        "decay to close. 0.10 = ±10%. Keeps decay from closing winners.",
    "exit_min_time_to_resolution_minutes":
        "Universal safety floor. Never exit if less than N minutes remain "
        "until natural settlement — avoids spread + fee drag for tiny "
        "time-value gain.",
}


_CASTERS: dict[str, type] = {
    "base_stake_pct":                float,
    "max_stake_pct":                 float,
    "daily_loss_limit_pct":          float,
    "weekly_loss_limit_pct":         float,
    "drawdown_halt_pct":             float,
    "streak_cooldown_losses":        int,
    "dry_powder_reserve_pct":        float,
    "cost_assumption_override":      float,
    "starting_cash":                 float,
    "min_days_to_resolution":        int,
    "max_days_to_resolution":        int,
    # Exit policy
    "exit_policy_enabled":               bool,
    "take_profit_enabled":               bool,
    "take_profit_threshold_pct":         float,
    "stop_loss_enabled":                 bool,
    "stop_loss_threshold_pct":           float,
    "stop_loss_min_time_remaining_pct":  float,
    "time_decay_enabled":                bool,
    "time_decay_max_hours":              int,
    "time_decay_flat_band_pct":          float,
    "exit_min_time_to_resolution_minutes": int,
    # Stake-cap toggle (sizer)
    "max_stake_pct_enabled":             bool,
}

# Persistable subset. Anything not here is silently dropped on update so
# stale venue / US-cred / V0-sizer writes from transferred modules don't
# poison the SQLite schema. Telegram bot token is keychain-only and
# never goes through this set; chat_id and notification_prefs persist.
_PERSISTABLE_COLUMNS: frozenset[str] = frozenset({
    "base_stake_pct",
    "max_stake_pct",
    "daily_loss_limit_pct",
    "weekly_loss_limit_pct",
    "drawdown_halt_pct",
    "streak_cooldown_losses",
    "dry_powder_reserve_pct",
    "cost_assumption_override",
    "archetype_skip_list",
    "archetype_stake_multipliers",
    "volume_tier_multipliers",
    "mode",
    "starting_cash",
    "wallet_address",
    "bot_enabled",
    "notification_prefs",
    "telegram_chat_id",
    "min_days_to_resolution",
    "max_days_to_resolution",
    "archetype_skip_market_price_bands",
    "tour_completed_at",
    # Exit policy
    "exit_policy_enabled",
    "take_profit_enabled",
    "take_profit_threshold_pct",
    "stop_loss_enabled",
    "stop_loss_threshold_pct",
    "stop_loss_min_time_remaining_pct",
    "time_decay_enabled",
    "time_decay_max_hours",
    "time_decay_flat_band_pct",
    "exit_min_time_to_resolution_minutes",
    # Stake-cap toggle (sizer)
    "max_stake_pct_enabled",
})


# ── Casting / validation ────────────────────────────────────────────────────
def _is_unset(raw) -> bool:
    return raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "null", "none"))


def _cast_list(raw) -> Tuple[str, ...]:
    if raw is None:
        return tuple()
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    if isinstance(raw, str):
        return tuple(s.strip() for s in raw.split(",") if s.strip())
    raise ValueError(f"list field must be tuple/list/str, got {type(raw).__name__}")


def _cast_notification_prefs(raw) -> Dict[str, bool]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("notification_prefs must be a JSON object") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"notification_prefs must be a dict, got {type(raw).__name__}")
    allowed = set(NOTIFICATION_CATEGORIES)
    clean: Dict[str, bool] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key or key not in allowed:
            continue
        if isinstance(v, bool):
            clean[key] = v
        elif isinstance(v, (int, float)):
            clean[key] = bool(v)
        elif isinstance(v, str):
            clean[key] = v.strip().lower() in ("1", "true", "yes", "on")
        else:
            raise ValueError(f"notification_prefs[{key!r}] must be boolean")
    return clean


def _cast_skip_market_price_bands(raw) -> Tuple[Tuple[float, float], ...]:
    """Coerce arbitrary input into a tuple of (lo, hi) float pairs.

    Accepts:
      - None or "" -> no bands (empty tuple)
      - JSON string like '[[0.4, 0.5], [0.5, 0.6]]'
      - Python list/tuple of pair-likes [[lo, hi], ...]
      - A single pair like [0.4, 0.5] -> wrapped
    Each pair is normalised to (min, max) with both values clamped to
    [0, 1]. Invalid pairs (lo >= hi after clamping, non-numeric, wrong
    arity) raise ValueError so the persistence path rejects garbage.
    Bands are sorted by lo then deduplicated so the stored representation
    is stable across saves.
    """
    if raw is None or raw == "":
        return tuple()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "skip_market_price_bands must be JSON-encoded "
                "list of [lo, hi] pairs"
            ) from exc
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"skip_market_price_bands must be a list of pairs, "
            f"got {type(raw).__name__}"
        )
    # Allow a single pair like [0.4, 0.5] by wrapping.
    if (
        len(raw) == 2
        and all(isinstance(x, (int, float)) for x in raw)
    ):
        raw = [raw]
    out: list[Tuple[float, float]] = []
    for i, pair in enumerate(raw):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(
                f"skip_market_price_bands[{i}] must be a [lo, hi] pair"
            )
        try:
            lo = float(pair[0])
            hi = float(pair[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"skip_market_price_bands[{i}] values must be numeric"
            ) from exc
        # Clamp to [0, 1] then ensure lo < hi.
        lo = max(0.0, min(1.0, lo))
        hi = max(0.0, min(1.0, hi))
        if lo >= hi:
            raise ValueError(
                f"skip_market_price_bands[{i}]: lo ({lo}) must be < hi ({hi})"
            )
        out.append((lo, hi))
    # Stable order, dedup identical pairs.
    out.sort()
    deduped: list[Tuple[float, float]] = []
    for p in out:
        if not deduped or deduped[-1] != p:
            deduped.append(p)
    return tuple(deduped)


def _cast_archetype_skip_market_price_bands(
    raw,
) -> Dict[str, Tuple[Tuple[float, float], ...]]:
    """Coerce arbitrary input into a {archetype: tuple-of-bands} dict.

    Accepts:
      - None or "" or {} -> empty dict
      - JSON string like '{"tennis": [[0.5, 0.6]]}'
      - Python dict mapping str -> list-of-pair-likes

    Each per-archetype value is validated through
    _cast_skip_market_price_bands so the rules are identical to the
    global skip_market_price_bands. Empty per-archetype lists are
    dropped from the output dict so the stored representation is
    canonical (no '{"tennis": []}' rows).
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "archetype_skip_market_price_bands must be JSON-encoded "
                "object of {archetype: [[lo, hi], ...]}"
            ) from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"archetype_skip_market_price_bands must be a dict, "
            f"got {type(raw).__name__}"
        )
    out: Dict[str, Tuple[Tuple[float, float], ...]] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        bands = _cast_skip_market_price_bands(v)
        if bands:
            out[key] = bands
    return out


def _cast_archetype_multipliers(raw) -> Dict[str, float]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "archetype_stake_multipliers must be a JSON object"
            ) from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"archetype_stake_multipliers must be a dict, got {type(raw).__name__}"
        )
    lo, hi = ARCHETYPE_MULTIPLIER_BOUNDS
    clean: Dict[str, float] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"archetype_stake_multipliers[{key!r}] must be numeric"
            ) from exc
        clean[key] = max(lo, min(hi, f))
    return clean


def cast_value(key: str, raw) -> Union[int, float, tuple, dict, None, str, bool]:
    if key in USER_CONFIG_DICT_FIELDS:
        return _cast_archetype_multipliers(raw)
    if key in USER_CONFIG_BOOL_DICT_FIELDS:
        return _cast_notification_prefs(raw)
    if key in USER_CONFIG_LIST_FIELDS:
        return _cast_list(raw)
    if key in USER_CONFIG_BAND_FIELDS:
        return _cast_skip_market_price_bands(raw)
    if key in USER_CONFIG_DICT_BAND_FIELDS:
        return _cast_archetype_skip_market_price_bands(raw)
    if key in USER_CONFIG_NULLABLE_FIELDS and _is_unset(raw):
        return None
    # 0 is the explicit "no constraint" sentinel for the time-to-
    # resolution fields. Translate before validation so the bounds
    # check (1..30) doesn't reject what the UI considers "off".
    if key in ("min_days_to_resolution", "max_days_to_resolution"):
        try:
            n = int(raw) if raw is not None and raw != "" else None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if n is None or n == 0:
            return None
        return n
    if key == "mode":
        if raw not in ("simulation", "live"):
            raise ValueError("mode must be 'simulation' or 'live'")
        return raw
    if key == "wallet_address":
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None
    if key == "telegram_chat_id":
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None
    if key == "tour_completed_at":
        # ISO 8601 string written by the React Onboarding wizard's
        # final step. None / empty string clears the flag (and resurfaces
        # the wizard on the next launch).
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None
    if key == "bot_enabled":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        raise ValueError("bot_enabled must be boolean")
    if key not in _CASTERS:
        raise ValueError(f"unknown user_config field: {key}")
    try:
        return _CASTERS[key](raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be {_CASTERS[key].__name__}") from exc


def validate_user_config_value(key: str, value) -> None:
    if key in USER_CONFIG_DICT_FIELDS:
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a dict")
        lo, hi = ARCHETYPE_MULTIPLIER_BOUNDS
        for k, v in value.items():
            if not isinstance(k, str) or not k:
                raise ValueError(f"{key} keys must be non-empty strings")
            if not isinstance(v, (int, float)):
                raise ValueError(f"{key}[{k!r}] must be numeric")
            if v < lo or v > hi:
                raise ValueError(f"{key}[{k!r}]={v} outside bounds [{lo}, {hi}]")
        return
    if key in USER_CONFIG_BOOL_DICT_FIELDS:
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a dict")
        for k, v in value.items():
            if not isinstance(k, str) or not k:
                raise ValueError(f"{key} keys must be non-empty strings")
            if not isinstance(v, bool):
                raise ValueError(f"{key}[{k!r}] must be a boolean")
        return
    if key in USER_CONFIG_LIST_FIELDS:
        if not isinstance(value, tuple):
            raise ValueError(f"{key} must be a tuple of strings")
        return
    if key in USER_CONFIG_BAND_FIELDS:
        # Already cast to a tuple of (lo, hi) tuples by the caster.
        # Re-validate piecewise so a direct-call path (not going
        # through cast_value) still gets the same bounds check.
        if not isinstance(value, tuple):
            raise ValueError(f"{key} must be a tuple of (lo, hi) pairs")
        for i, pair in enumerate(value):
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError(f"{key}[{i}] must be a (lo, hi) pair")
            lo, hi = pair
            if not (0.0 <= lo < hi <= 1.0):
                raise ValueError(
                    f"{key}[{i}]=({lo},{hi}) must satisfy 0 <= lo < hi <= 1"
                )
        return
    if key in USER_CONFIG_DICT_BAND_FIELDS:
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a dict of {{archetype: bands}}")
        for arch, bands in value.items():
            if not isinstance(arch, str) or not arch:
                raise ValueError(f"{key} keys must be non-empty strings")
            if not isinstance(bands, tuple):
                raise ValueError(
                    f"{key}[{arch!r}] must be a tuple of (lo, hi) pairs"
                )
            for i, pair in enumerate(bands):
                if not isinstance(pair, tuple) or len(pair) != 2:
                    raise ValueError(
                        f"{key}[{arch!r}][{i}] must be a (lo, hi) pair"
                    )
                lo, hi = pair
                if not (0.0 <= lo < hi <= 1.0):
                    raise ValueError(
                        f"{key}[{arch!r}][{i}]=({lo},{hi}) must satisfy "
                        f"0 <= lo < hi <= 1"
                    )
        return
    if key in USER_CONFIG_NULLABLE_FIELDS and value is None:
        return
    # Pure bool / string fields with no numeric bounds. The caster already
    # confirmed the type; nothing else to validate. Without this list, the
    # final "if key not in USER_CONFIG_BOUNDS" branch rejects every
    # bool-toggle update with "unknown user_config field" even though the
    # field is fully registered in _CASTERS and _PERSISTABLE_COLUMNS.
    # tour_completed_at: opaque ISO-8601 string written once by the
    # Onboarding wizard's final step. No bounds check; the caster
    # already normalised None/empty to None.
    if key in (
        "mode", "wallet_address", "bot_enabled", "telegram_chat_id",
        "tour_completed_at",
        # Exit-policy toggles. Each pairs with a numeric threshold that
        # IS bounded (take_profit_threshold_pct, etc.); only the bool
        # switches need the no-bounds escape hatch.
        "exit_policy_enabled",
        "take_profit_enabled",
        "stop_loss_enabled",
        "time_decay_enabled",
        # Stake-cap toggle. Pairs with the bounded max_stake_pct.
        "max_stake_pct_enabled",
    ):
        return
    if key not in USER_CONFIG_BOUNDS:
        raise ValueError(f"unknown user_config field: {key}")
    lo, hi = USER_CONFIG_BOUNDS[key]
    if value < lo or value > hi:
        raise ValueError(f"{key}={value} outside bounds [{lo}, {hi}]")


def validated_update_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    clean: dict = {}
    for key, raw in payload.items():
        value = cast_value(key, raw)
        validate_user_config_value(key, value)
        clean[key] = value
    _validate_time_to_resolution(clean)
    _validate_mode_credentials(clean)
    return clean


def _validate_mode_credentials(clean: dict) -> None:
    """Cross-field rule: mode='live' requires wallet + private key.

    Without this gate, PUT /api/config {"mode":"live"} silently sets
    mode=live with no credentials. The next scan sees `mode=live` +
    `bot_enabled=true`, calls `_open_live`, finds no creds, returns
    None - silent skip. The dashboard says "live mode" but no trades
    happen and no error surfaces. Reject the mode flip up front so
    the user sees a clear "wallet/key not set" error in the Settings
    UI instead.
    """
    if clean.get("mode") != "live":
        return
    persisted = get_user_config()
    new_wallet = clean.get("wallet_address", persisted.wallet_address)
    if not new_wallet:
        raise ValueError(
            "live mode requires a wallet address. Set it in Settings -> "
            "Connections before switching mode."
        )
    if _keyring_get(KEYRING_POLYMARKET_KEY) is None:
        raise ValueError(
            "live mode requires a Polymarket private key in the "
            "keychain. Paste it in Settings -> Connections before "
            "switching mode."
        )


def _validate_time_to_resolution(clean: dict) -> None:
    """Cross-field rule: max_days >= min_days when both are set.

    Either field may arrive in this update OR already be persisted on
    the singleton row from a prior update. We resolve to the EFFECTIVE
    pair (incoming value if provided, else current persisted value)
    and compare. None on either side means "no constraint" and skips
    the check.
    """
    if "min_days_to_resolution" not in clean and "max_days_to_resolution" not in clean:
        return
    # Resolve missing side from the persisted singleton.
    persisted = get_user_config()
    new_min = clean.get("min_days_to_resolution",
                        persisted.min_days_to_resolution)
    new_max = clean.get("max_days_to_resolution",
                        persisted.max_days_to_resolution)
    if new_min is None or new_max is None:
        return
    if new_max < new_min:
        raise ValueError(
            f"max_days_to_resolution ({new_max}) must be >= "
            f"min_days_to_resolution ({new_min}). Set either one to "
            f"0 to remove its constraint."
        )


# ── Decode helpers ──────────────────────────────────────────────────────────
def _decode_csv(raw) -> Tuple[str, ...]:
    if raw is None:
        return tuple()
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return tuple(s.strip() for s in str(raw).split(",") if s.strip())


def _decode_archetype_multipliers(raw) -> Dict[str, float]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    lo, hi = ARCHETYPE_MULTIPLIER_BOUNDS
    out: Dict[str, float] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        out[key] = max(lo, min(hi, f))
    return out


def _decode_archetype_skip_market_price_bands(
    raw,
) -> Dict[str, Tuple[Tuple[float, float], ...]]:
    """Read path mirror of _cast_archetype_skip_market_price_bands.
    Lenient on bad rows so a corrupt cell doesn't take the whole config
    offline."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Tuple[Tuple[float, float], ...]] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        bands = _decode_skip_market_price_bands(v)
        if bands:
            out[key] = bands
    return out


def _decode_skip_market_price_bands(raw) -> Tuple[Tuple[float, float], ...]:
    """Read path mirror of _cast_skip_market_price_bands. Lenient on
    bad rows so a corrupt cell doesn't take the whole config offline -
    the caster on the write path is the strict gate."""
    if raw is None or raw == "":
        return tuple()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return tuple()
    if not isinstance(raw, (list, tuple)):
        return tuple()
    out: list[Tuple[float, float]] = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        try:
            lo, hi = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            continue
        lo = max(0.0, min(1.0, lo))
        hi = max(0.0, min(1.0, hi))
        if lo >= hi:
            continue
        out.append((lo, hi))
    out.sort()
    return tuple(out)


def _encode_csv(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value if str(x).strip()]
        return ",".join(items) if items else None
    s = str(value).strip()
    return s or None


# ── DB-backed accessors ─────────────────────────────────────────────────────
def ensure_default_user_config() -> None:
    """Insert the singleton 'local' row if missing, and backfill V1
    archetype defaults onto rows that have never been touched.

    Future fresh installs hit the INSERT path and get the doctrine
    values from the start. The UPDATE path only exists to backfill
    pre-V1 local DBs that were seeded with empty defaults; it fires
    iff the row still has the factory-state fingerprint AND the user
    has not finished onboarding yet (`tour_completed_at IS NULL`).
    Once the onboarding flow has been completed we never overwrite
    the user's stored choice, even if it happens to look like the
    factory state.

    Idempotent: re-running on a row that already matches V1 defaults
    is a no-op.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        skip_csv = ",".join(V1_DEFAULT_ARCHETYPE_SKIP_LIST)
        mult_json = json.dumps(V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS)
        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO user_config "
                "(user_id, archetype_skip_list, archetype_stake_multipliers) "
                "VALUES (:uid, :skip, :mult) "
                "ON CONFLICT (user_id) DO NOTHING"
            ), {"uid": DEFAULT_USER_ID, "skip": skip_csv, "mult": mult_json})
            conn.execute(text(
                "UPDATE user_config "
                "SET archetype_skip_list = :skip, "
                "    archetype_stake_multipliers = :mult "
                "WHERE user_id = :uid "
                "  AND archetype_skip_list IS NULL "
                "  AND COALESCE(archetype_stake_multipliers, '{}') "
                "      IN ('{}', '', 'null') "
                "  AND tour_completed_at IS NULL"
            ), {"uid": DEFAULT_USER_ID, "skip": skip_csv, "mult": mult_json})

            # ── Backfill tour_completed_at for already-onboarded users ──
            # 2026-05-28: the prior Onboarding wizard only stamped
            # `tour_completed_at` when the user clicked the final "Open
            # the dashboard" button. Any user who closed the GUI before
            # that click (or whose request silently failed) re-saw the
            # wizard on next launch. v1.5.22 removes the Done screen so
            # the new code stamps the flag on Save/Skip instead, but
            # users who got stuck mid-flight need a one-shot heal.
            #
            # Heuristic: a non-null wallet_address is a clear "this user
            # has been past the wizard before" signal. wallet_address
            # only gets written when the user types a private key in
            # either Onboarding or Settings - on a fresh install it's
            # NULL until the user does that. Stamping NOW unblocks
            # those users without forcing them to re-click the wizard.
            # Fires once per user; subsequent boots are no-ops because
            # the WHERE clause filters on `tour_completed_at IS NULL`.
            conn.execute(text(
                "UPDATE user_config "
                "SET tour_completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE user_id = :uid "
                "  AND tour_completed_at IS NULL "
                "  AND wallet_address IS NOT NULL"
            ), {"uid": DEFAULT_USER_ID})

            # ── One-shot migration: global skip_market_price_bands ──────
            # The global Price band filter was dropped 2026-05-03 in
            # favour of per-archetype bands only. Copy any existing
            # global band list to every known archetype so the user's
            # prior settings carry over, then NULL out the global so
            # this migration only fires once.
            row = conn.execute(text(
                "SELECT skip_market_price_bands, "
                "       archetype_skip_market_price_bands "
                "FROM user_config WHERE user_id = :uid"
            ), {"uid": DEFAULT_USER_ID}).fetchone()
            if row is not None:
                global_raw = row[0]
                arch_raw   = row[1]
                arch_is_empty = (
                    arch_raw is None
                    or arch_raw == ""
                    or arch_raw in ("{}", "null")
                )
                if global_raw and arch_is_empty:
                    try:
                        global_list = json.loads(global_raw)
                    except (TypeError, ValueError):
                        global_list = None
                    if isinstance(global_list, list) and global_list:
                        # Lazy import here to avoid pulling the
                        # archetype classifier on every config read.
                        from engine.archetype_classifier import ARCHETYPES
                        per_archetype = {
                            arch: list(global_list) for arch in ARCHETYPES
                        }
                        conn.execute(text(
                            "UPDATE user_config "
                            "SET archetype_skip_market_price_bands = :a, "
                            "    skip_market_price_bands = NULL "
                            "WHERE user_id = :uid"
                        ), {
                            "uid": DEFAULT_USER_ID,
                            "a":   json.dumps(per_archetype),
                        })
                        print(
                            f"[user_config] migrated {len(global_list)} "
                            f"global band(s) to {len(ARCHETYPES)} archetypes",
                            file=sys.stderr,
                        )

            # ── Fix-up migration: clear the broken left-only band pattern ──
            # Earlier today's migration mis-mapped legacy
            # `min_market_favourite_price` to a left-only band set
            # [[0.0,0.1] ... [(N-1)*0.1, N*0.1]] which blocked EVERY
            # NO-favoured market plus the weak YES band - effectively
            # making the bot YES-only. The correct symmetric middle-band
            # set is now produced by the corrected migration in
            # db/models.py, but rows already migrated need to be fixed
            # here.
            #
            # Detection: every archetype's bands are identical AND the
            # first band is [0.0, 0.1] AND the bands form a consecutive
            # run starting at 0.0. That's the exact broken-migration
            # signature; manual user edits would not produce it.
            #
            # Action: clear the per-archetype bands entirely. The user
            # gets a clean slate to reconfigure from the Risk page.
            row = conn.execute(text(
                "SELECT archetype_skip_market_price_bands "
                "FROM user_config WHERE user_id = :uid"
            ), {"uid": DEFAULT_USER_ID}).fetchone()
            if row is not None and row[0]:
                try:
                    arch_dict = json.loads(row[0])
                except (TypeError, ValueError):
                    arch_dict = None
                if isinstance(arch_dict, dict) and arch_dict:
                    # Pull the first archetype's band list as a reference
                    # set; we want every archetype to match this exactly.
                    sample = next(iter(arch_dict.values()))
                    def _is_broken_left_only(bands):
                        if not isinstance(bands, list) or len(bands) < 1:
                            return False
                        if not all(
                            isinstance(p, list) and len(p) == 2
                            for p in bands
                        ):
                            return False
                        # Must be consecutive 10pp buckets starting at 0.0.
                        for i, (lo, hi) in enumerate(bands):
                            expected_lo = i * 0.10
                            expected_hi = expected_lo + 0.10
                            if abs(lo - expected_lo) > 1e-6:
                                return False
                            if abs(hi - expected_hi) > 1e-6:
                                return False
                        return True
                    if _is_broken_left_only(sample) and all(
                        bands == sample for bands in arch_dict.values()
                    ):
                        conn.execute(text(
                            "UPDATE user_config "
                            "SET archetype_skip_market_price_bands = '{}' "
                            "WHERE user_id = :uid"
                        ), {"uid": DEFAULT_USER_ID})
                        print(
                            f"[user_config] cleared broken left-only "
                            f"band migration on {len(arch_dict)} "
                            f"archetypes (was [{len(sample)} bands])",
                            file=sys.stderr,
                        )
    except Exception as exc:
        print(f"[user_config] ensure_default failed: {exc}", file=sys.stderr)


def _decode_notification_prefs(raw) -> Dict[str, bool]:
    """Decode the JSON-text notification_prefs column. Drops unknown keys."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    allowed = set(NOTIFICATION_CATEGORIES)
    out: Dict[str, bool] = {}
    for k, v in raw.items():
        if str(k) not in allowed:
            continue
        if isinstance(v, bool):
            out[str(k)] = v
        elif isinstance(v, (int, float)):
            out[str(k)] = bool(v)
        elif isinstance(v, str):
            out[str(k)] = v.strip().lower() in ("1", "true", "yes", "on")
    return out


def get_user_config(user_id: str = DEFAULT_USER_ID) -> UserConfig:
    """Load the singleton config row. Returns dataclass defaults on any error."""
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT base_stake_pct, max_stake_pct, "
                "       daily_loss_limit_pct, weekly_loss_limit_pct, "
                "       drawdown_halt_pct, streak_cooldown_losses, "
                "       dry_powder_reserve_pct, "
                "       cost_assumption_override, archetype_skip_list, "
                "       mode, starting_cash, wallet_address, "
                "       bot_enabled, archetype_stake_multipliers, "
                "       notification_prefs, telegram_chat_id, "
                "       min_days_to_resolution, max_days_to_resolution, "
                "       archetype_skip_market_price_bands, "
                "       tour_completed_at, "
                # Exit policy (indices 20..29). Read them here so an
                # UPDATE survives the round-trip - without these the
                # write path persists correctly but the dataclass
                # falls back to defaults on every read.
                "       exit_policy_enabled, take_profit_enabled, "
                "       take_profit_threshold_pct, stop_loss_enabled, "
                "       stop_loss_threshold_pct, "
                "       stop_loss_min_time_remaining_pct, "
                "       time_decay_enabled, time_decay_max_hours, "
                "       time_decay_flat_band_pct, "
                "       exit_min_time_to_resolution_minutes, "
                "       max_stake_pct_enabled, "
                "       volume_tier_multipliers "
                "FROM user_config WHERE user_id = :uid"
            ), {"uid": user_id}).fetchone()
        if row is None:
            return UserConfig()
        return UserConfig(
            base_stake_pct                = float(row[0]),
            max_stake_pct                 = float(row[1]),
            daily_loss_limit_pct          = float(row[2]),
            weekly_loss_limit_pct         = float(row[3]),
            drawdown_halt_pct             = float(row[4]),
            streak_cooldown_losses        = int(row[5]),
            dry_powder_reserve_pct        = float(row[6]),
            cost_assumption_override      = (float(row[7]) if row[7] is not None else None),
            archetype_skip_list           = _decode_csv(row[8]),
            mode                          = (str(row[9]) if row[9] is not None else None),
            starting_cash                 = (float(row[10]) if row[10] is not None else None),
            wallet_address                = (str(row[11]) if row[11] is not None else None),
            bot_enabled                   = bool(row[12]) if row[12] is not None else False,
            archetype_stake_multipliers   = _decode_archetype_multipliers(row[13]),
            notification_prefs            = _decode_notification_prefs(row[14]),
            telegram_chat_id              = (str(row[15]).strip() if row[15] is not None and str(row[15]).strip() else None),
            min_days_to_resolution        = (int(row[16]) if row[16] is not None else None),
            max_days_to_resolution        = (int(row[17]) if row[17] is not None else None),
            archetype_skip_market_price_bands = _decode_archetype_skip_market_price_bands(row[18]),
            tour_completed_at             = (str(row[19]) if row[19] is not None else None),
            # Exit policy. row[20..30] correspond to the dataclass
            # defaults defined at the top of UserConfig.
            exit_policy_enabled                 = bool(row[20]) if row[20] is not None else False,
            take_profit_enabled                 = bool(row[21]) if row[21] is not None else True,
            take_profit_threshold_pct           = float(row[22]) if row[22] is not None else 0.50,
            stop_loss_enabled                   = bool(row[23]) if row[23] is not None else True,
            stop_loss_threshold_pct             = float(row[24]) if row[24] is not None else 0.30,
            stop_loss_min_time_remaining_pct    = float(row[25]) if row[25] is not None else 0.20,
            time_decay_enabled                  = bool(row[26]) if row[26] is not None else False,
            time_decay_max_hours                = int(row[27])   if row[27] is not None else 72,
            time_decay_flat_band_pct            = float(row[28]) if row[28] is not None else 0.10,
            exit_min_time_to_resolution_minutes = int(row[29])   if row[29] is not None else 5,
            max_stake_pct_enabled               = bool(row[30]) if row[30] is not None else False,
            volume_tier_multipliers             = _decode_archetype_multipliers(row[31])
                                                    if len(row) > 31 else {},
        )
    except Exception as exc:
        print(f"[user_config] get_user_config failed: {exc}", file=sys.stderr)
        return UserConfig()


def update_user_config(user_id: str = DEFAULT_USER_ID, **changes) -> UserConfig:
    """Validate and apply field updates. Atomic on the DB side."""
    if not changes:
        return get_user_config(user_id)

    clean: dict = {}
    for key, raw in changes.items():
        if key not in _PERSISTABLE_COLUMNS:
            print(f"[user_config] dropping non-persistable key {key!r}",
                  file=sys.stderr)
            continue
        value = cast_value(key, raw)
        validate_user_config_value(key, value)
        clean[key] = value

    if not clean:
        return get_user_config(user_id)

    # Cross-field check: defended here too so direct callers
    # (onboarding flows, telegram setters, etc.) get the same
    # max>=min protection as the dashboard PUT path.
    _validate_time_to_resolution(clean)

    json_fields = set(USER_CONFIG_DICT_FIELDS) | set(USER_CONFIG_BOOL_DICT_FIELDS)
    set_parts = ", ".join(f"{k} = :{k}" for k in clean)

    params: dict = {}
    for k, v in clean.items():
        if k in USER_CONFIG_LIST_FIELDS:
            params[k] = _encode_csv(v)
        elif k in USER_CONFIG_BAND_FIELDS:
            # Tuple of (lo, hi) tuples -> JSON-encoded list of [lo, hi].
            # Empty tuple persists as JSON empty list, which the read
            # decoder treats identically to NULL (no bands disabled).
            params[k] = json.dumps([list(p) for p in (v or ())])
        elif k in USER_CONFIG_DICT_BAND_FIELDS:
            # Dict[str, tuple-of-tuples] -> JSON object mapping
            # archetype -> list-of-pairs. Empty dict serialises to '{}'.
            encoded: dict[str, list[list[float]]] = {}
            for arch, bands in (v or {}).items():
                encoded[arch] = [list(p) for p in (bands or ())]
            params[k] = json.dumps(encoded)
        elif k in json_fields:
            params[k] = json.dumps(v or {})
        else:
            params[k] = v
    params["uid"] = user_id

    from sqlalchemy import text
    from db.engine import get_engine

    # Snapshot the bot_enabled value BEFORE the write so the audit
    # entry can record old → new. Only matters when bot_enabled is
    # actually in this update; we skip the extra DB read otherwise.
    prev_enabled: Optional[bool] = None
    if "bot_enabled" in clean:
        try:
            prev_enabled = get_user_config(user_id).bot_enabled
        except Exception:
            prev_enabled = None

    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO user_config (user_id) VALUES (:uid) "
            "ON CONFLICT (user_id) DO NOTHING"
        ), {"uid": user_id})
        conn.execute(text(
            f"UPDATE user_config SET {set_parts}, "
            f"updated_at = CURRENT_TIMESTAMP "
            f"WHERE user_id = :uid"
        ), params)

        # Audit trail for bot_enabled toggles. The flag is the
        # single most consequential user-facing setting (it's
        # literally "is the bot running"), and 2026-05-14 we hit a
        # bug where the bot was flipping ON without a user action.
        # Capturing every change with caller info + an event_log
        # row makes the next occurrence forensically obvious.
        if "bot_enabled" in clean:
            new_enabled = bool(clean["bot_enabled"])
            if new_enabled != prev_enabled:
                import traceback
                # extract_stack returns frames outer→inner; the
                # last entry is this function. Take the 8 most
                # recent caller frames — enough to show the call
                # chain (handler → asyncio offload → here) without
                # spamming the log.
                stack = traceback.extract_stack()[:-1][-8:]
                stack_lines = " | ".join(
                    f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}"
                    for f in stack
                )
                print(
                    f"[bot_enabled_audit] {prev_enabled} -> {new_enabled} "
                    f"for user={user_id} | stack: {stack_lines}",
                    file=sys.stderr, flush=True,
                )
                # Best-effort event_log write. If the table schema
                # has drifted or the connection is wedged, swallow
                # the exception — the stderr line above is the
                # primary audit channel; this is the persisted
                # backup.
                try:
                    conn.execute(text(
                        "INSERT INTO event_log "
                        "(user_id, timestamp, event_type, severity, "
                        " description, source) "
                        "VALUES (:uid, CURRENT_TIMESTAMP, "
                        " 'bot_enabled_changed', 'info', :desc, :src)"
                    ), {
                        "uid":  user_id,
                        "desc": f"{prev_enabled} -> {new_enabled}",
                        "src":  stack_lines[:500],
                    })
                except Exception as exc:
                    print(f"[bot_enabled_audit] event_log insert "
                          f"failed (non-fatal): {exc}",
                          file=sys.stderr, flush=True)

    return get_user_config(user_id)


# ── Secret storage ──────────────────────────────────────────────────────────
#
# Secrets used to live in the macOS keychain. They no longer do.
#
# Why we moved off keychain (2026-04-29):
#   - Each rebuild produces a binary with a new code signature.
#   - macOS keychain ACLs are per-binary-signature.
#   - Reading entries written by an earlier signature triggers a
#     SecurityAgent password prompt.
#   - On a fresh install with 7 stored secrets that meant the user
#     typed their login password 7 times in a row before the app
#     became usable.
#   - Worse, while a SecurityAgent prompt is on screen the macOS
#     Security framework holds an internal mutex; Python's keyring
#     binding doesn't release the GIL during that wait, which wedges
#     the sidecar's asyncio event loop. Every endpoint times out
#     until the user clears the prompt cascade. (Sample(1) trace
#     showed the main thread in `_pthread_mutex_firstfit_lock_slow`
#     while keyring threads sat in SecurityAgent.)
#
# Where they live now: <app-data>/data/secrets.json
#   - Flat JSON `{key: value}` map.
#   - chmod 600 so only the running user can read.
#   - Atomic write via tempfile + os.replace.
#   - Cached in-process so subsequent reads don't touch the disk.
#
# Practical security comparison:
#   - macOS already access-controls $HOME (other users can't read).
#   - chmod 600 narrows that to "only this user".
#   - Keychain ACLs don't add real protection on an unlocked Mac
#     where a malicious user-process can read either store.
#   - Tradeoff is small; UX gain is enormous.
#
# Migration: every legacy keychain entry is read AT MOST ONCE per
# process, copied to the file, then deleted from keychain. So the
# old prompt cascade fires one last time during the upgrade boot,
# then never again.

import threading as _threading  # noqa: E402
_SECRETS_LOCK: _threading.Lock = _threading.Lock()
_SECRETS_CACHE: Optional[dict] = None


def _secrets_path():
    """Path to secrets.json. Lazy-imports app_data_dir to keep this
    module's import graph small."""
    from db.engine import app_data_dir
    p = app_data_dir() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p / "secrets.json"


def _read_secrets() -> dict:
    """Return the current secrets map. Cached in-process; first call
    after process start hits disk, subsequent calls are free."""
    global _SECRETS_CACHE
    with _SECRETS_LOCK:
        if _SECRETS_CACHE is not None:
            return dict(_SECRETS_CACHE)
        path = _secrets_path()
        if not path.exists():
            _SECRETS_CACHE = {}
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            _SECRETS_CACHE = data
            return dict(data)
        except Exception as exc:
            print(f"[user_config] secrets file read failed: {exc}",
                  file=sys.stderr)
            _SECRETS_CACHE = {}
            return {}


def _write_secrets(data: dict) -> None:
    """Atomic write of the secrets map. Updates the in-process cache
    so subsequent reads see the new value without re-hitting disk."""
    global _SECRETS_CACHE
    with _SECRETS_LOCK:
        path = _secrets_path()
        import tempfile
        import os as _os
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=".secrets.",
            suffix=".json",
        )
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            _os.chmod(tmp, 0o600)
            _os.replace(tmp, str(path))
            _SECRETS_CACHE = dict(data)
        except Exception:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            raise


# Track which legacy keychain entries we've already attempted to
# migrate during this process. Stops repeated SecurityAgent prompts
# for keys that genuinely don't exist (the legacy read still costs
# us a keychain syscall even when the entry is absent).
_MIGRATED_KEYS: set[str] = set()


def _keyring_get(key: str) -> Optional[str]:
    """Read a secret. File-first; legacy keychain only as a one-time
    migration fallback."""
    secrets = _read_secrets()
    val = secrets.get(key)
    if val:
        return val

    if key in _MIGRATED_KEYS:
        return None
    _MIGRATED_KEYS.add(key)

    # Migration: try the legacy keychain entry exactly once.
    try:
        import keyring
        legacy = keyring.get_password(KEYRING_SERVICE, key)
    except Exception as exc:
        print(f"[user_config] legacy keyring get({key}) failed: {exc}",
              file=sys.stderr)
        return None

    if not legacy:
        return None

    # Found a legacy value. Copy it to the file and wipe the keychain
    # entry so future reads bypass keychain entirely.
    secrets[key] = legacy
    try:
        _write_secrets(secrets)
        try:
            import keyring as _kr
            _kr.delete_password(KEYRING_SERVICE, key)
        except Exception:
            pass
    except Exception as exc:
        print(f"[user_config] secrets migrate({key}) failed: {exc}",
              file=sys.stderr)
    return legacy


def _keyring_set(key: str, value: Optional[str]) -> None:
    """Write/clear a secret. Always file-backed. Best-effort wipes the
    legacy keychain entry too so a stale value can't override the file.

    File-write failures RAISE rather than silently log. Earlier
    behaviour was: on file-write failure, print to stderr + return
    None. Upstream `set_anthropic_api_key` /
    `set_user_polymarket_creds` therefore reported success;
    `local_api._put_credentials` returned 200 OK to the UI. The
    user thought their key was saved and the next live trade
    discovered it wasn't. Now the exception propagates to the
    route's try/except and the UI sees a clear 500 with the
    failure reason.
    """
    secrets = _read_secrets()
    if value is None or value == "":
        secrets.pop(key, None)
    else:
        secrets[key] = value
    _write_secrets(secrets)
    # Best-effort: clear any legacy keychain entry. Cheap on a clean
    # install (entry doesn't exist, delete is a no-op); on upgrade
    # this purges the legacy copy so reads short-circuit at the file.
    try:
        import keyring
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except Exception:
            pass
    except Exception:
        pass


# ── Polymarket creds (offshore Polygon EIP-712) ─────────────────────────────
def get_user_polymarket_creds(user_id: str = DEFAULT_USER_ID) -> dict:
    """Return {'wallet_address', 'private_key'} for the local user."""
    cfg = get_user_config(user_id)
    return {
        "wallet_address": cfg.wallet_address,
        "private_key":    _keyring_get(KEYRING_POLYMARKET_KEY),
    }


def set_user_polymarket_creds(
    user_id: str = DEFAULT_USER_ID,
    *,
    wallet_address: Optional[str] = None,
    private_key:    Optional[str] = None,
) -> None:
    """Wallet address goes to DB, private key goes to OS keychain. Either may be None to clear."""
    if wallet_address is not None:
        update_user_config(user_id, wallet_address=wallet_address)
    if private_key is not None:
        _keyring_set(KEYRING_POLYMARKET_KEY, private_key)


def get_active_polymarket_creds(cfg: UserConfig) -> dict:
    """Live trader hands this dict to the executor. Always offshore in v1."""
    return {
        "wallet_address": cfg.wallet_address,
        "private_key":    _keyring_get(KEYRING_POLYMARKET_KEY),
    }


# ── LLM connections (multi-provider list + role assignment) ─────────────────
# secrets.json shape:
#   "llm_connections": [ {id, provider, label, model, base_url, api_key}, ... ]
#   "llm_roles":       { "forecaster_primary": <id|null>, "forecaster_backup": ...,
#                        "search_primary": ..., "search_backup": ... }
#
# This replaces the three fixed slots (anthropic_api_key / llm_backup_api_key /
# gemini_api_key). The user adds an API key for ANY provider, picks the model
# per entry, and assigns which connection serves which use case (forecaster vs
# search) as primary or backup. On first read we migrate those three legacy
# flat keys into the list once, then drop them so the list is the single
# source of truth.
SECRETS_LLM_CONNECTIONS = "llm_connections"
SECRETS_LLM_ROLES = "llm_roles"


def _new_connection_id() -> str:
    import secrets as _sec
    return "conn_" + _sec.token_hex(6)


def _empty_roles() -> dict:
    return {r: None for r in _providers.ROLE_KEYS}


def _migrate_legacy_llm_keys(secrets_map: dict) -> bool:
    """Build llm_connections + llm_roles from the three legacy flat keys
    the first time. Mutates secrets_map in place; returns True if it
    changed anything (caller persists).

    Idempotent: keyed off the presence of the llm_connections field, so
    a second call is a no-op even when the migrated list is empty (the
    user had no keys to migrate).
    """
    if SECRETS_LLM_CONNECTIONS in secrets_map:
        return False

    conns: list[dict] = []
    roles = _empty_roles()

    def _mk(api_key: str, fallback_provider: str) -> dict:
        prov = _providers.detect_provider(api_key) or fallback_provider
        return _providers.normalize_connection({
            "id":       _new_connection_id(),
            "provider": prov,
            "api_key":  api_key,
        })

    primary = (secrets_map.get(KEYRING_ANTHROPIC_KEY) or "").strip()
    backup  = (secrets_map.get(KEYRING_LLM_BACKUP_KEY) or "").strip()
    gemini  = (secrets_map.get(KEYRING_GEMINI_KEY) or "").strip()

    if primary:
        c = _mk(primary, "anthropic")
        conns.append(c)
        roles["forecaster_primary"] = c["id"]
    if backup:
        c = _mk(backup, "anthropic")
        conns.append(c)
        roles["forecaster_backup"] = c["id"]
    if gemini:
        c = _mk(gemini, "gemini")
        conns.append(c)
        roles["search_primary"] = c["id"]

    secrets_map[SECRETS_LLM_CONNECTIONS] = conns
    secrets_map[SECRETS_LLM_ROLES] = roles
    # The list now owns these; drop the flat copies so there is exactly
    # one source of truth.
    for k in (KEYRING_ANTHROPIC_KEY, KEYRING_LLM_BACKUP_KEY, KEYRING_GEMINI_KEY):
        secrets_map.pop(k, None)
    return True


def get_llm_connections() -> list[dict]:
    """Return the configured LLM connections (each a dict with
    id/provider/label/model/base_url/api_key). Runs the one-time legacy
    migration on first access."""
    secrets_map = _read_secrets()
    if _migrate_legacy_llm_keys(secrets_map):
        _write_secrets(secrets_map)
    raw = secrets_map.get(SECRETS_LLM_CONNECTIONS) or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for e in raw:
        if isinstance(e, dict) and e.get("id"):
            out.append(dict(e))
    return out


def get_llm_connection(conn_id: Optional[str]) -> Optional[dict]:
    if not conn_id:
        return None
    for c in get_llm_connections():
        if c["id"] == conn_id:
            return c
    return None


def _save_llm_connections(conns: list[dict], roles: Optional[dict] = None) -> None:
    """Persist the connection list (and optionally the role map). Always
    stamps the migration sentinel + clears the legacy flat keys so a
    later read never re-migrates over a user edit."""
    secrets_map = _read_secrets()
    secrets_map[SECRETS_LLM_CONNECTIONS] = [dict(c) for c in conns]
    if roles is not None:
        secrets_map[SECRETS_LLM_ROLES] = dict(roles)
    elif SECRETS_LLM_ROLES not in secrets_map:
        secrets_map[SECRETS_LLM_ROLES] = _empty_roles()
    for k in (KEYRING_ANTHROPIC_KEY, KEYRING_LLM_BACKUP_KEY, KEYRING_GEMINI_KEY):
        secrets_map.pop(k, None)
    _write_secrets(secrets_map)


def get_llm_roles() -> dict:
    """Return the role->connection-id map. Always has all four role keys
    present; pointers to deleted connections are coerced to None."""
    valid_ids = {c["id"] for c in get_llm_connections()}
    secrets_map = _read_secrets()
    raw = secrets_map.get(SECRETS_LLM_ROLES) or {}
    roles = _empty_roles()
    if isinstance(raw, dict):
        for k in roles:
            v = raw.get(k)
            if isinstance(v, str) and v in valid_ids:
                roles[k] = v
    return roles


def set_llm_roles(roles: dict) -> dict:
    """Persist the full role map. Unknown role keys are ignored; pointers
    to non-existent connections are stored as None."""
    valid_ids = {c["id"] for c in get_llm_connections()}
    clean = _empty_roles()
    if isinstance(roles, dict):
        for k in clean:
            v = roles.get(k)
            clean[k] = v if (isinstance(v, str) and v in valid_ids) else None
    secrets_map = _read_secrets()
    secrets_map[SECRETS_LLM_ROLES] = clean
    _write_secrets(secrets_map)
    return clean


def set_llm_role(role: str, conn_id: Optional[str]) -> dict:
    """Assign (or clear, with conn_id=None) a single role slot."""
    if role not in _providers.ROLE_KEYS:
        raise ValueError(f"unknown role '{role}'")
    roles = get_llm_roles()
    roles[role] = conn_id or None
    return set_llm_roles(roles)


def add_llm_connection(entry: dict) -> dict:
    """Validate, assign an id, append, and persist. Returns the stored
    connection. Raises ValueError on an unusable entry."""
    norm = _providers.normalize_connection(entry)
    if not norm["id"]:
        norm["id"] = _new_connection_id()
    err = _providers.validate_connection(norm)
    if err:
        raise ValueError(err)
    conns = get_llm_connections()
    conns.append(norm)
    _save_llm_connections(conns)
    return norm


def update_llm_connection(conn_id: str, patch: dict) -> Optional[dict]:
    """Merge `patch` into an existing connection and persist. Returns the
    updated connection, or None if no connection has that id.

    api_key semantics: a non-empty api_key in `patch` replaces the
    stored secret; an absent/empty api_key leaves it unchanged (the UI
    does not re-send the secret on a metadata-only edit). To remove a
    key, delete the connection.
    """
    conns = get_llm_connections()
    idx = next((i for i, c in enumerate(conns) if c["id"] == conn_id), None)
    if idx is None:
        return None
    merged = dict(conns[idx])
    if isinstance(patch, dict):
        for f in ("provider", "label", "model", "base_url"):
            if f in patch and patch[f] is not None:
                merged[f] = patch[f]
        new_key = patch.get("api_key")
        if isinstance(new_key, str) and new_key.strip():
            merged["api_key"] = new_key.strip()
    norm = _providers.normalize_connection(merged)
    norm["id"] = conn_id
    err = _providers.validate_connection(norm)
    if err:
        raise ValueError(err)
    conns[idx] = norm
    _save_llm_connections(conns)
    return norm


def delete_llm_connection(conn_id: str) -> bool:
    """Remove a connection and null any role that pointed at it. Returns
    False if no connection had that id."""
    conns = get_llm_connections()
    remaining = [c for c in conns if c["id"] != conn_id]
    if len(remaining) == len(conns):
        return False
    roles = get_llm_roles()
    for k in roles:
        if roles[k] == conn_id:
            roles[k] = None
    _save_llm_connections(remaining, roles)
    return True


def resolve_llm_role(role: str) -> Optional[dict]:
    """The connection assigned to a role, or None."""
    return get_llm_connection(get_llm_roles().get(role))


def resolve_llm_chain(use_case: str) -> list[dict]:
    """Ordered list of usable connections for a use case (primary first,
    then backup). 'search' falls back to the forecaster chain when no
    search role is set, so a single key still powers research. Drops
    unusable entries (no key / unknown provider) and de-dupes by id.
    """
    roles = get_llm_roles()

    def _chain_for(uc: str) -> list[dict]:
        out: list[dict] = []
        for role in _providers.USE_CASE_CHAINS.get(uc, ()):
            conn = get_llm_connection(roles.get(role))
            if conn and _providers.validate_connection(conn) is None:
                out.append(conn)
        return out

    chain = _chain_for(use_case)
    if not chain and use_case == "search":
        chain = _chain_for("forecaster")

    seen: set[str] = set()
    deduped: list[dict] = []
    for c in chain:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        deduped.append(c)
    return deduped


def has_forecaster_connection() -> bool:
    """True when at least one usable forecaster connection is wired."""
    return bool(resolve_llm_chain("forecaster"))


def has_search_connection() -> bool:
    """True when a search use-case connection resolves (incl. forecaster
    fallback)."""
    return bool(resolve_llm_chain("search"))


def has_dedicated_search_connection() -> bool:
    """True only when a search role (primary or backup) is explicitly
    wired to a usable connection.

    Distinct from has_search_connection(), which is also True when the
    search use case is merely borrowing the forecaster chain. Callers
    that run an *optional, cost-bearing* search pass (research bundle
    curation, news summarisation) use this so those passes fire only
    when the user dedicated a cheap model to search - never silently on
    the expensive forecaster model. Mirrors the old "skip unless a
    Gemini key is set" behaviour after the legacy->connections
    migration.
    """
    roles = get_llm_roles()
    for role in ("search_primary", "search_backup"):
        conn = get_llm_connection(roles.get(role))
        if conn and _providers.validate_connection(conn) is None:
            return True
    return False


def _upsert_role_connection(
    role: str, provider: str, value: Optional[str],
) -> None:
    """Back-compat helper for the legacy single-key setters. Maps a bare
    api-key write onto the connection model: updates the key in the
    connection currently assigned to `role`, or creates one and assigns
    it. An empty value deletes the connection in that slot."""
    v = (value or "").strip()
    cid = get_llm_roles().get(role)
    if not v:
        if cid:
            delete_llm_connection(cid)
        return
    if cid and get_llm_connection(cid):
        update_llm_connection(cid, {"api_key": v})
        return
    conn = add_llm_connection({"provider": provider, "api_key": v})
    set_llm_role(role, conn["id"])


# ── LLM API keys (legacy resolvers over the connection model) ───────────────
# Kept under their original names because ~6 call sites import them. They now
# resolve against llm_connections/llm_roles instead of the retired flat keys.
def get_anthropic_api_key() -> Optional[str]:
    """An Anthropic key if one powers the forecaster (or any connection),
    so main.py can seed ANTHROPIC_API_KEY for back-compat readers. Falls
    back to the env var for an externally-provided key."""
    for conn in resolve_llm_chain("forecaster") + get_llm_connections():
        if (_providers.provider_kind(conn.get("provider")) == "anthropic"
                and conn.get("api_key")):
            return conn["api_key"]
    import os
    return os.environ.get("ANTHROPIC_API_KEY") or None


def set_anthropic_api_key(value: Optional[str]) -> None:
    """Back-compat: upsert the forecaster-primary slot as an Anthropic
    connection. New code uses add/update_llm_connection + set_llm_role."""
    _upsert_role_connection("forecaster_primary", "anthropic", value)


def get_llm_backup_key() -> Optional[str]:
    """The forecaster-backup connection's key, or None."""
    conn = resolve_llm_role("forecaster_backup")
    return (conn.get("api_key") if conn else None) or None


def set_llm_backup_key(value: Optional[str]) -> None:
    """Back-compat: upsert the forecaster-backup slot, preserving its
    provider if one is already assigned (default Anthropic)."""
    conn = resolve_llm_role("forecaster_backup")
    provider = conn["provider"] if conn else "anthropic"
    _upsert_role_connection("forecaster_backup", provider, value)


# ── Optional research feed keys ─────────────────────────────────────────────
def get_newsapi_key() -> Optional[str]:
    v = _keyring_get(KEYRING_NEWSAPI_KEY)
    if v:
        return v
    import os
    return os.environ.get("NEWS_API_KEY") or os.environ.get("NEWSAPI_KEY") or None


def set_newsapi_key(value: Optional[str]) -> None:
    _keyring_set(KEYRING_NEWSAPI_KEY, value)


def get_cryptopanic_key() -> Optional[str]:
    v = _keyring_get(KEYRING_CRYPTOPANIC_KEY)
    if v:
        return v
    import os
    return os.environ.get("CRYPTOPANIC_API_KEY") or None


def set_cryptopanic_key(value: Optional[str]) -> None:
    _keyring_set(KEYRING_CRYPTOPANIC_KEY, value)


def get_gemini_api_key() -> Optional[str]:
    """A Gemini key if one is wired for the search use case (or any
    connection). research/fetcher.py + feeds/news_feed.py now route
    through the search role, but this resolver stays for the
    has_gemini_key boolean and env back-compat."""
    for conn in resolve_llm_chain("search") + get_llm_connections():
        if (_providers.provider_kind(conn.get("provider")) == "gemini"
                and conn.get("api_key")):
            return conn["api_key"]
    import os
    return os.environ.get("GEMINI_API_KEY") or None


def set_gemini_api_key(value: Optional[str]) -> None:
    """Back-compat: upsert the search-primary slot as a Gemini
    connection."""
    _upsert_role_connection("search_primary", "gemini", value)


def get_polymarket_api_creds() -> Optional[dict]:
    """Manual Polymarket CLOB api credentials (api_key, api_secret,
    api_passphrase). Returns the triplet ONLY if all three are
    populated; partial creds are no-ops because all three are
    required for a valid CLOB authed call.

    pm_executor + polymarket_wallet use this to skip the SDK's
    create_or_derive_api_key flow entirely — useful when the
    auto-derive returns a stale post-migration key.
    """
    k = _keyring_get(KEYRING_POLYMARKET_API_KEY)
    s = _keyring_get(KEYRING_POLYMARKET_API_SECRET)
    p = _keyring_get(KEYRING_POLYMARKET_API_PASSPHRASE)
    if k and s and p:
        return {"api_key": k, "api_secret": s, "api_passphrase": p}
    return None


def set_polymarket_api_creds(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    api_passphrase: Optional[str] = None,
) -> None:
    """Each arg is written independently when not None. Pass None to
    leave a field unchanged (so the user can update one at a time
    from the UI without losing the others)."""
    if api_key is not None:
        _keyring_set(KEYRING_POLYMARKET_API_KEY, api_key)
    if api_secret is not None:
        _keyring_set(KEYRING_POLYMARKET_API_SECRET, api_secret)
    if api_passphrase is not None:
        _keyring_set(KEYRING_POLYMARKET_API_PASSPHRASE, api_passphrase)


def get_polymarket_relayer_api_key() -> Optional[str]:
    """The single-UUID Relayer API Key from polymarket.com Settings.
    Powers gasless redemption via the relayer's simple 2-header auth
    (RELAYER_API_KEY + RELAYER_API_KEY_ADDRESS). The address half is
    derived from the user's private key at call time.
    """
    v = _keyring_get(KEYRING_POLYMARKET_RELAYER_API_KEY)
    if v and v.strip():
        return v.strip()
    return None


def set_polymarket_relayer_api_key(value: Optional[str]) -> None:
    """Write or clear the Relayer API Key. Empty string clears it
    so the user can disable gasless redeem if they want to fall back
    to direct-RPC."""
    _keyring_set(KEYRING_POLYMARKET_RELAYER_API_KEY, value)



# ── License (offline Ed25519 hard gate) ────────────────────────────────────
# The desktop app will not boot past `<LicenseGate>` until a signed
# license blob has been pasted and verified against the embedded
# Ed25519 public key (see engine/license.py). The blob lives in the
# keychain so the user can copy it back out; the small JSON meta
# (verified payload + activation timestamp) lives in the data
# directory. Re-verification happens on every /api/license/status
# call - the crypto check is sub-millisecond and there is no online
# round-trip to amortise.
def get_license_key() -> Optional[str]:
    return _keyring_get(KEYRING_LICENSE_KEY)


def set_license_key(value: Optional[str]) -> None:
    _keyring_set(KEYRING_LICENSE_KEY, value)


def _license_meta_path():
    """Path to the license-meta JSON file. Imported lazily because
    `db.engine.app_data_dir` pulls in the SQLAlchemy engine, and we
    don't want that as a side-effect of importing user_config."""
    from db.engine import app_data_dir
    p = app_data_dir() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p / "license_meta.json"


def get_license_meta() -> dict:
    """Read the cached validation metadata for the stored license.

    Lives in <app-data>/data/license_meta.json (NOT the keychain).
    Moved out of keychain 2026-04-29 because:
      - it's not a secret (status flag + ISO timestamp + LS instance id)
      - keeping it in keychain meant one extra macOS access prompt
        per binary rebuild (per-binary ACLs)

    Migration: if the file doesn't exist, falls back to the legacy
    keychain entry. The next `set_license_meta` write will create the
    file and clear the keychain entry, so each install pays the
    migration cost exactly once.

    Shape:
      {"status": "valid" | "invalid" | "revoked",
       "last_validated_at": "<ISO 8601 UTC>",
       "instance_id": "<string from LS>"}

    Returns an empty dict when no license has been activated yet, or
    when the stored JSON is corrupted (treated as "never validated").
    """
    try:
        path = _license_meta_path()
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[user_config] license_meta file read failed: {exc}",
              file=sys.stderr)

    # Legacy fallback: keychain. Only hit on the FIRST read after
    # upgrade; subsequent set_license_meta wipes the keychain entry.
    raw = _keyring_get(KEYRING_LICENSE_META)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def set_license_meta(meta: Optional[dict]) -> None:
    """Persist license meta to the JSON file. Clearing (meta=None)
    removes both the file and the legacy keychain entry."""
    try:
        path = _license_meta_path()
        if meta is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            # Atomic write so a crash mid-write can't corrupt the file.
            import tempfile
            import os as _os
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=".license_meta.",
                suffix=".json",
            )
            try:
                with _os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(meta, f)
                _os.chmod(tmp, 0o600)  # owner-only readable
                _os.replace(tmp, str(path))
            except Exception:
                try:
                    _os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception as exc:
        print(f"[user_config] license_meta file write failed: {exc}",
              file=sys.stderr)

    # Always clear the legacy keychain entry. Cheap on a clean install
    # (the entry doesn't exist, _keyring_set noops); on upgrade this
    # purges the legacy copy so the next get_license_meta short-circuits
    # at the file read and never touches keychain again.
    try:
        _keyring_set(KEYRING_LICENSE_META, None)
    except Exception:
        pass


# ── Telegram (outbound notifications) ───────────────────────────────────────
# The bot token (a secret, format `123456:AA...` from @BotFather) lives
# in the OS keychain. The chat_id (a numeric string identifying where
# to send) lives in user_config because it's not sensitive on its own
# and the dashboard reads it back to render the connection state.
def get_telegram_bot_token() -> Optional[str]:
    return _keyring_get(KEYRING_TELEGRAM_TOKEN)


def set_telegram_bot_token(value: Optional[str]) -> None:
    _keyring_set(KEYRING_TELEGRAM_TOKEN, value)


def get_user_telegram_config(user_id: str = DEFAULT_USER_ID) -> dict:
    """Return {'bot_token_configured', 'chat_id'} for the dashboard.

    The token itself is never returned; only whether one is set. This
    keeps the secret out of any HTTP response and matches how the
    Polymarket private-key getter works.
    """
    cfg = get_user_config(user_id)
    return {
        "bot_token_configured": bool(get_telegram_bot_token()),
        "chat_id":              cfg.telegram_chat_id,
    }


def set_user_telegram_config(
    user_id: str = DEFAULT_USER_ID,
    *,
    bot_token: Optional[str] = None,
    chat_id:   Optional[str] = None,
    clear:     bool = False,
) -> dict:
    """Persist a Telegram config update.

    Args:
      bot_token: new bot token; pass None to leave the existing value
        untouched. Empty string clears.
      chat_id:   new chat id; pass None to leave existing value
        untouched. Empty string clears.
      clear:     when True, wipes both regardless of the other args.
        Used by the disconnect flow.
    """
    if clear:
        _keyring_set(KEYRING_TELEGRAM_TOKEN, None)
        update_user_config(user_id, telegram_chat_id=None)
        return get_user_telegram_config(user_id)

    if bot_token is not None:
        token = bot_token.strip() or None
        _keyring_set(KEYRING_TELEGRAM_TOKEN, token)
    if chat_id is not None:
        cid = chat_id.strip() or None
        update_user_config(user_id, telegram_chat_id=cid)

    return get_user_telegram_config(user_id)


# ── Onboarding ──────────────────────────────────────────────────────────────
def complete_user_onboarding(
    user_id: str = DEFAULT_USER_ID,
    *,
    mode: str,
    starting_cash: float,
    wallet_address: Optional[str] = None,
) -> UserConfig:
    """Mark the user as onboarded. Called once from the dashboard onboarding flow."""
    if mode not in ("simulation", "live"):
        raise ValueError("mode must be 'simulation' or 'live'")
    payload: dict = {
        "mode":          mode,
        "starting_cash": float(starting_cash),
    }
    if wallet_address is not None:
        payload["wallet_address"] = wallet_address
    return update_user_config(user_id, **payload)


def list_onboarded_user_ids() -> list[str]:
    """Single-user app: returns ['local'] iff onboarded, else []."""
    cfg = get_user_config(DEFAULT_USER_ID)
    return [DEFAULT_USER_ID] if cfg.is_onboarded else []


def get_user_join_time(user_id: str = DEFAULT_USER_ID) -> Optional[datetime]:
    """Return user_config.created_at for visibility filtering."""
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT created_at FROM user_config WHERE user_id = :uid"
            ), {"uid": user_id}).fetchone()
        if row is None or row[0] is None:
            return None
        v = row[0]
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v))
        except Exception:
            return None
    except Exception:
        return None


def get_default_user_config() -> UserConfig:
    """Convenience for tests and the onboarding form preview."""
    return UserConfig()


# ── Notification prefs ──────────────────────────────────────────────────────
def should_notify(user_id: str = DEFAULT_USER_ID, category: str = "") -> bool:
    """Whether `category` is enabled for `user_id`.

    Categories not present in `notification_prefs` default to True, so a
    fresh install gets every notification until the user opts out. An
    unknown category returns True too (defence-in-depth: callers ought
    to use NOTIFICATION_CATEGORIES, but don't punish them for typos).
    Returns False only when the user has explicitly toggled the
    category off.
    """
    if not category:
        return True
    cfg = get_user_config(user_id)
    prefs = cfg.notification_prefs or {}
    return bool(prefs.get(category, True))


# ── Admin (single-user app: the user is always 'admin') ─────────────────────
def is_admin(user_id: str = DEFAULT_USER_ID) -> bool:
    return user_id == DEFAULT_USER_ID

