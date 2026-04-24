"""
Per-user risk configuration.

Each user configures their own risk tolerance within system-defined bounds.
The sizer and risk manager read from a UserConfig at decision time, never
from the global config module. Changes made through the dashboard take
effect on the next evaluation.

The `user_config` table stores one row per user_id. For now the bot runs in
single-user simulation mode and uses user_id='default'; the structure supports
multi-user and that is intentional.

DB failures fall through to the dataclass defaults so tests and offline
scripts keep working without a DATABASE_URL.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict, field
from typing import Dict, Optional, Tuple, Union


DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"


@dataclass
class UserConfig:
    # Sizer thresholds - two gates + confidence softener.
    # Gate 1: direction / side selection (never skips).
    # Gate 2: minimum p_win on the chosen side.
    # The prior Gate 3 (minimum expected return) was removed as a doctrine
    # violation: it skipped heavy favourites where the math still favoured
    # taking the bet. Side selection + p_win floor is the full skip logic.
    min_p_win:                      float = 0.55
    confidence_full_stake:          float = 0.70
    confidence_override_threshold:  float = 0.75

    base_stake_pct:         float = 0.02
    max_stake_pct:          float = 0.05

    # Circuit breakers.
    daily_loss_limit_pct:   float = 0.10
    weekly_loss_limit_pct:  float = 0.20
    drawdown_halt_pct:      float = 0.40
    streak_cooldown_losses: int   = 3
    dry_powder_reserve_pct: float = 0.20

    # Diagnostic-driven overrides (populated by learning cadence proposals).
    # None means "use the sizer default".
    cost_assumption_override: Optional[float]   = None
    # No archetypes blocked by default - the learning cadence populates
    # this list per-user based on resolved-trade evidence. Users can also
    # edit it manually via the Risk-controls UI.
    archetype_skip_list:      Tuple[str, ...]   = field(default_factory=tuple)
    # Per-archetype stake multiplier, applied after the confidence softener
    # in pm_sizer. Missing keys default to 1.0 (no adjustment). Each value
    # is clamped to [0.1, 10.0] on write. Populated by the learning cadence
    # once an archetype has >=25 settled trades of evidence.
    archetype_stake_multipliers: Dict[str, float] = field(default_factory=dict)

    # Per-user execution state (SaaS multi-tenancy).
    # All four default to None for brand-new users who haven't onboarded.
    # The bot refuses to trade for a user whose mode or starting_cash is
    # None; live mode additionally requires the credential set matching
    # this user's venue (see can_trade_live below).
    mode:                  Optional[str]   = None    # 'simulation' | 'live'
    starting_cash:         Optional[float] = None    # USD, per-user bankroll seed
    polymarket_api_key:    Optional[str]   = None
    polymarket_api_secret: Optional[str]   = None
    polymarket_passphrase: Optional[str]   = None
    wallet_address:        Optional[str]   = None

    # Multi-venue support (migration 024). Each user picks exactly one
    # venue at onboarding and can change it from the dashboard connections
    # page. 'polymarket' (offshore Polymarket.com, USDC on Polygon, EIP-712
    # signing) or 'polymarket_us' (CFTC-regulated DCM, USD, API-key signing).
    # Legacy rows default to 'polymarket' so pre-migration users keep
    # behaviour. Polymarket US credentials are independent of the offshore
    # set so switching venues doesn't stomp the other side's keys.
    venue:                    str           = "polymarket"
    polymarket_us_api_key:    Optional[str] = None
    polymarket_us_api_secret: Optional[str] = None
    polymarket_us_passphrase: Optional[str] = None

    # Per-user bot on/off switch. Defaults to False so newly onboarded users
    # land on the dashboard with no automated trades. User clicks "Start bot"
    # to flip this to True.
    bot_enabled:           bool            = False

    # Per-category Telegram notification preferences. Missing keys default
    # to True (send it). Users opt out of individual categories via the
    # notifications settings page; everything they haven't touched keeps
    # the previous always-on behaviour.
    notification_prefs:    Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_onboarded(self) -> bool:
        """True iff the user has picked mode + starting bankroll."""
        return self.mode is not None and self.starting_cash is not None

    @property
    def can_trade_live(self) -> bool:
        """True iff mode='live' AND the credentials for this user's venue are set.

        Offshore venue ('polymarket'): requires polymarket_api_key +
        polymarket_api_secret + wallet_address (passphrase optional).

        US venue ('polymarket_us'): requires polymarket_us_api_key +
        polymarket_us_api_secret (passphrase optional, no wallet - DCM
        settles off-chain in USD).

        Unknown venue values return False - the CHECK constraint on
        user_config.venue prevents writes, but a stale or hand-edited
        row must still refuse live trading rather than pick a wrong
        credential set.
        """
        if self.mode != "live":
            return False
        if self.venue == "polymarket":
            return bool(
                self.polymarket_api_key
                and self.polymarket_api_secret
                and self.wallet_address
            )
        if self.venue == "polymarket_us":
            return bool(
                self.polymarket_us_api_key
                and self.polymarket_us_api_secret
            )
        return False

    @property
    def ready_to_trade(self) -> bool:
        """Bot may act for this user."""
        if not self.is_onboarded:
            return False
        if not self.bot_enabled:
            return False
        if self.mode == "simulation":
            return True
        return self.can_trade_live


# (min_inclusive, max_inclusive) - enforced on every write via the dashboard.
# Diagnostic-driven overrides use None to mean "unset"; bounds only apply
# when a concrete value is supplied.
USER_CONFIG_BOUNDS: dict[str, Tuple[float, float]] = {
    "min_p_win":                     (0.50, 0.90),
    "confidence_full_stake":         (0.50, 0.90),
    "confidence_override_threshold": (0.60, 0.95),
    "base_stake_pct":                (0.005, 0.05),
    "max_stake_pct":                 (0.01, 0.10),
    # Loss caps and drawdown halt accept the full 0-100% range so users
    # can set whatever envelope fits their risk tolerance. Defaults are
    # still conservative; these ranges just enable flexibility.
    "daily_loss_limit_pct":          (0.01, 1.00),
    "weekly_loss_limit_pct":         (0.01, 1.00),
    "drawdown_halt_pct":             (0.01, 1.00),
    "streak_cooldown_losses":        (2, 10),
    "dry_powder_reserve_pct":        (0.10, 0.40),
    "cost_assumption_override":      (0.0, 0.10),
    "starting_cash":                 (10.0, 100_000.0),
}

# Fields whose concrete values are collections of archetype labels,
# not numerics. `None` / empty means "no override"; bounds do not apply.
USER_CONFIG_LIST_FIELDS: Tuple[str, ...] = (
    "archetype_skip_list",
)

# Fields whose concrete values are dicts (archetype → numeric). Each entry's
# value is clamped to ARCHETYPE_MULTIPLIER_BOUNDS on write. `None` / empty
# means "no overrides".
USER_CONFIG_DICT_FIELDS: Tuple[str, ...] = (
    "archetype_stake_multipliers",
)

# Fields whose concrete values are dicts (category → bool). Missing keys
# default to True on read (send the notification) so the pre-migration
# behaviour is preserved for rows without an override.
USER_CONFIG_BOOL_DICT_FIELDS: Tuple[str, ...] = (
    "notification_prefs",
)

# Allowed keys inside notification_prefs. Writes outside this set are
# silently dropped so a stale dashboard can't scribble arbitrary keys.
NOTIFICATION_CATEGORIES: Tuple[str, ...] = (
    "position_opened",
    "position_settled",
    "daily_summary",
    "weekly_summary",
    "calibration",
    "risk_event",
)

# Per-entry bounds for every key inside USER_CONFIG_DICT_FIELDS.
ARCHETYPE_MULTIPLIER_BOUNDS: Tuple[float, float] = (0.1, 10.0)

# Fields whose numeric values may legally be `None` (unset).
USER_CONFIG_NULLABLE_FIELDS: Tuple[str, ...] = (
    "cost_assumption_override",
)


# Inline explanations rendered alongside each field on the dashboard.
USER_CONFIG_DESCRIPTIONS: dict[str, str] = {
    "min_p_win":
        "Minimum probability the chosen side must have to take a bet. "
        "Side is the side Delfi's forecast favors; p_win is Delfi's "
        "probability for that side. Higher values filter out low-conviction "
        "bets even when direction agrees with the market.",
    "confidence_full_stake":
        "Confidence at which the sizer applies the full configured stake. "
        "At confidence 0 the multiplier is 1%; it scales linearly to 100% "
        "at this threshold, then holds at 100% above. The softener never "
        "skips - only shrinks size when confidence is low.",
    "confidence_override_threshold":
        "Confidence at or above which the sizer ignores the market and "
        "follows Delfi's forecast directly. Below this value the side is "
        "picked by the mean of Delfi's probability and the market's "
        "implied probability.",
    "base_stake_pct":
        "Baseline stake as a fraction of bankroll at full confidence, "
        "before the confidence softener and max_stake cap apply.",
    "max_stake_pct":
        "Hard cap per trade as a fraction of bankroll, regardless of "
        "confidence.",
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
        "Fraction of bankroll held in reserve and never deployed, "
        "so a bad day cannot liquidate the book.",
    "cost_assumption_override":
        "Override the sizer's default cost assumption (spread + fees + "
        "slippage). Set when realised cost drifts above the default; leave "
        "unset to use the built-in 1.5% estimate.",
    "archetype_skip_list":
        "Archetypes the sizer will refuse to trade. Populated by the "
        "learning cadence when an archetype's Brier score exceeds the "
        "uninformed baseline over a reliable sample.",
    "archetype_stake_multipliers":
        "Per-archetype stake multiplier applied after the confidence "
        "softener. 1.0 = no adjustment, 2.0 = double-size, 0.5 = half-size. "
        "Populated by the learning cadence once an archetype has at least "
        "25 settled trades. Users can override directly from the Risk "
        "controls page. Each entry is clamped to [0.1, 10.0].",
    "notification_prefs":
        "Per-category Telegram notification preferences. Missing keys "
        "default to on. Users toggle individual categories from the "
        "notifications settings page.",
}


# Type caster per field - applied when accepting updates from the dashboard.
_CASTERS: dict[str, type] = {
    "min_p_win":                     float,
    "confidence_full_stake":         float,
    "confidence_override_threshold": float,
    "base_stake_pct":                float,
    "max_stake_pct":                 float,
    "daily_loss_limit_pct":          float,
    "weekly_loss_limit_pct":         float,
    "drawdown_halt_pct":             float,
    "streak_cooldown_losses":        int,
    "dry_powder_reserve_pct":        float,
    "cost_assumption_override":      float,
    "starting_cash":                 float,
}

# Fields that cannot be edited via the generic /api/user-config PUT path.
# Mode is changed via a dedicated endpoint (dashboard guardrails); creds
# go through /api/credentials; display_name via /api/profile. Keeping them
# out of this list prevents someone from posting `{mode: "live"}` to the
# risk-config endpoint and bypassing the credential gate.
_NON_EDITABLE_VIA_USER_CONFIG: frozenset[str] = frozenset({
    "mode",
    "polymarket_api_key",
    "polymarket_api_secret",
    "polymarket_passphrase",
    "wallet_address",
    # Venue change has to travel together with a compatible credential set -
    # routed through /api/credentials / set_user_venue so the two can't drift.
    "venue",
    "polymarket_us_api_key",
    "polymarket_us_api_secret",
    "polymarket_us_passphrase",
    "display_name",
})


def cast_value(key: str, raw) -> Union[int, float, tuple, dict, None]:
    """Cast a raw dashboard value to the field's expected type.

    Nullable numeric fields accept None / "" / "null" as "unset". List fields
    accept tuple/list/comma-separated string and return a tuple of stripped
    non-empty strings. Dict fields accept dict or JSON string and return a
    dict with stringified keys and float values clamped to the per-field
    bounds.
    """
    if key in USER_CONFIG_DICT_FIELDS:
        return _cast_archetype_multipliers(raw)
    if key in USER_CONFIG_BOOL_DICT_FIELDS:
        return _cast_notification_prefs(raw)
    if key in USER_CONFIG_LIST_FIELDS:
        return _cast_list(raw)
    if key in USER_CONFIG_NULLABLE_FIELDS and _is_unset(raw):
        return None
    if key not in _CASTERS:
        raise ValueError(f"unknown user_config field: {key}")
    try:
        return _CASTERS[key](raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be {_CASTERS[key].__name__}") from exc


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
    """Accept dict | JSON-string | None. Keys outside NOTIFICATION_CATEGORIES
    are dropped. Values are coerced to bool (truthy). Returns an empty dict
    for None/empty so 'no overrides' is an unambiguous state."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "notification_prefs must be a JSON object"
            ) from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"notification_prefs must be a dict, got {type(raw).__name__}"
        )
    allowed = set(NOTIFICATION_CATEGORIES)
    clean: Dict[str, bool] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key or key not in allowed:
            continue
        # Accept bools, 0/1, and common string variants from form posts.
        if isinstance(v, bool):
            clean[key] = v
        elif isinstance(v, (int, float)):
            clean[key] = bool(v)
        elif isinstance(v, str):
            clean[key] = v.strip().lower() in ("1", "true", "yes", "on")
        else:
            raise ValueError(
                f"notification_prefs[{key!r}] must be boolean"
            )
    return clean


