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
set_user_polymarket_creds, set_user_venue,
get_user_polymarket_us_creds, set_user_polymarket_us_creds.

Polymarket-US helpers are no-op stubs (US venue is deferred). Telegram
helpers were removed entirely - notifications now flow through the
SQLite event_log table the dashboard reads.
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
KEYRING_ANTHROPIC_KEY = "anthropic_api_key"


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

    # Execution state.
    mode:                  Optional[str]   = None    # 'simulation' | 'live'
    starting_cash:         Optional[float] = None
    wallet_address:        Optional[str]   = None
    bot_enabled:           bool            = False

    # Local v1 has no US venue. These dataclass fields stay so legacy
    # callers (learning_cadence, dashboards, executor backstops) don't
    # blow up at import. The persistence layer ignores writes to them.
    venue:                    str           = "polymarket"
    polymarket_api_key:       Optional[str] = None
    polymarket_api_secret:    Optional[str] = None
    polymarket_passphrase:    Optional[str] = None
    polymarket_us_api_key:    Optional[str] = None
    polymarket_us_api_secret: Optional[str] = None
    polymarket_us_passphrase: Optional[str] = None
    notification_prefs:       Dict[str, bool] = field(default_factory=dict)

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
}

USER_CONFIG_LIST_FIELDS: Tuple[str, ...] = ("archetype_skip_list",)
USER_CONFIG_DICT_FIELDS: Tuple[str, ...] = ("archetype_stake_multipliers",)
USER_CONFIG_BOOL_DICT_FIELDS: Tuple[str, ...] = ("notification_prefs",)
USER_CONFIG_NULLABLE_FIELDS: Tuple[str, ...] = ("cost_assumption_override",)

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
}

# Persistable subset. Anything not here is silently dropped on update so
# stale Telegram / venue / US-cred / V0-sizer writes from transferred
# modules don't poison the SQLite schema.
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
    if key in USER_CONFIG_NULLABLE_FIELDS and _is_unset(raw):
        return None
    if key == "mode":
        if raw not in ("simulation", "live"):
            raise ValueError("mode must be 'simulation' or 'live'")
        return raw
    if key == "wallet_address":
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
    if key in USER_CONFIG_NULLABLE_FIELDS and value is None:
        return
    if key in ("mode", "wallet_address", "bot_enabled"):
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
    return clean


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
    except Exception as exc:
        print(f"[user_config] ensure_default failed: {exc}", file=sys.stderr)


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
                "       bot_enabled, archetype_stake_multipliers "
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

    json_fields = set(USER_CONFIG_DICT_FIELDS)
    set_parts = ", ".join(f"{k} = :{k}" for k in clean)

    params: dict = {}
    for k, v in clean.items():
        if k in USER_CONFIG_LIST_FIELDS:
            params[k] = _encode_csv(v)
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


# ── OS keychain helpers ─────────────────────────────────────────────────────
def _keyring_get(key: str) -> Optional[str]:
    try:
        import keyring
        v = keyring.get_password(KEYRING_SERVICE, key)
        return v or None
    except Exception as exc:
        print(f"[user_config] keyring get({key}) failed: {exc}", file=sys.stderr)
        return None


def _keyring_set(key: str, value: Optional[str]) -> None:
    try:
        import keyring
        if value is None or value == "":
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except Exception:
                pass
            return
        keyring.set_password(KEYRING_SERVICE, key, value)
    except Exception as exc:
        print(f"[user_config] keyring set({key}) failed: {exc}", file=sys.stderr)


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


# ── Anthropic API key ───────────────────────────────────────────────────────
def get_anthropic_api_key() -> Optional[str]:
    """Read from keychain first; fall back to env var (dev / sidecar override)."""
    v = _keyring_get(KEYRING_ANTHROPIC_KEY)
    if v:
        return v
    import os
    return os.environ.get("ANTHROPIC_API_KEY") or None


def set_anthropic_api_key(value: Optional[str]) -> None:
    _keyring_set(KEYRING_ANTHROPIC_KEY, value)


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
    """Stub for legacy callers that still gate sends through this hook.

    Local v1 emits every notification to the SQLite event_log; the
    dashboard does its own per-category filtering on read.
    """
    return True


# ── Admin (single-user app: the user is always 'admin') ─────────────────────
def is_admin(user_id: str = DEFAULT_USER_ID) -> bool:
    return user_id == DEFAULT_USER_ID


# ── Polymarket-US + venue (deferred / no-op stubs) ──────────────────────────
def set_user_venue(user_id: str = DEFAULT_USER_ID, venue: str = "polymarket") -> None:
    return None


def get_user_polymarket_us_creds(user_id: str = DEFAULT_USER_ID) -> dict:
    return {"api_key": None, "api_secret": None, "passphrase": None}


def set_user_polymarket_us_creds(
    user_id: str = DEFAULT_USER_ID,
    *,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    passphrase: Optional[str] = None,
) -> None:
    return None
