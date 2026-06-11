"""
Delfi - Telegram Messages Spec v1 (locked copy).

Single source of truth for every user-facing Telegram message. Every function
returns a formatted HTML string ready to hand to notifier.send(). Edit the
copy here - nothing in the call-sites should embed user-facing strings
directly.

Voice rules (Oracle's Codex v1.1):
  * Delfi is "it", never "she".
  * No em dashes. No exclamation points.
  * Emojis are status glyphs only (✅ wins/online, ❌ losses, ⚠️ generic
    errors, ⏸ restart/pause, ▶️ resume, 📊 status/daily, 📈 weekly,
    🎯 new position, ⚙️ calibration proposal). Nothing else gets an emoji.
  * Telegram HTML parse_mode. Allowed tags: <b> <strong> <i> <em> <u> <s>
    <a> <code> <pre> <blockquote> <tg-spoiler>.

All callers import from this module. Admin-only output (feed health, wiring
errors, tracebacks) does not live here - it is logged to stderr.
"""

from __future__ import annotations

from typing import Optional


# Hard limits matching the original truncation behaviour.
MAX_QUESTION_NEW_POSITION = 140
MAX_QUESTION_SETTLEMENT = 120


def _clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ── 1. New position opened ────────────────────────────────────────────────────
def new_position(
    *,
    question: str,
    side: str,
    stake_usd: float,
    forecast_pct: float,
    confidence: float,
    bankroll_after: float,
    mode: str,
    equity_after: Optional[float] = None,
    locked_capital: Optional[float] = None,
) -> str:
    """The new-position notification renders a single canonical money
    block so the math is verifiable at a glance:

        Stake          - what just left cash to buy CTF tokens
        Balance        - spendable cash AFTER the bet
        Locked capital - sum of cost basis on every open position
                         (includes the bet that was just opened)
        Total equity   - Balance + Locked capital (= total wealth)

    The same block format is used by settled_win, settled_loss, and
    the early-exit messages so balances reconcile across surfaces.

    `bankroll_after` is the post-bet wallet balance (real on-chain
    in live mode, DB formula in simulation).
    `locked_capital` is the sum of cost_usd for every open position;
    when not supplied, falls back to `stake_usd` (the just-opened
    bet's cost), correct only when this is the user's only open
    position. Callers should pass the real value when available.
    `equity_after` defaults to `bankroll_after + locked_capital`.
    """
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    locked = (
        float(locked_capital) if locked_capital is not None
        else float(stake_usd)
    )
    eq = (
        float(equity_after) if equity_after is not None
        else float(bankroll_after) + locked
    )
    return (
        f"🎯 <b>New position</b>\n"
        f"{_clip(question, MAX_QUESTION_NEW_POSITION)}\n"
        f"\n"
        f"Delfi forecasts: {side} ({forecast_pct:.0f}% probability)\n"
        f"Confidence: {confidence:.2f}\n"
        f"\n"
        f"Stake: ${stake_usd:.2f}\n"
        f"Balance: ${bankroll_after:.2f}\n"
        f"Locked capital: ${locked:.2f}\n"
        f"Total equity: ${eq:.2f}\n"
        f"Mode: {mode_label}"
    )


# ── 2. Position settled - WIN ────────────────────────────────────────────────
def settled_win(
    *,
    question: str,
    side: str,
    outcome: str,
    pnl: float,
    roi: float,    # kept for call-site stability; not rendered
    bankroll: float,
    equity: float,
    locked_capital: Optional[float] = None,
    mode: str | None = None,
) -> str:
    """WIN settlement notification. Renders the same canonical money
    block as new_position (Balance / Locked capital / Total equity)
    so values reconcile across surfaces.

    `locked_capital`, when not supplied, falls back to
    ``equity - bankroll`` (the algebraic identity for any caller
    using the unified get_portfolio_stats(); see pm_executor).
    """
    _ = roi  # preserved for API compat
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    locked = (
        float(locked_capital) if locked_capital is not None
        else max(0.0, float(equity) - float(bankroll))
    )
    return (
        f"✅ <b>WIN</b> | +${pnl:.2f}\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Bet: {side}\n"
        f"Resolved: {outcome}\n"
        f"Balance: ${bankroll:.2f}\n"
        f"Locked capital: ${locked:.2f}\n"
        f"Total equity: ${equity:.2f}\n"
        f"Mode: {mode_label}"
    )


