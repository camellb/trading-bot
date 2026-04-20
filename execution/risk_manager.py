"""
Portfolio-level risk manager for Polymarket positions.

Sits between the sizer (pm_sizer.py) and the executor (pm_executor.py).
Every sizing decision must pass through check_risk() before execution.
If check_risk() returns (False, reason), the trade is refused.

Controls implemented:

    1. Daily loss limit — if realised + unrealised losses today exceed a
       configurable % of starting bankroll, refuse all new positions until
       the next calendar day. Prevents compounding bad runs.

    2. Weekly loss limit — same logic on a Monday-to-Sunday window. Weekly
       is wider (default 20%) because some losing days are normal.

    3. Consecutive loss cooldown — after N consecutive losses, reduce
       position sizes by a multiplier (default 50%) for the next N trades.
       This is a behavioural guardrail: streak losses often correlate with
       a regime change the model hasn't adapted to yet.

    4. Portfolio heat — total capital at risk across all open positions.
       If aggregate open stakes exceed a % of bankroll (default 30%),
       refuse new positions. Prevents over-commitment even when
       individual positions are small.

    5. Correlation guard — max positions per event group and per archetype.
       Correlated markets amplify drawdowns: if Claude is wrong about an
       event, every position in that event loses simultaneously.

    6. Drawdown circuit breaker — if bankroll drops below a % of peak
       bankroll (default 60% = 40% drawdown), halt ALL trading and
       surface an alert. This is the nuclear option for regime failure.

Statefulness:
    Loss limits and portfolio heat are computed from the database on every
    call — stateless across restarts. Streak tracking is in-memory and
    resets to zero on restart (conservative: no cooldown penalty after
    restart, which is safe because a restart also interrupts any bad run).
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text

import config
from db.engine import get_engine
from execution.pm_sizer import SizingDecision


class RiskManager:
    """
    Portfolio-level risk gatekeeper.

    Instantiate once at startup and call check_risk() before every execution.
    Thread-safe for the streak counter (single-writer model in the bot).
    """

    def __init__(self, mode: Optional[str] = None):
        self.mode = (mode or getattr(config, "PM_MODE", "shadow")).lower()

        # ── In-memory streak tracking ───────────────────────────────────────
        # Resets on restart — conservative default (no cooldown penalty).
        self._consecutive_losses: int = 0
        self._cooldown_trades_remaining: int = 0

    # =====================================================================
    # Public API
    # =====================================================================

    def check_risk(
        self,
        decision: SizingDecision,
        bankroll: float,
        mode: Optional[str] = None,
        event_slug: Optional[str] = None,
        archetype: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Evaluate whether a sized position should be allowed to execute.

        Args:
            decision:    The SizingDecision from pm_sizer.size_position().
            bankroll:    Current available bankroll in USD.
            mode:        'shadow' or 'live'. Falls back to self.mode.
            event_slug:  Event group slug for correlation checks.
            archetype:   Market archetype (sports_match, geopolitical, etc.).

        Returns:
            (True, None)              if the trade is allowed.
            (False, "reason string")  if the trade is blocked.
        """
        if not decision.should_trade:
            # Already rejected by the sizer — nothing for us to do.
            return True, None

        m = mode or self.mode
        starting = self._starting_cash(m)

        # ── 1. Drawdown circuit breaker (most severe — check first) ─────
        ok, reason = self._check_drawdown_halt(bankroll, starting, m)
        if not ok:
            return False, reason

        # ── 2. Daily loss limit ─────────────────────────────────────────
        ok, reason = self._check_daily_loss(starting, m)
        if not ok:
            return False, reason

        # ── 3. Weekly loss limit ────────────────────────────────────────
        ok, reason = self._check_weekly_loss(starting, m)
        if not ok:
            return False, reason

        # ── 4. Portfolio heat ───────────────────────────────────────────
        ok, reason = self._check_portfolio_heat(bankroll, decision, m)
        if not ok:
            return False, reason

        # ── 5. Dry powder reserve ──────────────────────────────────────
        ok, reason = self._check_dry_powder(bankroll, decision, m)
        if not ok:
            return False, reason

        # ── 6. Correlation guard: event group ───────────────────────────
        if event_slug:
            ok, reason = self._check_event_concentration(event_slug, m)
            if not ok:
                return False, reason

        # ── 7. Correlation guard: archetype ─────────────────────────────
        if archetype:
            ok, reason = self._check_archetype_concentration(archetype, m)
            if not ok:
                return False, reason

        # All checks passed.
        return True, None

    def apply_streak_adjustment(self, decision: SizingDecision) -> SizingDecision:
        """
        If a loss-streak cooldown is active, reduce the stake by the
        configured multiplier. Returns a new SizingDecision (or the
        original if no adjustment is needed).

        Call this AFTER check_risk() passes but BEFORE execution.
        """
        if self._cooldown_trades_remaining <= 0:
            return decision

        mult = float(getattr(config, "PM_LOSS_STREAK_SIZE_MULT", 0.5))
        adjusted_stake = decision.stake_usd * mult
        adjusted_shares = adjusted_stake / decision.entry_price if decision.entry_price > 0 else 0.0

        # Check min trade after adjustment
        min_trade = float(getattr(config, "PM_MIN_TRADE_USD", 2.0))
        if adjusted_stake < min_trade:
            return SizingDecision(
                side=decision.side,
                entry_price=decision.entry_price,
                edge=decision.edge,
                kelly_full=decision.kelly_full,
                kelly_frac=decision.kelly_frac,
                confidence=decision.confidence,
                stake_usd=0.0,
                shares=0.0,
                skip_reason=(f"streak cooldown reduced stake ${decision.stake_usd:.2f} "
                             f"x {mult:.0%} = ${adjusted_stake:.2f} < min ${min_trade:.2f}"),
            )

        self._cooldown_trades_remaining -= 1
        return SizingDecision(
            side=decision.side,
            entry_price=decision.entry_price,
            edge=decision.edge,
            kelly_full=decision.kelly_full,
            kelly_frac=decision.kelly_frac,
            confidence=decision.confidence,
            stake_usd=adjusted_stake,
            shares=adjusted_shares,
            skip_reason=None,
        )

    def record_outcome(self, pnl: float, is_win: bool) -> None:
        """
        Update streak tracking after a position settles.

        Args:
            pnl:    Realised P&L in USD (positive = profit).
            is_win: Whether the position was a win.
        """
        threshold = int(getattr(config, "PM_LOSS_STREAK_THRESHOLD", 3))

        if is_win:
            self._consecutive_losses = 0
            # A win does NOT cancel an active cooldown — the remaining
            # reduced-size trades still apply. This is intentional:
            # one win after a streak doesn't mean the regime is fixed.
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= threshold:
                # Activate cooldown for the next N trades (same as threshold).
                self._cooldown_trades_remaining = threshold
                print(
                    f"[risk_manager] loss streak {self._consecutive_losses} "
                    f">= {threshold}: activating cooldown for next "
                    f"{threshold} trades",
                    flush=True,
                )

    def get_risk_state(self) -> dict:
        """
        Return current state of all risk metrics for dashboard display.
        """
        m = self.mode
        starting = self._starting_cash(m)

        daily_pnl = self._get_period_pnl("day", m)
        weekly_pnl = self._get_period_pnl("week", m)
        open_cost = self._get_open_cost(m)
        peak_bankroll = self._get_peak_bankroll(starting, m)
        current_bankroll = self._get_current_bankroll(starting, m)

        daily_limit_pct = float(getattr(config, "PM_DAILY_LOSS_LIMIT_PCT", 0.10))
        weekly_limit_pct = float(getattr(config, "PM_WEEKLY_LOSS_LIMIT_PCT", 0.20))
        heat_limit_pct = float(getattr(config, "PM_MAX_PORTFOLIO_HEAT_PCT", 0.30))
        drawdown_halt_pct = float(getattr(config, "PM_DRAWDOWN_HALT_PCT", 0.60))

        daily_limit_usd = starting * daily_limit_pct
        weekly_limit_usd = starting * weekly_limit_pct
        heat_limit_usd = current_bankroll * heat_limit_pct

        drawdown_pct = (current_bankroll / peak_bankroll) if peak_bankroll > 0 else 1.0

        return {
            "mode": m,
            "starting_cash": starting,
            "current_bankroll": current_bankroll,
            "peak_bankroll": peak_bankroll,
            # Daily
            "daily_pnl": daily_pnl,
            "daily_limit_usd": daily_limit_usd,
            "daily_limit_pct": daily_limit_pct,
            "daily_limit_breached": daily_pnl < -daily_limit_usd,
            # Weekly
            "weekly_pnl": weekly_pnl,
            "weekly_limit_usd": weekly_limit_usd,
            "weekly_limit_pct": weekly_limit_pct,
            "weekly_limit_breached": weekly_pnl < -weekly_limit_usd,
            # Portfolio heat
            "open_cost": open_cost,
            "heat_limit_usd": heat_limit_usd,
            "heat_limit_pct": heat_limit_pct,
            "heat_pct": (open_cost / current_bankroll) if current_bankroll > 0 else 0.0,
            "heat_breached": open_cost > heat_limit_usd,
            # Drawdown
            "drawdown_pct": 1.0 - drawdown_pct,  # 0.0 = no drawdown, 0.4 = 40% drawdown
            "drawdown_halt_pct": 1.0 - drawdown_halt_pct,
            "drawdown_halted": drawdown_pct < drawdown_halt_pct,
            # Streak
            "consecutive_losses": self._consecutive_losses,
            "cooldown_trades_remaining": self._cooldown_trades_remaining,
            "loss_streak_threshold": int(getattr(config, "PM_LOSS_STREAK_THRESHOLD", 3)),
            # Dry powder
            "dry_powder_reserve_pct": float(getattr(config, "PM_DRY_POWDER_RESERVE_PCT", 0.20)),
            "deployable_capital": current_bankroll * (1.0 - float(getattr(config, "PM_DRY_POWDER_RESERVE_PCT", 0.20))),
        }

    def estimate_correlation_to_portfolio(
        self,
        event_slug: Optional[str],
        archetype: Optional[str],
        category: Optional[str],
        mode: str,
    ) -> float:
        """
        Estimate average correlation between a candidate position and
        existing open positions. Used to shrink stake for diversification.

        Correlation heuristics:
        - Same event group → high (0.7)
        - Same archetype, overlapping timeframe → moderate (0.3)
        - Same category → low (0.15)
        - Default → minimal (0.05)
        """
        try:
            with get_engine().begin() as conn:
                open_positions = conn.execute(text(
                    "SELECT event_slug, market_archetype, category "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status = 'open'"
                ), {"m": mode}).fetchall()

            if not open_positions:
                return 0.0

            same_event = float(getattr(config, "PM_CORRELATION_SAME_EVENT", 0.70))
            same_arch = float(getattr(config, "PM_CORRELATION_SAME_ARCHETYPE", 0.30))
            same_cat = float(getattr(config, "PM_CORRELATION_SAME_CATEGORY", 0.15))
            default = float(getattr(config, "PM_CORRELATION_DEFAULT", 0.05))

            rhos = []
            for row in open_positions:
                pos_event = row[0]
                pos_arch = row[1]
                pos_cat = row[2]

                if event_slug and pos_event and event_slug == pos_event:
                    rhos.append(same_event)
                elif archetype and pos_arch and archetype == pos_arch:
                    rhos.append(same_arch)
                elif category and pos_cat and category == pos_cat:
                    rhos.append(same_cat)
                else:
                    rhos.append(default)

            return sum(rhos) / len(rhos) if rhos else 0.0
        except Exception as exc:
            print(f"[risk_manager] estimate_correlation failed: {exc}", file=sys.stderr)
            return 0.0

    def adjust_stake_for_correlation(
        self,
        decision: SizingDecision,
        event_slug: Optional[str],
        archetype: Optional[str],
        category: Optional[str],
        mode: str,
    ) -> SizingDecision:
        """
        Shrink stake based on correlation with existing portfolio.
        f_adjusted = f * sqrt(1 - rho_avg^2)
        """
        rho = self.estimate_correlation_to_portfolio(event_slug, archetype, category, mode)
        if rho <= 0.05:
            return decision  # negligible correlation

        adjustment = math.sqrt(max(0, 1 - rho ** 2))
        adjusted_stake = decision.stake_usd * adjustment
        adjusted_shares = adjusted_stake / decision.entry_price if decision.entry_price > 0 else 0.0

        min_trade = float(getattr(config, "PM_MIN_TRADE_USD", 2.0))
        if adjusted_stake < min_trade:
            return SizingDecision(
                side=decision.side,
                entry_price=decision.entry_price,
                edge=decision.edge,
                kelly_full=decision.kelly_full,
                kelly_frac=decision.kelly_frac,
                confidence=decision.confidence,
                stake_usd=0.0,
                shares=0.0,
                skip_reason=(f"correlation-adjusted stake ${adjusted_stake:.2f} "
                             f"(rho={rho:.2f}, adj={adjustment:.2f}) < min ${min_trade:.2f}"),
            )

        return SizingDecision(
            side=decision.side,
            entry_price=decision.entry_price,
            edge=decision.edge,
            kelly_full=decision.kelly_full,
            kelly_frac=decision.kelly_frac,
            confidence=decision.confidence,
            stake_usd=adjusted_stake,
            shares=adjusted_shares,
            skip_reason=None,
        )

    # =====================================================================
    # Internal checks
    # =====================================================================

    def _check_daily_loss(self, starting: float, mode: str) -> tuple[bool, Optional[str]]:
        """Refuse if today's P&L exceeds the daily loss limit."""
        limit_pct = float(getattr(config, "PM_DAILY_LOSS_LIMIT_PCT", 0.10))
        limit_usd = starting * limit_pct
        daily_pnl = self._get_period_pnl("day", mode)

        if daily_pnl < -limit_usd:
            return False, (
                f"daily loss limit: ${daily_pnl:+.2f} today exceeds "
                f"{limit_pct:.0%} of ${starting:.0f} "
                f"(limit -${limit_usd:.2f})"
            )
        return True, None

    def _check_weekly_loss(self, starting: float, mode: str) -> tuple[bool, Optional[str]]:
        """Refuse if this week's P&L exceeds the weekly loss limit."""
        limit_pct = float(getattr(config, "PM_WEEKLY_LOSS_LIMIT_PCT", 0.20))
        limit_usd = starting * limit_pct
        weekly_pnl = self._get_period_pnl("week", mode)

        if weekly_pnl < -limit_usd:
            return False, (
                f"weekly loss limit: ${weekly_pnl:+.2f} this week exceeds "
                f"{limit_pct:.0%} of ${starting:.0f} "
                f"(limit -${limit_usd:.2f})"
            )
        return True, None

    def _check_portfolio_heat(
        self, bankroll: float, decision: SizingDecision, mode: str,
    ) -> tuple[bool, Optional[str]]:
        """Refuse if total open stakes + this trade exceed the heat limit."""
        limit_pct = float(getattr(config, "PM_MAX_PORTFOLIO_HEAT_PCT", 0.30))
        limit_usd = bankroll * limit_pct
        open_cost = self._get_open_cost(mode)
        projected = open_cost + decision.stake_usd

        if projected > limit_usd:
            return False, (
                f"portfolio heat: open ${open_cost:.2f} + new ${decision.stake_usd:.2f} "
                f"= ${projected:.2f} exceeds {limit_pct:.0%} of bankroll ${bankroll:.2f} "
                f"(limit ${limit_usd:.2f})"
            )
        return True, None

    def _check_event_concentration(
        self, event_slug: str, mode: str,
    ) -> tuple[bool, Optional[str]]:
        """Refuse if we already have max positions in this event group."""
        max_per_event = int(getattr(config, "PM_MAX_PER_EVENT", 3))
        count = self._count_open_by_event(event_slug, mode)

        if count >= max_per_event:
            return False, (
                f"event concentration: {count} open positions in event "
                f"{event_slug!r} (max {max_per_event})"
            )
        return True, None

    def _check_archetype_concentration(
        self, archetype: str, mode: str,
    ) -> tuple[bool, Optional[str]]:
        """Refuse if we already have max positions in this archetype."""
        max_per_archetype = int(getattr(config, "PM_MAX_PER_ARCHETYPE", 10))
        count = self._count_open_by_archetype(archetype, mode)

        if count >= max_per_archetype:
            return False, (
                f"archetype concentration: {count} open positions with archetype "
                f"{archetype!r} (max {max_per_archetype})"
            )
        return True, None

    def _check_drawdown_halt(
        self, bankroll: float, starting: float, mode: str,
    ) -> tuple[bool, Optional[str]]:
        """
        Halt all trading if bankroll has dropped below a % of peak.

        Peak bankroll = max(starting_cash, starting_cash + max cumulative
        realised P&L seen in settled positions). This is conservative:
        unrealised gains don't raise the peak, so the circuit breaker
        triggers based on confirmed equity only.
        """
        halt_pct = float(getattr(config, "PM_DRAWDOWN_HALT_PCT", 0.60))
        peak = self._get_peak_bankroll(starting, mode)

        if peak <= 0:
            return True, None

        ratio = bankroll / peak
        if ratio < halt_pct:
            drawdown = 1.0 - ratio
            return False, (
                f"drawdown circuit breaker: bankroll ${bankroll:.2f} is "
                f"{drawdown:.1%} below peak ${peak:.2f} "
                f"(halt threshold {1.0 - halt_pct:.0%} drawdown)"
            )
        return True, None

    def _check_dry_powder(
        self, bankroll: float, decision: SizingDecision, mode: str,
    ) -> tuple[bool, Optional[str]]:
        """Ensure we always keep a reserve for high-edge opportunities."""
        reserve_pct = float(getattr(config, "PM_DRY_POWDER_RESERVE_PCT", 0.20))
        open_cost = self._get_open_cost(mode)
        max_deployable = bankroll * (1.0 - reserve_pct)
        projected = open_cost + decision.stake_usd

        if projected > max_deployable:
            return False, (
                f"dry powder reserve: open ${open_cost:.2f} + new ${decision.stake_usd:.2f} "
                f"= ${projected:.2f} exceeds deployable ${max_deployable:.2f} "
                f"(keeping {reserve_pct:.0%} reserve of ${bankroll:.2f})"
            )
        return True, None

    # =====================================================================
    # Database queries
    # =====================================================================

    def _starting_cash(self, mode: str) -> float:
        if mode == "live":
            return float(getattr(config, "PM_LIVE_STARTING_CASH", 200.0))
        return float(getattr(config, "PM_SHADOW_STARTING_CASH", 1000.0))

    def _get_period_pnl(self, period: str, mode: str) -> float:
        """
        Sum realised P&L for settled positions within the given period.

        period: 'day' (since midnight UTC today) or 'week' (since Monday
        midnight UTC this week).
        """
        now = datetime.now(timezone.utc)
        if period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            # Monday = 0 in weekday()
            days_since_monday = now.weekday()
            start = (now - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0)
        else:
            raise ValueError(f"Unknown period: {period!r}")

        try:
            with get_engine().begin() as conn:
                result = conn.execute(text(
                    "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                    "FROM pm_positions "
                    "WHERE mode = :m "
                    "  AND status IN ('settled', 'invalid') "
                    "  AND settled_at >= :since"
                ), {"m": mode, "since": start}).scalar()
                return float(result or 0.0)
        except Exception as exc:
            print(f"[risk_manager] _get_period_pnl({period}) failed: {exc}",
                  file=sys.stderr)
            return 0.0  # fail open — don't block trades on DB error

    def _get_open_cost(self, mode: str) -> float:
        """Total cost_usd of all open positions."""
        try:
            with get_engine().begin() as conn:
                result = conn.execute(text(
                    "SELECT COALESCE(SUM(cost_usd), 0) "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status = 'open'"
                ), {"m": mode}).scalar()
                return float(result or 0.0)
        except Exception as exc:
            print(f"[risk_manager] _get_open_cost failed: {exc}",
                  file=sys.stderr)
            return 0.0

    def _get_current_bankroll(self, starting: float, mode: str) -> float:
        """Current bankroll = starting + realised P&L - open cost."""
        try:
            with get_engine().begin() as conn:
                realized = conn.execute(text(
                    "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status IN ('settled', 'invalid')"
                ), {"m": mode}).scalar() or 0.0
                open_cost = conn.execute(text(
                    "SELECT COALESCE(SUM(cost_usd), 0) "
                    "FROM pm_positions "
                    "WHERE mode = :m AND status = 'open'"
                ), {"m": mode}).scalar() or 0.0
                return float(starting) + float(realized) - float(open_cost)
        except Exception as exc:
            print(f"[risk_manager] _get_current_bankroll failed: {exc}",
                  file=sys.stderr)
            return float(starting)

    def _get_peak_bankroll(self, starting: float, mode: str) -> float:
        """
        Estimate peak bankroll from cumulative realised P&L.

        We compute the running maximum of (starting + cumulative P&L) over
        all settled positions ordered by settlement time. This gives us the
        high-water mark without storing it persistently.
        """
        try:
            with get_engine().begin() as conn:
                rows = conn.execute(text(
                    "SELECT realized_pnl_usd "
                    "FROM pm_positions "
                    "WHERE mode = :m "
                    "  AND status IN ('settled', 'invalid') "
                    "  AND realized_pnl_usd IS NOT NULL "
                    "ORDER BY settled_at ASC"
                ), {"m": mode}).fetchall()

                if not rows:
                    return float(starting)

                cumulative = 0.0
                peak = 0.0
                for row in rows:
                    cumulative += float(row[0])
                    if cumulative > peak:
                        peak = cumulative

                return float(starting) + peak
        except Exception as exc:
            print(f"[risk_manager] _get_peak_bankroll failed: {exc}",
                  file=sys.stderr)
            return float(starting)

    def _count_open_by_event(self, event_slug: str, mode: str) -> int:
        """Count open positions in a given event group."""
        try:
            with get_engine().begin() as conn:
                result = conn.execute(text(
                    "SELECT COUNT(*) FROM pm_positions "
                    "WHERE mode = :m AND status = 'open' AND event_slug = :slug"
                ), {"m": mode, "slug": event_slug}).scalar()
                return int(result or 0)
        except Exception as exc:
            print(f"[risk_manager] _count_open_by_event failed: {exc}",
                  file=sys.stderr)
            return 0

    def _count_open_by_archetype(self, archetype: str, mode: str) -> int:
        """Count open positions with a given market archetype."""
        try:
            with get_engine().begin() as conn:
                result = conn.execute(text(
                    "SELECT COUNT(*) FROM pm_positions "
                    "WHERE mode = :m AND status = 'open' "
                    "  AND market_archetype = :arch"
                ), {"m": mode, "arch": archetype}).scalar()
                return int(result or 0)
        except Exception as exc:
            print(f"[risk_manager] _count_open_by_archetype failed: {exc}",
                  file=sys.stderr)
            return 0
