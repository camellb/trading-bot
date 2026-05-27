"""Exit-policy decision engine.

Pure logic that, given a single open position + current market bid +
the user's exit-policy config + the market's natural-resolution time,
decides whether to close the position early. No I/O, no DB writes, no
SDK calls — those happen upstream in pm_executor / polymarket_runner.

The three exit triggers (each individually toggleable in UserConfig):

1. Take-profit
   Close when unrealized return >= take_profit_threshold_pct.
   Computed against the CURRENT BID (the price we could actually sell
   at), not mid or last trade, so a wide spread can't trigger false
   exits.

2. Stop-loss
   Close when unrealized return <= -stop_loss_threshold_pct, BUT only
   if at least stop_loss_min_time_remaining_pct of the original time-
   to-resolution is still left. Prevents cutting losses on a wick
   in the final minutes of a market when a recovery is plausible.

3. Time-decay
   Close stalled positions that have been open more than
   time_decay_max_hours AND whose unrealized return is inside the
   ±time_decay_flat_band_pct band ("flat enough" — don't kick a
   winner out just because it's been open a while).

A universal safety floor on top of all three: never exit if the market
is within exit_min_time_to_resolution_minutes of its natural settlement
— spread + Polymarket fees exceed the time-value gain of selling that
close to resolution.

Doctrine: this module owns the WHY (the rule), pm_executor.close_live
owns the HOW (the SELL order). Counterfactual scoring of exits (was
this the right call or premature?) happens in engine/review_report.py
after natural settlement back-fills `counterfactual_pnl_usd` on the
position row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Literal


ExitReason = Literal["take_profit", "stop_loss", "time_decay"]

EXIT_REASONS: tuple[str, ...] = ("take_profit", "stop_loss", "time_decay")


@dataclass
class ExitDecision:
    """Result of evaluating one open position against the user's exit
    policy. `should_exit=True` means the caller should place a SELL
    order at the supplied bid; otherwise hold."""

    should_exit:     bool
    reason:          Optional[ExitReason]
    details:         str               # user-facing one-line explanation
    unrealized_pct:  float              # (current_bid - entry) / entry
    bid_used:        float              # the bid value the decision was made against
    hours_open:      Optional[float] = None
    hours_remaining: Optional[float] = None


def _hold(unrealized_pct: float, bid: float, why: str,
          hours_open: Optional[float] = None,
          hours_remaining: Optional[float] = None) -> ExitDecision:
    return ExitDecision(
        should_exit=False,
        reason=None,
        details=why,
        unrealized_pct=unrealized_pct,
        bid_used=bid,
        hours_open=hours_open,
        hours_remaining=hours_remaining,
    )


def _exit(reason: ExitReason, unrealized_pct: float, bid: float,
          details: str,
          hours_open: Optional[float] = None,
          hours_remaining: Optional[float] = None) -> ExitDecision:
    return ExitDecision(
        should_exit=True,
        reason=reason,
        details=details,
        unrealized_pct=unrealized_pct,
        bid_used=bid,
        hours_open=hours_open,
        hours_remaining=hours_remaining,
    )


def evaluate_exit(
    *,
    position:              dict,
    current_bid:           Optional[float],
    user_config,
    expected_resolution_at: Optional[datetime],
    now:                   Optional[datetime] = None,
) -> ExitDecision:
    """Decide whether to close `position` right now.

    Parameters
    ----------
    position:
        Dict-like row from `pm_positions`. Must have at least
        `entry_price`, `created_at`, `status`. `status` must be 'open'
        — the caller should filter beforehand.
    current_bid:
        Current top-of-book bid for the position's outcome on
        Polymarket. None / 0 means the orderbook has no bid (illiquid)
        — we hold in that case to avoid selling into a thin or empty
        book.
    user_config:
        UserConfig dataclass (engine/user_config.py). The 10 exit-
        policy fields are consulted; the master switch
        `exit_policy_enabled` short-circuits everything.
    expected_resolution_at:
        UTC timestamp when the market is expected to settle naturally.
        None means "unknown" — we conservatively skip the safety floor
        check, the stop-loss time gate falls back to "fire" (the bigger
        risk on small bankrolls is letting losses run, not cutting too
        eagerly).
    now:
        Override for the current time, mostly for tests. Defaults to
        datetime.now(UTC).
    """
    now = now or datetime.now(timezone.utc)
    entry = float(position.get("entry_price") or 0.0)
    if entry <= 0.0:
        return _hold(0.0, current_bid or 0.0,
                     "missing or zero entry price; cannot compute return")
    if current_bid is None or current_bid <= 0.0:
        return _hold(0.0, current_bid or 0.0,
                     "no bid on the orderbook; holding")

    unrealized_pct = (current_bid - entry) / entry

    # Master switch
    if not getattr(user_config, "exit_policy_enabled", False):
        return _hold(unrealized_pct, current_bid,
                     "exit policy disabled")

    # Time bookkeeping
    opened_at = position.get("created_at")
    if isinstance(opened_at, str):
        try:
            opened_at = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        except Exception:
            opened_at = None
    if opened_at is not None and opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    hours_open: Optional[float] = (
        (now - opened_at).total_seconds() / 3600.0
        if opened_at is not None else None
    )

    if expected_resolution_at is not None and expected_resolution_at.tzinfo is None:
        expected_resolution_at = expected_resolution_at.replace(tzinfo=timezone.utc)
    hours_remaining: Optional[float] = (
        (expected_resolution_at - now).total_seconds() / 3600.0
        if expected_resolution_at is not None else None
    )

    # Universal safety floor: don't exit if very close to natural
    # settlement. Spread + fees eat the marginal value.
    safety_min_minutes = int(getattr(
        user_config, "exit_min_time_to_resolution_minutes", 5
    ))
    if hours_remaining is not None and hours_remaining * 60.0 < safety_min_minutes:
        return _hold(
            unrealized_pct, current_bid,
            f"only {hours_remaining * 60.0:.1f} min to natural settlement "
            f"(safety floor {safety_min_minutes} min)",
            hours_open=hours_open, hours_remaining=hours_remaining,
        )

    # ── Stop-loss (checked FIRST so a losing position doesn't accidentally
    #    qualify for time-decay's flat-band fallthrough) ───────────────────
    sl_enabled = bool(getattr(user_config, "stop_loss_enabled", False))
    sl_threshold = float(getattr(user_config, "stop_loss_threshold_pct", 0.30))
    sl_min_time_remaining_pct = float(getattr(
        user_config, "stop_loss_min_time_remaining_pct", 0.20
    ))
    if sl_enabled and unrealized_pct <= -abs(sl_threshold):
        # Total expected duration of the market, derived from
        # opened_at→expected_resolution_at when both are known. If
        # either is missing, fire — the bigger risk on small bankrolls
        # is letting losses run.
        time_gate_ok = True
        if (hours_open is not None and hours_remaining is not None
                and hours_remaining >= 0):
            total = hours_open + hours_remaining
            if total > 0:
                remaining_frac = hours_remaining / total
                if remaining_frac < sl_min_time_remaining_pct:
                    time_gate_ok = False
        if time_gate_ok:
            return _exit(
                "stop_loss", unrealized_pct, current_bid,
                f"stop-loss tripped at {unrealized_pct * 100:+.1f}%",
                hours_open=hours_open, hours_remaining=hours_remaining,
            )
        # Else: stop-loss would have tripped but we're too close to
        # resolution; let it ride out.

    # ── Take-profit ───────────────────────────────────────────────────────
    tp_enabled = bool(getattr(user_config, "take_profit_enabled", False))
    tp_threshold = float(getattr(user_config, "take_profit_threshold_pct", 0.50))
    if tp_enabled and unrealized_pct >= abs(tp_threshold):
        return _exit(
            "take_profit", unrealized_pct, current_bid,
            f"take-profit tripped at {unrealized_pct * 100:+.1f}%",
            hours_open=hours_open, hours_remaining=hours_remaining,
        )

    # ── Time-decay (checked LAST — only fires for stalled flat positions) ─
    td_enabled = bool(getattr(user_config, "time_decay_enabled", False))
    td_max_hours = float(getattr(user_config, "time_decay_max_hours", 72))
    td_flat_band = float(getattr(user_config, "time_decay_flat_band_pct", 0.10))
    if (td_enabled
            and hours_open is not None
            and hours_open >= td_max_hours
            and abs(unrealized_pct) <= abs(td_flat_band)):
        return _exit(
            "time_decay", unrealized_pct, current_bid,
            f"time-decay: open {hours_open:.1f}h "
            f"(max {td_max_hours:.0f}h), unrealized "
            f"{unrealized_pct * 100:+.1f}% within ±{td_flat_band * 100:.0f}% "
            f"flat band",
            hours_open=hours_open, hours_remaining=hours_remaining,
        )

    return _hold(
        unrealized_pct, current_bid,
        f"hold: unrealized {unrealized_pct * 100:+.1f}%",
        hours_open=hours_open, hours_remaining=hours_remaining,
    )
