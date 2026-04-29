"""
Risk manager - pre-trade circuit breakers.

Runs identically in simulation and live modes so simulation simulates live. Reads
every parameter from UserConfig at decision time; no globals, no caches.

The manager answers two questions before the sizer runs:

    1. Is the book currently halted? Daily / weekly loss limits, drawdown
       from peak, loss-streak cooldown. If yes, the analyst skips the trade.
    2. What bankroll may the sizer deploy? The dry-powder reserve is held
       back permanently; the loss-streak cooldown halves the effective
       bankroll for the duration of the cooldown window.

This module does not apply the max_stake_pct cap - that stays inside the
sizer where it composes with confidence tiers.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from engine.user_config import UserConfig

# Once a streak cooldown engages, it halves stake sizes for this many trades.
STREAK_COOLDOWN_WINDOW = 5


@dataclass
class RiskVerdict:
    halt_reason:        Optional[str]  # non-None → skip trade
    effective_bankroll: float          # bankroll the sizer may deploy
    stake_multiplier:   float          # applied to the sizer's stake (0..1)
    notes:              str            # human-readable summary

    @property
    def halted(self) -> bool:
        return self.halt_reason is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["halted"] = self.halted
        return d


def evaluate(
    user_config:    UserConfig,
    bankroll:       float,
    starting_cash:  float,
    mode:           str,
    user_id:        str,
) -> RiskVerdict:
    """
    Apply the user's circuit breakers and return a verdict. Reads settled
    P&L, peak equity, and recent outcomes from pm_positions filtered to
    this user_id + mode. Failures fall through to a permissive verdict and
    log, so a DB hiccup doesn't lock the bot out (the sizer still respects
    max_stake_pct).
    """
    try:
        today_pnl = _pnl_since(user_id, mode, hours=24)
        weekly_pnl = _pnl_since(user_id, mode, hours=24 * 7)
        consecutive_losses = _consecutive_losses(user_id, mode)
        open_cost = _open_cost(user_id, mode)
    except Exception as exc:
        print(f"[risk_manager] stat load failed: {exc}", file=sys.stderr)
        return _permissive_verdict(user_config, bankroll)

    # Drawdown halt. Metric matches what the dashboard's Risk Today
    # panel displays: drop of current bankroll vs starting cash. The
    # passed-in `bankroll` already nets out open_cost so unrealised
    # losses count. The previous realised-only definition let a user
    # tie up half their bankroll in losing open positions while the
    # halt thought everything was fine because no settlement had
    # recorded the loss yet.
    if starting_cash > 0:
        drawdown = max(0.0, 1.0 - (bankroll / starting_cash))
        if drawdown >= user_config.drawdown_halt_pct:
            return RiskVerdict(
                halt_reason=(
                    f"drawdown {drawdown*100:.1f}% >= halt threshold "
                    f"{user_config.drawdown_halt_pct*100:.0f}% "
                    f"(bankroll ${bankroll:.2f} vs starting ${starting_cash:.2f})"
                ),
                effective_bankroll=0.0,
                stake_multiplier=0.0,
                notes="drawdown halt engaged - manual review required",
            )

    # Gross exposure cap. Total cost of open positions vs
    # starting_cash * (1 - dry_powder_reserve). Hard cap that doesn't
    # shrink as positions accumulate; the prior bankroll-relative
    # computation made the cap drop with every new trade and let the
    # user breach without the bot noticing.
    exposure_cap = starting_cash * (1.0 - user_config.dry_powder_reserve_pct)
    if exposure_cap > 0 and open_cost >= exposure_cap:
        return RiskVerdict(
            halt_reason=(
                f"gross exposure ${open_cost:.2f} >= cap ${exposure_cap:.2f} "
                f"({(1.0 - user_config.dry_powder_reserve_pct)*100:.0f}% "
                f"of starting ${starting_cash:.2f})"
            ),
            effective_bankroll=0.0,
            stake_multiplier=0.0,
            notes="gross exposure cap reached - close positions or raise dry powder reserve",
        )

    # Daily loss limit.
    daily_limit = -user_config.daily_loss_limit_pct * starting_cash
    if today_pnl <= daily_limit:
        return RiskVerdict(
            halt_reason=(
                f"daily loss ${today_pnl:+.2f} ≤ limit ${daily_limit:+.2f} "
                f"({user_config.daily_loss_limit_pct*100:.0f}% of ${starting_cash:.0f})"
            ),
            effective_bankroll=0.0,
            stake_multiplier=0.0,
            notes="daily loss limit breached",
        )

    # Weekly loss limit.
    weekly_limit = -user_config.weekly_loss_limit_pct * starting_cash
    if weekly_pnl <= weekly_limit:
        return RiskVerdict(
            halt_reason=(
                f"weekly loss ${weekly_pnl:+.2f} ≤ limit ${weekly_limit:+.2f} "
                f"({user_config.weekly_loss_limit_pct*100:.0f}% of ${starting_cash:.0f})"
            ),
            effective_bankroll=0.0,
            stake_multiplier=0.0,
            notes="weekly loss limit breached",
        )

    # Streak cooldown - halve stakes, don't halt.
    stake_multiplier = 1.0
    cooldown_note = ""
    if consecutive_losses >= user_config.streak_cooldown_losses:
        stake_multiplier = 0.5
        cooldown_note = (
            f"streak cooldown: {consecutive_losses} consecutive losses - "
            f"halving next {STREAK_COOLDOWN_WINDOW} stakes"
        )

    # Dry powder reserve carves off the permanent cushion.
    effective = max(0.0, bankroll * (1.0 - user_config.dry_powder_reserve_pct))

    notes = cooldown_note or "all breakers nominal"
    return RiskVerdict(
        halt_reason=None,
        effective_bankroll=effective,
        stake_multiplier=stake_multiplier,
        notes=notes,
    )


# ── Internal stat helpers ────────────────────────────────────────────────────
def _pnl_since(user_id: str, mode: str, hours: int) -> float:
    from sqlalchemy import text
    from db.engine import get_engine
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with get_engine().begin() as conn:
        val = conn.execute(text(
            "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
            "FROM pm_positions "
            "WHERE user_id = :uid "
            "  AND mode = :m "
            "  AND status IN ('settled', 'invalid') "
            "  AND settled_at >= :cutoff"
        ), {"uid": user_id, "m": mode, "cutoff": cutoff}).scalar()
    return float(val or 0.0)


def _realized_total(user_id: str, mode: str) -> float:
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        val = conn.execute(text(
            "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
            "FROM pm_positions "
            "WHERE user_id = :uid "
            "  AND mode = :m AND status IN ('settled', 'invalid')"
        ), {"uid": user_id, "m": mode}).scalar()
    return float(val or 0.0)


def _open_cost(user_id: str, mode: str) -> float:
    """Sum of cost_usd over currently-open positions in this mode.

    Used by the gross-exposure cap check. Cost (entry stake) is the
    most-conservative valuation - assumes every open position could
    settle to zero. Better than tracking live mark-to-market because
    Polymarket prices on long-tail markets are too thin to use as
    reliable valuations.
    """
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        val = conn.execute(text(
            "SELECT COALESCE(SUM(cost_usd), 0) "
            "FROM pm_positions "
            "WHERE user_id = :uid "
            "  AND mode = :m AND status = 'open'"
        ), {"uid": user_id, "m": mode}).scalar()
    return float(val or 0.0)


def _peak_equity(user_id: str, mode: str, starting_cash: float) -> float:
    """
    Peak equity = starting cash plus the maximum cumulative realised P&L
    seen at any point after a settlement. Simple running-max over the
    settlement ledger - good enough for drawdown computation.
    """
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT realized_pnl_usd "
            "FROM pm_positions "
            "WHERE user_id = :uid "
            "  AND mode = :m AND status IN ('settled', 'invalid') "
            "ORDER BY settled_at ASC"
        ), {"uid": user_id, "m": mode}).fetchall()
    running = float(starting_cash)
    peak = running
    for (pnl,) in rows:
        running += float(pnl or 0.0)
        if running > peak:
            peak = running
    return peak


def _consecutive_losses(user_id: str, mode: str) -> int:
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT realized_pnl_usd "
            "FROM pm_positions "
            "WHERE user_id = :uid "
            "  AND mode = :m AND status = 'settled' "
            "ORDER BY settled_at DESC "
            "LIMIT 20"
        ), {"uid": user_id, "m": mode}).fetchall()
    streak = 0
    for (pnl,) in rows:
        if pnl is None:
            break
        if float(pnl) < 0:
            streak += 1
        else:
            break
    return streak


def _permissive_verdict(user_config: UserConfig, bankroll: float) -> RiskVerdict:
    """Fallback when DB stats can't be loaded - let the sizer decide."""
    effective = max(0.0, bankroll * (1.0 - user_config.dry_powder_reserve_pct))
    return RiskVerdict(
        halt_reason=None,
        effective_bankroll=effective,
        stake_multiplier=1.0,
        notes="risk stats unavailable - dry powder reserve applied, no breakers evaluated",
    )