# ── 2b. Order REJECTED (live-only) ───────────────────────────────────────────
def order_rejected(
    *,
    question: str,
    side: str,
    stake_usd: float,
    price: float,
    error_text: str,
    mode: str | None = None,
) -> str:
    """Polymarket rejected a live order at the CLOB. Surface to
    Telegram so the user sees the rejection within seconds instead
    of finding out from the dashboard much later.
    """
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    return (
        f"⚠️ <b>Order rejected</b>\n"
        f"{_clip(question, MAX_QUESTION_NEW_POSITION)}\n"
        f"\n"
        f"Side: {side}\n"
        f"Stake: ${stake_usd:.2f} @ ${price:.3f}\n"
        f"\n"
        f"Reason: {_clip(error_text, 300)}\n"
        f"Mode: {mode_label}"
    )


# ── 2b'. Early-exit SELL REJECTED (live-only) ───────────────────────────────
def early_exit_failed(
    *,
    question: str,
    side: str,
    sell_price: float,
    error_text: str,
    mode: str | None = None,
) -> str:
    """Polymarket rejected the SELL the exit policy tried to place.

    Mirrors the Order-rejected card used for BUY-side rejections so
    every rejection in Telegram looks the same. The user already saw
    the BUY-side template shipped earlier; this is the matching SELL
    surface, used when stop-loss / take-profit / time-decay tries to
    close a live position and Polymarket bounces the order.

    Note: only fires for genuine API rejections the caller could not
    auto-recover. The common drift case ("not enough balance /
    allowance" because the shares are no longer in the wallet) is
    reconciled silently in pm_executor.close_position_early and never
    reaches this template, per the doctrine "the bot owns the entire
    trade lifecycle" (CLAUDE.md).
    """
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    return (
        f"⚠️ <b>Early exit failed</b>\n"
        f"{_clip(question, MAX_QUESTION_NEW_POSITION)}\n"
        f"\n"
        f"Bet: {side}\n"
        f"Trying to close at: ${sell_price:.3f}\n"
        f"\n"
        f"Reason: {_clip(error_text, 300)}\n"
        f"Mode: {mode_label}"
    )


# ── 2b''. Position refunded (market resolved INVALID) ───────────────────────
def position_invalid(
    *,
    question: str,
    side: str,
    stake_usd: float,
    bankroll: float,
    equity: float,
    locked_capital: Optional[float] = None,
    mode: str | None = None,
) -> str:
    """Polymarket resolved the market INVALID and refunded the stake.
    No P&L either way; the user gets their money back.

    Renders the same Balance / Locked capital / Total equity / Mode
    block as settled_win / settled_loss so the Telegram feed stays
    visually consistent for every position outcome.

    Replaces the prior raw-string description that pushed
    "Position #N resolved INVALID (Question). Stake refunded; no
    P&L." into Telegram bypassing the spec.
    """
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    locked = (
        float(locked_capital) if locked_capital is not None
        else max(0.0, float(equity) - float(bankroll))
    )
    return (
        f"⚠️ <b>Refunded</b>\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Bet: {side}\n"
        f"Outcome: market resolved INVALID\n"
        f"Stake refunded: ${stake_usd:.2f}\n"
        f"Balance: ${bankroll:.2f}\n"
        f"Locked capital: ${locked:.2f}\n"
        f"Total equity: ${equity:.2f}\n"
        f"Mode: {mode_label}"
    )


# ── 2b'''. Bankroll-pause notice ────────────────────────────────────────────
def bankroll_pause(
    *,
    bankroll: float,
    min_required: float,
    mode: str | None = None,
) -> str:
    """Available cash dropped below the platform minimum needed to
    place a trade. The bot keeps forecasting and resolving open
    positions but cannot open new ones until the wallet is funded.

    Replaces the prior hand-built notify() call in pm_analyst that
    used a banned emoji and bypassed event_log entirely.
    """
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    return (
        f"⏸ <b>Trading paused</b>\n"
        f"Available cash is below the minimum needed to place a trade.\n"
        f"\n"
        f"Balance: ${bankroll:.2f}\n"
        f"Minimum per trade: ${min_required:.2f}\n"
        f"Mode: {mode_label}\n"
        f"\n"
        f"Trading resumes automatically once the wallet is funded."
    )