def _cast_archetype_multipliers(raw) -> Dict[str, float]:
    """Accept dict | JSON-string | None. Clamp each value to bounds, drop
    entries that aren't parseable floats or have empty archetype keys."""
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


def validate_user_config_value(key: str, value) -> None:
    """Raise ValueError if key is unknown or value is out of bounds.

    List fields accept any tuple of strings; dict fields accept any dict with
    numeric values already clamped by the caster; nullable numeric fields
    accept None. Everything else must be within its (min, max) bounds.
    """
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
                raise ValueError(
                    f"{key}[{k!r}]={v} outside bounds [{lo}, {hi}]"
                )
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
    if key in USER_CONFIG_NULLABLE_FIELDS and value is None:
        return
    if key not in USER_CONFIG_BOUNDS:
        raise ValueError(f"unknown user_config field: {key}")
    lo, hi = USER_CONFIG_BOUNDS[key]
    if value < lo or value > hi:
        raise ValueError(
            f"{key}={value} outside bounds [{lo}, {hi}]"
        )


def validated_update_payload(payload: dict) -> dict:
    """
    Cast each field to the correct type and validate bounds. Returns a
    dict safe to pass to update_user_config(**payload). Raises ValueError
    on any invalid key or out-of-bounds value.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    clean: dict = {}
    for key, raw in payload.items():
        value = cast_value(key, raw)
        validate_user_config_value(key, value)
        clean[key] = value
    return clean


# ── DB-backed accessors ─────────────────────────────────────────────────────
def ensure_default_user_config() -> None:
    """
    Create the default user_config row if it doesn't already exist.
    Idempotent - safe to call on every startup.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO user_config (user_id) VALUES (:uid) "
                "ON CONFLICT (user_id) DO NOTHING"
            ), {"uid": DEFAULT_USER_ID})
    except Exception as exc:
        print(f"[user_config] ensure_default failed: {exc}", file=sys.stderr)


