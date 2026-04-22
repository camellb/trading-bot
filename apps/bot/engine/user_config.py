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

import sys
from dataclasses import dataclass, asdict, field
from typing import Optional, Tuple, Union


DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"


@dataclass
class UserConfig:
    # Sizer thresholds — two gates + confidence softener.
    # Gate 1: direction / side selection (never skips).
    # Gate 2: minimum p_win on the chosen side.
    # The prior Gate 3 (minimum expected return) was removed as a doctrine
    # violation: it skipped heavy favourites where the math still favoured
    # taking the bet. Side selection + p_win floor is the full skip logic.
    min_p_win:                      float = 0.50
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
    # Default skip list: short-horizon tennis archetypes. Resolution data
    # showed these categories lose money at the forecaster's current
    # calibration; users can remove entries via the Risk-controls UI.
    archetype_skip_list:      Tuple[str, ...]   = field(
        default_factory=lambda: ("tennis_qualifier", "tennis_lower_tier")
    )

    def to_dict(self) -> dict:
        return asdict(self)


# (min_inclusive, max_inclusive) — enforced on every write via the dashboard.
# Diagnostic-driven overrides use None to mean "unset"; bounds only apply
# when a concrete value is supplied.
USER_CONFIG_BOUNDS: dict[str, Tuple[float, float]] = {
    "min_p_win":                     (0.50, 0.90),
    "confidence_full_stake":         (0.50, 0.90),
    "confidence_override_threshold": (0.60, 0.95),
    "base_stake_pct":                (0.005, 0.05),
    "max_stake_pct":                 (0.01, 0.10),
    "daily_loss_limit_pct":          (0.05, 0.25),
    "weekly_loss_limit_pct":         (0.10, 0.40),
    "drawdown_halt_pct":             (0.20, 0.60),
    "streak_cooldown_losses":        (2, 10),
    "dry_powder_reserve_pct":        (0.10, 0.40),
    "cost_assumption_override":      (0.0, 0.10),
}

# Fields whose concrete values are collections of archetype labels,
# not numerics. `None` / empty means "no override"; bounds do not apply.
USER_CONFIG_LIST_FIELDS: Tuple[str, ...] = (
    "archetype_skip_list",
)

# Fields whose numeric values may legally be `None` (unset).
USER_CONFIG_NULLABLE_FIELDS: Tuple[str, ...] = (
    "cost_assumption_override",
)


# Inline explanations rendered alongside each field on the dashboard.
USER_CONFIG_DESCRIPTIONS: dict[str, str] = {
    "min_p_win":
        "Minimum probability the chosen side must have to take a bet. "
        "Side is the side Claude's forecast favors; p_win is Claude's "
        "probability for that side. Higher values filter out low-conviction "
        "bets even when direction agrees with the market.",
    "confidence_full_stake":
        "Confidence at which the sizer applies the full configured stake. "
        "At confidence 0 the multiplier is 1%; it scales linearly to 100% "
        "at this threshold, then holds at 100% above. The softener never "
        "skips — only shrinks size when confidence is low.",
    "confidence_override_threshold":
        "Confidence at or above which the sizer ignores the market and "
        "follows Claude's forecast directly. Below this value the side is "
        "picked by the mean of Claude's probability and the market's "
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
}


# Type caster per field — applied when accepting updates from the dashboard.
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
}


def cast_value(key: str, raw) -> Union[int, float, tuple, None]:
    """Cast a raw dashboard value to the field's expected type.

    Nullable numeric fields accept None / "" / "null" as "unset". List fields
    accept tuple/list/comma-separated string and return a tuple of stripped
    non-empty strings.
    """
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


def validate_user_config_value(key: str, value) -> None:
    """Raise ValueError if key is unknown or value is out of bounds.

    List fields accept any tuple of strings; nullable numeric fields accept
    None. Everything else must be within its (min, max) bounds.
    """
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
    Idempotent — safe to call on every startup.
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
    """
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
                "       min_p_win, "
                "       confidence_full_stake, confidence_override_threshold "
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
                min_p_win                     = float(row[9]),
                confidence_full_stake         = float(row[10]),
                confidence_override_threshold = float(row[11]),
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
    Validate and apply field updates for a user. Atomic on the DB side —
    either every change lands or none do.
    """
    if not changes:
        return get_user_config(user_id)

    # Validate upfront (cast + bounds) — fail before touching the DB.
    clean: dict = {}
    for key, raw in changes.items():
        value = cast_value(key, raw)
        validate_user_config_value(key, value)
        clean[key] = value

    set_parts = ", ".join(f"{k} = :{k}" for k in clean)
    params: dict = {}
    for k, v in clean.items():
        # List-typed fields persist as CSV TEXT columns; tuples must be
        # flattened before hitting SQLAlchemy's text() parameter binding.
        params[k] = _encode_csv(v) if k in USER_CONFIG_LIST_FIELDS else v
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


# ── Legacy alias ────────────────────────────────────────────────────────────
def get_default_user_config() -> UserConfig:
    """
    Back-compat alias for call sites that don't yet pass a user_id.
    Prefer `get_user_config(user_id)` in new code.
    """
    return get_user_config(DEFAULT_USER_ID)