# ── 2b''''. Mode-switch (Simulation <-> Live) ───────────────────────────────
def mode_switch(
    *,
    prior_mode: str,
    new_mode: str,
) -> str:
    """User flipped the master Simulation/Live toggle in Settings.
    Pushed so the user has a permanent record of the change in their
    Telegram history, with a brief explanation of what changes.

    Replaces the prior hand-built notify() call in local_api that
    bypassed event_log + the user's notification preferences.
    """
    label_old = "Live" if (prior_mode or "").lower() == "live" else "Simulation"
    label_new = "Live" if (new_mode  or "").lower() == "live" else "Simulation"
    if (new_mode or "").lower() == "live":
        detail = (
            "Real-money orders will fire on the next scan if a market "
            "clears the filter. Make sure your wallet is funded and "
            "risk settings are set correctly."
        )
    else:
        detail = (
            "Live trading paused. Delfi will keep forecasting but no "
            "real-money orders will be placed."
        )
    return (
        f"⏸ <b>Delfi switched to {label_new}</b>\n"
        f"From: {label_old}\n"
        f"\n"
        f"{detail}"
    )


# ── 2b'''''. Connectivity transitions (Polymarket reach) ────────────────────
def connectivity_lost(*, state: str, detail: str) -> str:
    """Polymarket connectivity probe just transitioned from ok to a
    failure state. Surface to Telegram so the user knows the bot is
    silently blocked instead of finding out hours later.

    state in {"unreachable", "geo_blocked"}.
    """
    if state == "unreachable":
        title = "Polymarket unreachable"
        explain = (
            "The bot can't reach Polymarket's servers. Common causes:\n"
            "  - VPN disconnected\n"
            "  - ISP started DNS-blocking polymarket.com domains\n"
            "  - Polymarket-side outage\n\n"
            "Trading is paused until the connection comes back. The "
            "bot will keep retrying every 5 minutes and ping you when "
            "it's working again."
        )
    else:  # geo_blocked
        title = "Trading geo-blocked"
        explain = (
            "Polymarket is reachable but orders are being rejected "
            "with HTTP 403 \"Trading restricted\". This usually means "
            "the VPN exit node is in a blocked region (e.g. routing "
            "through US datacenters), OR no VPN is running.\n\n"
            "Switch to a VPN exit in an allowed region (Netherlands, "
            "Germany, UK, etc.) and the bot will resume on the next "
            "scan tick."
        )
    return (
        f"⚠️ <b>{title}</b>\n"
        f"\n"
        f"{explain}\n"
        f"\n"
        f"Detail: {_clip(detail, 200)}"
    )


def connectivity_restored(*, gamma_latency_ms) -> str:
    """Polymarket connectivity probe just transitioned BACK to ok.
    Pairs with `connectivity_lost` so the user gets the green light
    that trading is unblocked again.
    """
    lat = (
        f"{int(gamma_latency_ms)}ms"
        if gamma_latency_ms is not None else "ok"
    )
    return (
        f"✅ <b>Polymarket connected</b>\n"
        f"\n"
        f"The bot can reach Polymarket again. Trading will resume "
        f"on the next scan tick.\n"
        f"\n"
        f"Detail: gamma-api {lat}"
    )


# ── 2c. Early exit - WIN (exit policy tripped, position closed positive) ────
def early_exit_win(
    *,
    question: str,
    side: str,
    reason: str,        # 'take_profit' | 'stop_loss' | 'time_decay'
    pnl: float,
    roi: float,
    bankroll: float,
    equity: float,
    details: str,
    locked_capital: Optional[float] = None,
    mode: str | None = None,
) -> str:
    """Position closed early by the exit policy with a positive P&L.
    Most often a take-profit hit; stop-loss + time-decay can also land
    here on rare positive-P&L exits (e.g. flat band crossing zero).

    Renders the same canonical Balance / Locked capital / Total equity
    block as settled_win.
    """
    _ = roi  # preserved for API compat with settled_win
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    reason_label = {
        "take_profit": "Take-profit hit",
        "stop_loss":   "Stop-loss hit",
        "time_decay":  "Time-decay exit",
    }.get(reason, "Exit policy")
    locked = (
        float(locked_capital) if locked_capital is not None
        else max(0.0, float(equity) - float(bankroll))
    )
    return (
        f"✅ <b>Closed early</b> | +${pnl:.2f}\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Bet: {side}\n"
        f"Reason: {reason_label}\n"
        f"Detail: {_clip(details, 160)}\n"
        f"Balance: ${bankroll:.2f}\n"
        f"Locked capital: ${locked:.2f}\n"
        f"Total equity: ${equity:.2f}\n"
        f"Mode: {mode_label}"
    )