def get_user_config(user_id: str = DEFAULT_USER_ID) -> UserConfig:
    """
    Load the user's config from the DB. On any error (missing table,
    missing row, no DATABASE_URL) returns dataclass defaults so the
    caller never has to handle failure.

    For a brand-new user with no row, returns a UserConfig where the
    per-user execution fields (mode, starting_cash, polymarket_*,
    wallet_address) are all None - callers must treat such a config as
    "not ready to trade" (see UserConfig.ready_to_trade).
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        # notification_prefs ships in migration 019. When the bot is running
        # against a DB that hasn't been migrated yet, the SELECT falls back
        # to '{}'::jsonb via COALESCE on a non-existent column - so we do
        # the fallback ourselves at the row level.
        # Column order matches the UserConfig(...) constructor below; if you
        # add or reorder a column here, update the row-decode indices too.
        # Migration 024 added venue + polymarket_us_* (columns 21-24). The
        # fallback SELECT below drops notification_prefs (migration 019) but
        # keeps the 024 columns - losing venue to the outer except would
        # route live trades to the wrong credential set.
        select_sql = (
            "SELECT base_stake_pct, max_stake_pct, "
            "       daily_loss_limit_pct, weekly_loss_limit_pct, "
            "       drawdown_halt_pct, streak_cooldown_losses, "
            "       dry_powder_reserve_pct, "
            "       cost_assumption_override, archetype_skip_list, "
            "       min_p_win, "
            "       confidence_full_stake, confidence_override_threshold, "
            "       mode, starting_cash, "
            "       polymarket_api_key, polymarket_api_secret, "
            "       polymarket_passphrase, wallet_address, "
            "       bot_enabled, archetype_stake_multipliers, "
            "       notification_prefs, "
            "       venue, "
            "       polymarket_us_api_key, polymarket_us_api_secret, "
            "       polymarket_us_passphrase "
            "FROM user_config WHERE user_id = :uid"
        )
        # Try the primary SELECT in its own transaction. If it fails because
        # a migration hasn't been applied yet (e.g. notification_prefs),
        # Postgres aborts the transaction and any follow-up query on the
        # SAME connection would also fail. Retrying in a FRESH transaction
        # keeps the fallback path usable. Losing that isolation is exactly
        # what caused a prod incident where missing migration 019 silently
        # zeroed starting_cash for every user (get_user_config's outer
        # except swallowed the aborted-transaction error and returned
        # UserConfig() defaults - starting_cash=None - so the executor
        # then served bankroll math with starting_cash=0).
        row = None
        # Column indices for the venue + US-cred block, which moves depending
        # on whether the primary (with notification_prefs) or fallback SELECT
        # returned the row. Tracked explicitly so a future SELECT reshuffle
        # doesn't silently desync the row-decode.
        prefs_idx: Optional[int]   = None
        venue_idx: int             = 20     # overwritten per branch
        us_key_idx: int            = 21
        us_secret_idx: int         = 22
        us_passphrase_idx: int     = 23
        try:
            with get_engine().begin() as conn:
                row = conn.execute(text(select_sql),
                                    {"uid": user_id}).fetchone()
                prefs_idx         = 20
                venue_idx         = 21
                us_key_idx        = 22
                us_secret_idx     = 23
                us_passphrase_idx = 24
        except Exception:
            # Primary SELECT failed. Retry without notification_prefs in case
            # migration 019 hasn't been applied. Venue + US creds (migration
            # 024) stay in the fallback - losing those to the outer except
            # would silently route a polymarket_us user to the offshore
            # credential path.
            with get_engine().begin() as conn:
                row = conn.execute(text(
                    "SELECT base_stake_pct, max_stake_pct, "
                    "       daily_loss_limit_pct, weekly_loss_limit_pct, "
                    "       drawdown_halt_pct, streak_cooldown_losses, "
                    "       dry_powder_reserve_pct, "
                    "       cost_assumption_override, archetype_skip_list, "
                    "       min_p_win, "
                    "       confidence_full_stake, confidence_override_threshold, "
                    "       mode, starting_cash, "
                    "       polymarket_api_key, polymarket_api_secret, "
                    "       polymarket_passphrase, wallet_address, "
                    "       bot_enabled, archetype_stake_multipliers, "
                    "       venue, "
                    "       polymarket_us_api_key, polymarket_us_api_secret, "
                    "       polymarket_us_passphrase "
                    "FROM user_config WHERE user_id = :uid"
                ), {"uid": user_id}).fetchone()
                prefs_idx         = None
                venue_idx         = 20
                us_key_idx        = 21
                us_secret_idx     = 22
                us_passphrase_idx = 23
        # Shared row-decode path: runs for both the primary and fallback
        # branches above. prefs_idx is 20 when the primary succeeded (the
        # row tuple includes notification_prefs) and None when we fell back
        # to the legacy SELECT without that column. Venue + US creds are
        # always present because both branches SELECT them.
        if row is None:
            return UserConfig()
        prefs_raw          = row[prefs_idx] if prefs_idx is not None else None
        venue_raw          = row[venue_idx]
        us_key_raw         = row[us_key_idx]
        us_secret_raw      = row[us_secret_idx]
        us_passphrase_raw  = row[us_passphrase_idx]
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
            min_p_win                     = float(row[9]),
            confidence_full_stake         = float(row[10]),
            confidence_override_threshold = float(row[11]),
            mode                          = (str(row[12]) if row[12] is not None else None),
            starting_cash                 = (float(row[13]) if row[13] is not None else None),
            polymarket_api_key            = (str(row[14]) if row[14] is not None else None),
            polymarket_api_secret         = (str(row[15]) if row[15] is not None else None),
            polymarket_passphrase         = (str(row[16]) if row[16] is not None else None),
            wallet_address                = (str(row[17]) if row[17] is not None else None),
            bot_enabled                   = bool(row[18]) if row[18] is not None else False,
            archetype_stake_multipliers   = _decode_archetype_multipliers(row[19]),
            notification_prefs            = _decode_notification_prefs(prefs_raw),
            venue                         = (str(venue_raw) if venue_raw is not None
                                              else "polymarket"),
            polymarket_us_api_key         = (str(us_key_raw)        if us_key_raw        is not None else None),
            polymarket_us_api_secret      = (str(us_secret_raw)     if us_secret_raw     is not None else None),
            polymarket_us_passphrase      = (str(us_passphrase_raw) if us_passphrase_raw is not None else None),
        )
    except Exception as exc:
        print(f"[user_config] get_user_config({user_id}) failed: {exc}",
              file=sys.stderr)
        return UserConfig()


def _decode_csv(raw) -> Tuple[str, ...]:
    if raw is None:
        return tuple()
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return tuple(s.strip() for s in str(raw).split(",") if s.strip())


def _decode_notification_prefs(raw) -> Dict[str, bool]:
    """JSONB arrives as dict; defensively accept str. Drop unknown keys so a
    stale write can't poison the read path. Empty dict == 'use defaults' which
    the should_notify() helper interprets as 'send everything'."""
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
        key = str(k).strip()
        if not key or key not in allowed:
            continue
        out[key] = bool(v)
    return out


def should_notify(user_id: str, category: str) -> bool:
    """Return True if the user wants this category delivered.

    Missing rows, missing column, DB errors, and unknown keys all return
    True - notifications default to 'on' so a broken pref layer never
    silently mutes the user.
    """
    if category not in NOTIFICATION_CATEGORIES:
        return True
    try:
        cfg = get_user_config(user_id)
    except Exception:
        return True
    prefs = cfg.notification_prefs or {}
    # Missing key == on. Explicit False == off.
    return bool(prefs.get(category, True))


def _decode_archetype_multipliers(raw) -> Dict[str, float]:
    """JSONB arrives as dict from SQLAlchemy, but a string is possible if the
    column was backfilled by hand. Silently drop malformed entries - a stale
    write should never prevent the sizer from loading the rest of the config."""
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


def _encode_csv(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value if str(x).strip()]
        return ",".join(items) if items else None
    s = str(value).strip()
    return s or None


def update_user_config(user_id: str = DEFAULT_USER_ID, **changes) -> UserConfig:
    """
    Validate and apply field updates for a user. Atomic on the DB side -
    either every change lands or none do.
    """
    if not changes:
        return get_user_config(user_id)

    # Validate upfront (cast + bounds) - fail before touching the DB.
    clean: dict = {}
    for key, raw in changes.items():
        value = cast_value(key, raw)
        validate_user_config_value(key, value)
        clean[key] = value

    # Dict-typed fields cast with an explicit `::jsonb` so the driver sends
    # the payload as a JSON string; without the cast Postgres treats it as
    # plain TEXT and the INSERT fails on JSONB columns.
    jsonb_fields = set(USER_CONFIG_DICT_FIELDS) | set(USER_CONFIG_BOOL_DICT_FIELDS)
    set_parts = ", ".join(
        (f"{k} = CAST(:{k} AS JSONB)" if k in jsonb_fields
         else f"{k} = :{k}")
        for k in clean
    )
    params: dict = {}
    for k, v in clean.items():
        if k in USER_CONFIG_LIST_FIELDS:
            # List-typed fields persist as CSV TEXT columns; tuples must be
            # flattened before hitting SQLAlchemy's text() parameter binding.
            params[k] = _encode_csv(v)
        elif k in jsonb_fields:
            params[k] = json.dumps(v or {})
        else:
            params[k] = v
    params["uid"] = user_id

    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        # Insert-then-update pattern so a missing default row auto-creates.
        conn.execute(text(
            "INSERT INTO user_config (user_id) VALUES (:uid) "
            "ON CONFLICT (user_id) DO NOTHING"
        ), {"uid": user_id})
        conn.execute(text(
            f"UPDATE user_config SET {set_parts}, updated_at = NOW() "
            f"WHERE user_id = :uid"
        ), params)

    return get_user_config(user_id)


# ── Telegram creds ──────────────────────────────────────────────────────────
# Opt-in, per-user Telegram delivery. Both columns nullable - either unset
# means the notifier silently no-ops for that user. Stored on user_config
# rather than a sidecar table so the dashboard can write with the same
# RLS policies already in place.
def get_user_telegram_creds(user_id: str) -> Optional[Tuple[str, str]]:
    """
    Return (token, chat_id) if the user has both configured, else None.
    DB errors also return None so the notifier can no-op cleanly.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT telegram_bot_token, telegram_chat_id "
                "FROM user_config WHERE user_id = :uid"
            ), {"uid": user_id}).fetchone()
        if row is None:
            return None
        token = (row[0] or "").strip()
        chat_id = (row[1] or "").strip()
        if not token or not chat_id:
            return None
        return token, chat_id
    except Exception as exc:
        print(f"[user_config] get_user_telegram_creds({user_id}) failed: {exc}",
              file=sys.stderr)
        return None


