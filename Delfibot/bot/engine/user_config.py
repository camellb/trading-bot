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


@dataclass
class UserConfig:
    # V1 sizer (locked 2026-04-27): side = market favourite, single
    # Delfi-disagreement skip gate, flat archetype-multiplied stake. The
    # V0 fields min_p_win / confidence_full_stake /
    # confidence_override_threshold were removed when V1 shipped.
    base_stake_pct:         float = 0.02
    max_stake_pct:          float = 0.05

    # Circuit breakers.
    daily_loss_limit_pct:   float = 0.10
    weekly_loss_limit_pct:  float = 0.20
    drawdown_halt_pct:      float = 0.40
    streak_cooldown_losses: int   = 3
    dry_powder_reserve_pct: float = 0.20

    # Diagnostic-driven overrides.
    cost_assumption_override: Optional[float]   = None
    archetype_skip_list:      Tuple[str, ...]   = field(default_factory=tuple)
    archetype_stake_multipliers: Dict[str, float] = field(default_factory=dict)

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
        return self.mode is not None and self.starting_cash is not None

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
    "base_stake_pct":                (0.005, 0.05),
    "max_stake_pct":                 (0.01, 0.10),
    "daily_loss_limit_pct":          (0.01, 1.00),
    "weekly_loss_limit_pct":         (0.01, 1.00),
    "drawdown_halt_pct":             (0.01, 1.00),
    "streak_cooldown_losses":        (2, 10),
    "dry_powder_reserve_pct":        (0.10, 0.40),
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
USER_CONFIG_DICT_FIELDS: Tuple[str, ...] = ("archetype_stake_multipliers",)
USER_CONFIG_BOOL_DICT_FIELDS: Tuple[str, ...] = ("notification_prefs",)
USER_CONFIG_NULLABLE_FIELDS: Tuple[str, ...] = (
    "cost_assumption_override",
    "min_days_to_resolution",
    "max_days_to_resolution",
)

NOTIFICATION_CATEGORIES: Tuple[str, ...] = (
    "position_opened",
    "position_settled",
    "daily_summary",
    "weekly_summary",
    "calibration",
    "risk_event",
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
)
V1_DEFAULT_ARCHETYPE_STAKE_MULTIPLIERS: Dict[str, float] = {
    "basketball": 1.5,
    "tennis":     0.5,
}

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
    "mode",
    "starting_cash",
    "wallet_address",
    "bot_enabled",
    "notification_prefs",
    "telegram_chat_id",
    "min_days_to_resolution",
    "max_days_to_resolution",
    "archetype_skip_market_price_bands",
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
    if key in ("mode", "wallet_address", "bot_enabled", "telegram_chat_id"):
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
    return clean


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
                "       archetype_skip_market_price_bands "
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
    legacy keychain entry too so a stale value can't override the file."""
    secrets = _read_secrets()
    if value is None or value == "":
        secrets.pop(key, None)
    else:
        secrets[key] = value
    try:
        _write_secrets(secrets)
    except Exception as exc:
        print(f"[user_config] secrets file write failed: {exc}",
              file=sys.stderr)
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


# ── LLM API keys ────────────────────────────────────────────────────────────
# `get_anthropic_api_key` / `set_anthropic_api_key` are kept under their
# original names because that's what every existing caller imports. The UI
# surfaces this as "LLM API key" (primary). The backup helpers below are
# optional - they're stored regardless of provider so when the multi-LLM
# router lands they can be used for failover without further migration.
def get_anthropic_api_key() -> Optional[str]:
    """Read primary LLM key from keychain first; fall back to env var."""
    v = _keyring_get(KEYRING_ANTHROPIC_KEY)
    if v:
        return v
    import os
    return os.environ.get("ANTHROPIC_API_KEY") or None


def set_anthropic_api_key(value: Optional[str]) -> None:
    _keyring_set(KEYRING_ANTHROPIC_KEY, value)


def get_llm_backup_key() -> Optional[str]:
    """Read backup LLM key from keychain. None if user hasn't set one."""
    return _keyring_get(KEYRING_LLM_BACKUP_KEY) or None


def set_llm_backup_key(value: Optional[str]) -> None:
    _keyring_set(KEYRING_LLM_BACKUP_KEY, value)


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