# ── 2d. Early exit - LOSS (exit policy tripped, position closed negative) ───
def early_exit_loss(
    *,
    question: str,
    side: str,
    reason: str,
    pnl: float,
    roi: float,
    bankroll: float,
    equity: float,
    details: str,
    locked_capital: Optional[float] = None,
    mode: str | None = None,
) -> str:
    """Position closed early by the exit policy with a negative P&L.
    Stop-loss is the usual cause; time-decay also can land here when a
    stalled position is sold at a slight bid discount.

    Renders the same canonical Balance / Locked capital / Total equity
    block as settled_loss.
    """
    _ = roi
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    reason_label = {
        "take_profit": "Take-profit hit",
        "stop_loss":   "Stop-loss hit",
        "time_decay":  "Time-decay exit",
    }.get(reason, "Exit policy")
    locked = (
        float(locked_capital) if locked_capital is not None
        else max(0.0, float(equity) - float(bankroll))
    )
    return (
        f"❌ <b>Closed early</b> | -${abs(pnl):.2f}\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Bet: {side}\n"
        f"Reason: {reason_label}\n"
        f"Detail: {_clip(details, 160)}\n"
        f"Balance: ${bankroll:.2f}\n"
        f"Locked capital: ${locked:.2f}\n"
        f"Total equity: ${equity:.2f}\n"
        f"Mode: {mode_label}"
    )


# ── 2b. Order REJECTED (live-only) ───────────────────────────────────────────
def order_rejected(
    *,
    question: str,
    side: str,
    stake_usd: float,
    price: float,
    error_text: str,
    mode: str | None = None,
) -> str:
    """Polymarket rejected a live order at the CLOB. Surface to
    Telegram so the user sees the rejection within seconds instead
    of finding out from the dashboard much later.
    """
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    return (
        f"⚠️ <b>Order rejected</b>\n"
        f"{_clip(question, MAX_QUESTION_NEW_POSITION)}\n"
        f"\n"
        f"Side: {side}\n"
        f"Stake: ${stake_usd:.2f} @ ${price:.3f}\n"
        f"\n"
        f"Reason: {_clip(error_text, 300)}\n"
        f"Mode: {mode_label}"
    )


# ── 3. Position settled - LOSS ───────────────────────────────────────────────
def settled_loss(
    *,
    question: str,
    side: str,
    outcome: str,
    pnl: float,
    roi: float,    # kept for call-site stability; not rendered
    bankroll: float,
    equity: float,
    locked_capital: Optional[float] = None,
    mode: str | None = None,
) -> str:
    """LOSS settlement notification. Same canonical money block as
    new_position / settled_win so values reconcile across surfaces.
    """
    _ = roi  # preserved for API compat
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    locked = (
        float(locked_capital) if locked_capital is not None
        else max(0.0, float(equity) - float(bankroll))
    )
    return (
        f"❌ <b>LOSS</b> | -${abs(pnl):.2f}\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Bet: {side}\n"
        f"Resolved: {outcome}\n"
        f"Balance: ${bankroll:.2f}\n"
        f"Locked capital: ${locked:.2f}\n"
        f"Total equity: ${equity:.2f}\n"
        f"Mode: {mode_label}"
    )


# ── 4. First win (one-shot) ──────────────────────────────────────────────────
# Kept as a distinct function so notifier_state can track the one-shot flag,
# but the body delegates to settled_win so every win reads identically.
def first_win(
    *,
    question: str,
    side: str,
    pnl: float,
    roi: float,
    bankroll: float,
    equity: float,
    mode: str | None = None,
) -> str:
    return settled_win(
        question=question,
        side=side,
        outcome=side,  # first_win implies outcome matched the chosen side
        pnl=pnl,
        roi=roi,
        bankroll=bankroll,
        equity=equity,
        mode=mode,
    )