def set_user_telegram_creds(user_id: str,
                             bot_token: Optional[str],
                             chat_id:   Optional[str]) -> None:
    """
    Write telegram credentials for a user. Pass None (or empty) for either
    value to clear it - the getter requires both to be non-empty.
    Auto-creates the user_config row if it doesn't exist.
    """
    tok = (bot_token or "").strip() or None
    cid = (chat_id   or "").strip() or None
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO user_config (user_id) VALUES (:uid) "
            "ON CONFLICT (user_id) DO NOTHING"
        ), {"uid": user_id})
        conn.execute(text(
            "UPDATE user_config "
            "SET telegram_bot_token = :tok, "
            "    telegram_chat_id   = :cid, "
            "    updated_at         = NOW() "
            "WHERE user_id = :uid"
        ), {"tok": tok, "cid": cid, "uid": user_id})


def list_users_with_telegram() -> list[str]:
    """
    Return every user_id that has both bot_token and chat_id configured.
    Used for cron broadcasts (daily/weekly summary, startup notifications).
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT user_id FROM user_config "
                "WHERE COALESCE(telegram_bot_token, '') <> '' "
                "  AND COALESCE(telegram_chat_id,   '') <> ''"
            )).fetchall()
        return [str(r[0]) for r in rows]
    except Exception as exc:
        print(f"[user_config] list_users_with_telegram failed: {exc}",
              file=sys.stderr)
        return []


def list_admin_users_with_telegram() -> list[str]:
    """
    Return every admin user_id that has both bot_token and chat_id configured.
    Used for operator/process-level broadcasts (startup, restart, generic
    errors) that should not reach regular users.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT user_id FROM user_config "
                "WHERE is_admin = TRUE "
                "  AND COALESCE(telegram_bot_token, '') <> '' "
                "  AND COALESCE(telegram_chat_id,   '') <> ''"
            )).fetchall()
        return [str(r[0]) for r in rows]
    except Exception as exc:
        print(f"[user_config] list_admin_users_with_telegram failed: {exc}",
              file=sys.stderr)
        return []


