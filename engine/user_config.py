"""
Per-user risk configuration.

Each user configures their own risk tolerance within system-defined bounds.
The sizer and risk manager read from a UserConfig at decision time, never
from global config.

Phase 1 ships this as an in-memory default. Phase 2 persists it to a
user_config table and wires the dashboard to edit it.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Tuple


@dataclass
class UserConfig:
    # EV threshold — skip any side whose ev is below this.
    min_ev_threshold:       float = 0.03   # 3%

    # Stake sizing as fraction of bankroll.
    base_stake_pct:         float = 0.02   # 2%
    max_stake_pct:          float = 0.05   # 5%

    # Circuit breakers (Phase 2 wires these into risk manager).
    daily_loss_limit_pct:   float = 0.10
    weekly_loss_limit_pct:  float = 0.20
    drawdown_halt_pct:      float = 0.40
    streak_cooldown_losses: int   = 3
    dry_powder_reserve_pct: float = 0.20

    def to_dict(self) -> dict:
        return asdict(self)


# Bounds enforced when users edit their config via the dashboard (Phase 2).
# (min_inclusive, max_inclusive)
USER_CONFIG_BOUNDS: dict[str, Tuple[float, float]] = {
    "min_ev_threshold":       (0.01, 0.10),
    "base_stake_pct":         (0.005, 0.05),
    "max_stake_pct":          (0.01, 0.10),
    "daily_loss_limit_pct":   (0.05, 0.25),
    "weekly_loss_limit_pct":  (0.10, 0.40),
    "drawdown_halt_pct":      (0.20, 0.60),
    "streak_cooldown_losses": (2, 10),
    "dry_powder_reserve_pct": (0.10, 0.40),
}


def get_default_user_config() -> UserConfig:
    """Return the default UserConfig. Phase 2 replaces with DB-backed lookup."""
    return UserConfig()