# ── 5. First loss (one-shot) ─────────────────────────────────────────────────
# Delegates to settled_loss so every loss reads identically.
def first_loss(
    *,
    question: str,
    side: str,
    outcome: str,
    pnl: float,
    roi: float,
    bankroll: float,
    equity: float,
    mode: str | None = None,
) -> str:
    return settled_loss(
        question=question,
        side=side,
        outcome=outcome,
        pnl=pnl,
        roi=roi,
        bankroll=bankroll,
        equity=equity,
        mode=mode,
    )


# ── 6. Daily summary ─────────────────────────────────────────────────────────
#
# Shape locked 2026-05-26 by user. Top three lines are the wallet
# triple (Total Equity / Available Cash / Locked in trades) so the
# user sees the same picture in Telegram that they see at the top
# of the Dashboard. Then the two performance lines for the day.
# Everything else (W/L counts, all-time win rate, open-position
# counter, analysed-markets counter) was removed - too many numbers
# telling slightly different stories.
def daily_summary(
    *,
    equity: float,
    bankroll: float,
    open_cost: float,
    pnl_today: float,
    win_pct_today: float,
) -> str:
    return (
        f"📊 <b>Daily summary</b>\n"
        f"\n"
        f"Equity: ${equity:.2f}\n"
        f"Available cash: ${bankroll:.2f}\n"
        f"Locked in trades: ${open_cost:.2f}\n"
        f"P/L today: ${pnl_today:+.2f}\n"
        f"Today's win rate: {win_pct_today:.0f}%"
    )


# ── 7. Weekly summary ────────────────────────────────────────────────────────
#
# Same shape as daily_summary. Different time window + label only.
def weekly_summary(
    *,
    equity: float,
    bankroll: float,
    open_cost: float,
    pnl_week: float,
    win_pct_week: float,
) -> str:
    return (
        f"📈 <b>Weekly summary</b>\n"
        f"\n"
        f"Equity: ${equity:.2f}\n"
        f"Available cash: ${bankroll:.2f}\n"
        f"Locked in trades: ${open_cost:.2f}\n"
        f"P/L this week: ${pnl_week:+.2f}\n"
        f"This week's win rate: {win_pct_week:.0f}%"
    )


# ── 8. Calibration update proposed ───────────────────────────────────────────
def calibration_proposal(
    *,
    key: str,
    current,
    value,
    reasoning: str,
    expected_impact: str,
) -> str:
    return (
        f"⚙️ <b>Calibration update proposed</b>\n"
        f"\n"
        f"Delfi's recent performance suggests an adjustment:\n"
        f"<code>{key}</code>: {current} → {value}\n"
        f"\n"
        f"Reasoning: {reasoning}\n"
        f"Expected impact: {expected_impact}\n"
        f"\n"
        f"/apply to adopt · /reject to decline\n"
        f"No action means no change."
    )