# ── Polymarket creds ────────────────────────────────────────────────────────
# Per-user Polymarket API key/secret/passphrase + Polygon wallet address.
# Live-mode execution requires all three of (api_key, api_secret, wallet)
# to be non-empty; passphrase is optional (only some keys carry one).
def get_user_polymarket_creds(user_id: str) -> dict:
    """
    Return {'api_key', 'api_secret', 'passphrase', 'wallet_address'} -
    any missing value is None. Dashboard and bot both call this; the bot
    refuses live trades if any required field is empty.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT polymarket_api_key, polymarket_api_secret, "
                "       polymarket_passphrase, wallet_address "
                "FROM user_config WHERE user_id = :uid"
            ), {"uid": user_id}).fetchone()
        if row is None:
            return {"api_key": None, "api_secret": None,
                    "passphrase": None, "wallet_address": None}
        return {
            "api_key":        (str(row[0]) if row[0] else None),
            "api_secret":     (str(row[1]) if row[1] else None),
            "passphrase":     (str(row[2]) if row[2] else None),
            "wallet_address": (str(row[3]) if row[3] else None),
        }
    except Exception as exc:
        print(f"[user_config] get_user_polymarket_creds({user_id}) failed: {exc}",
              file=sys.stderr)
        return {"api_key": None, "api_secret": None,
                "passphrase": None, "wallet_address": None}


def set_user_polymarket_creds(user_id: str,
                              api_key:        Optional[str] = None,
                              api_secret:     Optional[str] = None,
                              passphrase:     Optional[str] = None,
                              wallet_address: Optional[str] = None) -> None:
    """
    Write Polymarket credentials for a user. Empty string → NULL (cleared).
    All four args are independently settable; passing None for an arg
    leaves that column untouched (unlike empty string which clears it).
    """
    updates: list[str] = []
    params: dict = {"uid": user_id}
    # `None` means "don't touch this column"; `""` means "clear it".
    for col, arg in (
        ("polymarket_api_key",    api_key),
        ("polymarket_api_secret", api_secret),
        ("polymarket_passphrase", passphrase),
        ("wallet_address",        wallet_address),
    ):
        if arg is None:
            continue
        trimmed = arg.strip() or None
        updates.append(f"{col} = :{col}")
        params[col] = trimmed
    if not updates:
        return

    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO user_config (user_id) VALUES (:uid) "
            "ON CONFLICT (user_id) DO NOTHING"
        ), {"uid": user_id})
        conn.execute(text(
            f"UPDATE user_config SET {', '.join(updates)}, updated_at = NOW() "
            f"WHERE user_id = :uid"
        ), params)


# ── Polymarket US creds ─────────────────────────────────────────────────────
# Parallel to get/set_user_polymarket_creds but for the CFTC-regulated US
# venue. Stored in separate columns so switching venues doesn't stomp the
# other side's keys - a user can onboard on offshore, try Polymarket US,
# and switch back without re-entering their offshore creds.
#
# No wallet_address: Polymarket US is a DCM and settles off-chain in USD,
# so there is nothing analogous to the Polygon wallet needed on offshore.
SUPPORTED_VENUES: Tuple[str, ...] = ("polymarket", "polymarket_us")


def get_user_polymarket_us_creds(user_id: str) -> dict:
    """
    Return {'api_key', 'api_secret', 'passphrase'} for Polymarket US, with
    any missing value as None. Mirrors get_user_polymarket_creds.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT polymarket_us_api_key, polymarket_us_api_secret, "
                "       polymarket_us_passphrase "
                "FROM user_config WHERE user_id = :uid"
            ), {"uid": user_id}).fetchone()
        if row is None:
            return {"api_key": None, "api_secret": None, "passphrase": None}
        return {
            "api_key":    (str(row[0]) if row[0] else None),
            "api_secret": (str(row[1]) if row[1] else None),
            "passphrase": (str(row[2]) if row[2] else None),
        }
    except Exception as exc:
        print(f"[user_config] get_user_polymarket_us_creds({user_id}) failed: {exc}",
              file=sys.stderr)
        return {"api_key": None, "api_secret": None, "passphrase": None}


def set_user_polymarket_us_creds(user_id: str,
                                 api_key:    Optional[str] = None,
                                 api_secret: Optional[str] = None,
                                 passphrase: Optional[str] = None) -> None:
    """
    Write Polymarket US credentials for a user. Same None-vs-empty
    semantics as set_user_polymarket_creds: None skips a column, empty
    string clears it.
    """
    updates: list[str] = []
    params: dict = {"uid": user_id}
    for col, arg in (
        ("polymarket_us_api_key",    api_key),
        ("polymarket_us_api_secret", api_secret),
        ("polymarket_us_passphrase", passphrase),
    ):
        if arg is None:
            continue
        trimmed = arg.strip() or None
        updates.append(f"{col} = :{col}")
        params[col] = trimmed
    if not updates:
        return

    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO user_config (user_id) VALUES (:uid) "
            "ON CONFLICT (user_id) DO NOTHING"
        ), {"uid": user_id})
        conn.execute(text(
            f"UPDATE user_config SET {', '.join(updates)}, updated_at = NOW() "
            f"WHERE user_id = :uid"
        ), params)


def set_user_venue(user_id: str, venue: str) -> None:
    """
    Write the user's venue selection. Validates against SUPPORTED_VENUES so
    a stale dashboard can't scribble an unknown value (the DB-side CHECK
    constraint would also reject it, but erroring here gives a cleaner
    message). Auto-creates the user_config row if missing.
    """
    if venue not in SUPPORTED_VENUES:
        raise ValueError(
            f"venue must be one of {SUPPORTED_VENUES!r}, got: {venue!r}"
        )
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO user_config (user_id) VALUES (:uid) "
            "ON CONFLICT (user_id) DO NOTHING"
        ), {"uid": user_id})
        conn.execute(text(
            "UPDATE user_config "
            "SET venue = :venue, "
            "    updated_at = NOW() "
            "WHERE user_id = :uid"
        ), {"uid": user_id, "venue": venue})