# ── 8b. Review report ready ──────────────────────────────────────────────────
def review_report_ready(report: dict) -> str:
    """Telegram-HTML rendering of the 50-trade review-ready event.

    Takes the dict produced by `engine.review_report.compose_report` and
    surfaces a tight summary: the thesis (clipped), the cycle headline
    (ROI, win rate, Brier, verdict), and lifetime ROI. Tells the user
    where the full report lives.

    Mirrors the Messages Spec voice rules: no em dashes, no exclamation
    points, Delfi is "it". 📊 status glyph reused for a review since
    the spec emoji set is closed.
    """
    data = (report or {}).get("data") or {}
    headline = data.get("headline") or {}
    lifetime = data.get("lifetime") or {}
    proposals = data.get("proposals") or []
    thesis = (report or {}).get("thesis") or ""
    verdict = data.get("verdict") or "neutral"

    cycle_n = int(headline.get("n") or 0)

    def _pct(v):
        if v is None:
            return "n/a"
        try:
            return f"{float(v) * 100:+.1f}%"
        except (TypeError, ValueError):
            return "n/a"

    def _pct_unsigned(v):
        if v is None:
            return "n/a"
        try:
            return f"{float(v) * 100:.0f}%"
        except (TypeError, ValueError):
            return "n/a"

    def _brier(v):
        if v is None:
            return "n/a"
        try:
            return f"{float(v):.3f}"
        except (TypeError, ValueError):
            return "n/a"

    thesis_short = _clip(thesis, 280)
    cycle_roi = _pct(headline.get("roi"))
    cycle_win = _pct_unsigned(headline.get("win_rate"))
    brier = _brier(headline.get("brier"))
    lifetime_roi = _pct(lifetime.get("roi"))

    n_pending = sum(1 for p in proposals if (p.get("status") == "pending"))
    proposals_line = (
        f"Proposals queued: {n_pending}" if n_pending else "No new proposals."
    )

    body = [
        "📊 <b>50-trade review ready</b>",
        "",
    ]
    if thesis_short:
        body += [thesis_short, ""]
    body += [
        f"Cycle ROI: {cycle_roi} ({cycle_win} wins, {cycle_n} trades)",
        f"Avg Brier: {brier} (verdict: {verdict})",
        f"Lifetime ROI: {lifetime_roi}",
        "",
        proposals_line,
        "",
        "Open Delfi for the full report.",
    ]
    return "\n".join(body)


# ── 9. Calibration applied ───────────────────────────────────────────────────
def calibration_applied(
    *,
    key: str,
    previous,
    value,
    restart_required: bool = False,
) -> str:
    restart_note = " · restart required" if restart_required else ""
    return (
        f"✅ <b>Calibration applied</b>\n"
        f"\n"
        f"<code>{key}</code>: {previous} → {value}{restart_note}"
    )


# ── 9b. Calibration applied (batch) ──────────────────────────────────────────
def calibration_applied_all(
    *,
    applied: list[dict],
    failed: list[dict] | None = None,
) -> str:
    """Multi-row /apply response. Renders one `<code>key: prev → new</code>`
    line per successfully applied suggestion, then a separate block for any
    rows that failed mid-batch. `applied` items must already carry the
    `display_key`, `display_previous`, `display_value` fields produced by
    `engine.learning_cadence.apply_all_pending_suggestions`.
    """
    failed = failed or []
    n_applied = len(applied)
    n_failed  = len(failed)

    if n_applied == 0 and n_failed == 0:
        return "Nothing pending right now."

    if n_applied == 1 and n_failed == 0:
        only = applied[0]
        return calibration_applied(
            key=only.get("display_key") or only.get("param_name") or "config",
            previous=only.get("display_previous"),
            value=(only.get("display_value")
                   if only.get("display_value") is not None
                   else only.get("value")),
            restart_required=False,
        )

    lines: list[str] = []
    if n_applied:
        header = (
            "✅ <b>Calibration applied</b>"
            if n_applied == 1
            else f"✅ <b>{n_applied} calibrations applied</b>"
        )
        lines.append(header)
        lines.append("")
        for item in applied:
            key = item.get("display_key") or item.get("param_name") or "config"
            prev = item.get("display_previous")
            val  = (item.get("display_value")
                    if item.get("display_value") is not None
                    else item.get("value"))
            lines.append(f"<code>{key}</code>: {prev} → {val}")

    if n_failed:
        if lines:
            lines.append("")
        plural = "s" if n_failed != 1 else ""
        lines.append(
            f"⚠️ {n_failed} suggestion{plural} could not be applied. "
            "Delfi will retry on the next cycle."
        )

    return "\n".join(lines)


# ── 10. Calibration declined ─────────────────────────────────────────────────
def calibration_declined() -> str:
    return "Declined. No change made."


# ── 11. Nothing pending ──────────────────────────────────────────────────────
def nothing_pending() -> str:
    return "Nothing pending right now."


# ── 12. /status response ─────────────────────────────────────────────────────
def status(
    *,
    paused: bool,
    mode: str,
    bankroll: float,
    open_positions: int,
    open_cost: float,
    wins: int,
    losses: int,
    win_pct: float,
    realized_pnl: float,
    positions_block: str,
) -> str:
    status_label = "Paused" if paused else "Live"
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    return (
        f"📊 <b>Status</b>\n"
        f"\n"
        f"Status: {status_label}\n"
        f"Mode: {mode_label}\n"
        f"\n"
        f"Balance: ${bankroll:.2f}\n"
        f"Open positions: {open_positions} (${open_cost:.2f} at risk)\n"
        f"Record: {wins}W {losses}L ({win_pct:.0f}%)\n"
        f"P/L (realized): ${realized_pnl:+.2f}\n"
        f"\n"
        f"<b>Open positions</b>\n"
        f"{positions_block}"
    )