def get_active_polymarket_creds(cfg: UserConfig) -> dict:
    """
    Return the credential set matching the user's current venue, in the
    shape {'api_key', 'api_secret', 'passphrase', 'wallet_address'}.
    wallet_address is always present; for Polymarket US it's None (DCM
    settles off-chain in USD, no Polygon wallet).
    Unknown venue values return all-None so callers can treat them as
    'not configured' rather than raising.
    """
    if cfg.venue == "polymarket":
        return {
            "api_key":        cfg.polymarket_api_key,
            "api_secret":     cfg.polymarket_api_secret,
            "passphrase":     cfg.polymarket_passphrase,
            "wallet_address": cfg.wallet_address,
        }
    if cfg.venue == "polymarket_us":
        return {
            "api_key":        cfg.polymarket_us_api_key,
            "api_secret":     cfg.polymarket_us_api_secret,
            "passphrase":     cfg.polymarket_us_passphrase,
            "wallet_address": None,
        }
    return {
        "api_key": None, "api_secret": None,
        "passphrase": None, "wallet_address": None,
    }


# ── Onboarding ──────────────────────────────────────────────────────────────
# Called by the web server's completeOnboarding action AND by any bot path
# that needs to seed a brand-new row. Writes display_name + mode +
# starting_cash + onboarded_at atomically.
def complete_user_onboarding(user_id: str,
                              display_name:  str,
                              mode:          str,
                              starting_cash: float) -> None:
    """
    Finalize onboarding. Validates mode ∈ {simulation, live} and
    starting_cash > 0. Raises ValueError on invalid input.
    """
    if mode not in ("simulation", "live"):
        raise ValueError(f"mode must be 'simulation' or 'live', got: {mode!r}")
    if not display_name or len(display_name.strip()) < 2:
        raise ValueError("display_name must be at least 2 characters")
    sc = float(starting_cash)
    lo, hi = USER_CONFIG_BOUNDS["starting_cash"]
    if sc < lo or sc > hi:
        raise ValueError(f"starting_cash={sc} outside bounds [{lo}, {hi}]")

    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO user_config (user_id) VALUES (:uid) "
            "ON CONFLICT (user_id) DO NOTHING"
        ), {"uid": user_id})
        conn.execute(text(
            "UPDATE user_config "
            "SET display_name  = :name, "
            "    mode          = :mode, "
            "    starting_cash = :cash, "
            "    onboarded_at  = COALESCE(onboarded_at, NOW()), "
            "    updated_at    = NOW() "
            "WHERE user_id = :uid"
        ), {
            "uid":  user_id,
            "name": display_name.strip(),
            "mode": mode,
            "cash": sc,
        })


# ── Multi-tenant lookups ────────────────────────────────────────────────────
def list_onboarded_user_ids() -> list[str]:
    """
    Every user who has completed onboarding (onboarded_at IS NOT NULL AND
    mode IS NOT NULL AND starting_cash IS NOT NULL). Used by the scanner
    to fan out per-user sizing + execution.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT user_id FROM user_config "
                "WHERE onboarded_at IS NOT NULL "
                "  AND mode IS NOT NULL "
                "  AND starting_cash IS NOT NULL"
            )).fetchall()
        return [str(r[0]) for r in rows]
    except Exception as exc:
        print(f"[user_config] list_onboarded_user_ids failed: {exc}",
              file=sys.stderr)
        return []


def is_admin(user_id: str) -> bool:
    """Return True iff the user has user_config.is_admin = TRUE. Defaults to
    False on any DB error so a failed lookup cannot accidentally elevate."""
    if not user_id:
        return False
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT is_admin FROM user_config WHERE user_id = :uid"
            ), {"uid": user_id}).fetchone()
        return bool(row[0]) if row else False
    except Exception as exc:
        print(f"[user_config] is_admin({user_id}) failed: {exc}",
              file=sys.stderr)
        return False


def get_user_join_time(user_id: str):
    """
    Return the auth.users.created_at for a user, as a timezone-aware
    datetime. None on missing row or DB failure. Used to filter shared
    rows (market_evaluations) so users only see data from after they joined.
    """
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT created_at FROM auth.users WHERE id = :uid"
            ), {"uid": user_id}).fetchone()
        return row[0] if row else None
    except Exception as exc:
        print(f"[user_config] get_user_join_time({user_id}) failed: {exc}",
              file=sys.stderr)
        return None


# ── Legacy alias ────────────────────────────────────────────────────────────
def get_default_user_config() -> UserConfig:
    """
    Back-compat alias for call sites that don't yet pass a user_id.
    Prefer `get_user_config(user_id)` in new code.
    """
    return get_user_config(DEFAULT_USER_ID)