# ── 13. /pause response ──────────────────────────────────────────────────────
def paused() -> str:
    return (
        f"⏸ <b>Trading paused</b>\n"
        f"\n"
        f"Delfi will stop opening new positions. Open positions remain active "
        f"until resolution.\n"
        f"\n"
        f"/resume when ready."
    )


# ── 14. /resume response ─────────────────────────────────────────────────────
def resumed() -> str:
    return (
        f"▶️ <b>Trading resumed</b>\n"
        f"\n"
        f"Delfi is scanning for new positions."
    )


# ── 14b. /pause when already paused ──────────────────────────────────────────
def already_paused() -> str:
    return (
        f"⏸ <b>Already paused</b>\n"
        f"\n"
        f"Delfi is not trading. /resume when ready."
    )


# ── 14c. /resume when not paused ─────────────────────────────────────────────
def already_running() -> str:
    return (
        f"▶️ <b>Already running</b>\n"
        f"\n"
        f"Delfi is already scanning for new positions."
    )


# ── 15. /help response ───────────────────────────────────────────────────────
def help_text() -> str:
    return (
        f"<b>Commands</b>\n"
        f"\n"
        f"/status - balance, open positions, win rate\n"
        f"/pause - stop placing new positions\n"
        f"/resume - start placing new positions again\n"
        f"/apply - accept a proposed calibration change\n"
        f"/reject - decline a proposed calibration change"
    )


# ── 15a. /start welcome ──────────────────────────────────────────────────────
def welcome(name: str = "") -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    return (
        f"✅ <b>Connected</b>\n"
        f"\n"
        f"{greeting}\n"
        f"\n"
        f"Delfi will send you notifications about every new position, every "
        f"resolved position, and a daily and weekly summary in this Telegram "
        f"chat.\n"
        f"\n"
        f"<b>Commands</b>\n"
        f"/status -balance, open positions, win rate\n"
        f"/pause -stop placing new positions\n"
        f"/resume -start placing new positions\n"
        f"/apply -accept a proposed calibration change\n"
        f"/reject -decline a proposed calibration change\n"
        f"/help -show this list"
    )


# ── 16. Startup (full) ───────────────────────────────────────────────────────
def startup_full(
    *,
    balance: float,
    open_n: int,
    at_risk: float,
    win_pct: float,
    resolved: int,
    mode: str,
) -> str:
    mode_label = "Live" if (mode or "").lower() == "live" else "Simulation"
    return (
        f"✅ <b>Delfi is online</b>\n"
        f"\n"
        f"Mode: {mode_label}\n"
        f"Balance: ${balance:.2f}\n"
        f"Open positions: {open_n} (${at_risk:.2f} at risk)\n"
        f"Win rate: {win_pct:.0f}% across {resolved} resolved positions"
    )


# ── 17. Startup (fallback) ───────────────────────────────────────────────────
def startup_fallback() -> str:
    return "✅ <b>Delfi is online</b>"


# ── 18. Restart (planned) ────────────────────────────────────────────────────
def restart_planned() -> str:
    return (
        f"⏸ <b>Delfi is restarting</b>\n"
        f"\n"
        f"Open positions are safe. Delfi will be back in a moment."
    )


# ── 19. Crash ────────────────────────────────────────────────────────────────
def restart_crash() -> str:
    return (
        f"⏸ <b>Delfi is restarting</b>\n"
        f"\n"
        f"Something went wrong. Open positions are safe. Delfi will be back "
        f"in a moment."
    )


# ── 20. Generic error ────────────────────────────────────────────────────────
def generic_error(*, context: str, detail: str) -> str:
    trimmed = (detail or "")[:200]
    return (
        f"⚠️ <b>Delfi hit an error</b>\n"
        f"{context}: {trimmed}\n"
        f"\n"
        f"Delfi will keep running. Trading continues."
    )
