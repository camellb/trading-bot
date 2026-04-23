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
    entry_cents: float,
    stake_usd: float,
    shares: float,
    forecast_pct: float,
    confidence: float,
    bankroll_after: float,
    resolve_date: str,
    mode: str,
) -> str:
    mode_suffix = "" if mode == "live" else " · simulation"
    return (
        f"🎯 <b>New position</b>{mode_suffix}\n"
        f"{_clip(question, MAX_QUESTION_NEW_POSITION)}\n"
        f"\n"
        f"Delfi forecasts: {side} ({forecast_pct:.0f}% probability)\n"
        f"Confidence: {confidence:.2f}\n"
        f"\n"
        f"Buying {side} at {entry_cents:.1f}¢\n"
        f"Stake: ${stake_usd:.2f} ({shares:.1f} shares)\n"
        f"Balance: ${bankroll_after:.2f}\n"
        f"Resolves: {resolve_date}"
    )


# ── 2. Position settled - WIN ────────────────────────────────────────────────
def settled_win(
    *,
    question: str,
    side: str,
    outcome: str,
    pnl: float,
    roi: float,
    bankroll: float,
) -> str:
    return (
        f"✅ <b>Position settled · WIN</b>\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Side: {side}\n"
        f"Resolved: {outcome}\n"
        f"Net P/L: +${pnl:.2f} ({roi:+.0f}%)\n"
        f"Balance: ${bankroll:.2f}"
    )


# ── 3. Position settled - LOSS ───────────────────────────────────────────────
def settled_loss(
    *,
    question: str,
    side: str,
    outcome: str,
    pnl: float,
    roi: float,
    bankroll: float,
) -> str:
    return (
        f"❌ <b>Position settled · LOSS</b>\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Side: {side}\n"
        f"Resolved: {outcome}\n"
        f"Net P/L: -${abs(pnl):.2f} ({roi:+.0f}%)\n"
        f"Balance: ${bankroll:.2f}\n"
        f"\n"
        f"Performance is measured across many positions. Delfi is recalibrating."
    )


# ── 4. First win (one-shot) ──────────────────────────────────────────────────
def first_win(
    *,
    question: str,
    pnl: float,
    roi: float,
    bankroll: float,
) -> str:
    return (
        f"✅ <b>First win</b>\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Net P/L: +${pnl:.2f} ({roi:+.0f}%)\n"
        f"Balance: ${bankroll:.2f}\n"
        f"\n"
        f"The first of many. Delfi will keep reading markets, sizing positions, "
        f"and reporting every outcome, win or loss."
    )


# ── 5. First loss (one-shot) ─────────────────────────────────────────────────
def first_loss(
    *,
    question: str,
    pnl: float,
    roi: float,
    bankroll: float,
) -> str:
    return (
        f"❌ <b>First loss</b>\n"
        f"{_clip(question, MAX_QUESTION_SETTLEMENT)}\n"
        f"\n"
        f"Net P/L: -${abs(pnl):.2f} ({roi:+.0f}%)\n"
        f"Balance: ${bankroll:.2f}\n"
        f"\n"
        f"Losses are part of the process. Delfi's accuracy is measured across "
        f"dozens of positions, not single ones. Every loss sharpens the calibration."
    )


# ── 6. Daily summary ─────────────────────────────────────────────────────────
def daily_summary(
    *,
    bankroll: float,
    pnl24: float,
    resolved24: int,
    wins24: int,
    losses24: int,
    win_pct: float,
    open_positions: int,
    open_cost: float,
    cnt24: int,
) -> str:
    return (
        f"📊 <b>Daily summary</b>\n"
        f"\n"
        f"Balance: ${bankroll:.2f}\n"
        f"P/L today: ${pnl24:+.2f} ({resolved24} resolved)\n"
        f"Today's record: {wins24}W {losses24}L\n"
        f"All-time win rate: {win_pct:.0f}%\n"
        f"\n"
        f"Open positions: {open_positions} (${open_cost:.2f} at risk)\n"
        f"Positions analysed today: {cnt24}"
    )


# ── 7. Weekly summary ────────────────────────────────────────────────────────
def weekly_summary(
    *,
    bankroll: float,
    pnl7: float,
    wins7: int,
    losses7: int,
    win_pct7: float,
    win_pct_all: float,
    pnl_all: float,
    settled_total: int,
) -> str:
    return (
        f"📈 <b>Weekly summary</b>\n"
        f"\n"
        f"Balance: ${bankroll:.2f}\n"
        f"P/L this week: ${pnl7:+.2f}\n"
        f"This week's record: {wins7}W {losses7}L ({win_pct7:.0f}%)\n"
        f"All-time win rate: {win_pct_all:.0f}%\n"
        f"\n"
        f"All-time P/L: ${pnl_all:+.2f} across {settled_total} positions"
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


# ── 10. Calibration declined ─────────────────────────────────────────────────
def calibration_declined() -> str:
    return "Declined. No change made."


# ── 11. Nothing pending ──────────────────────────────────────────────────────
def nothing_pending() -> str:
    return "Nothing pending right now."


# ── 12. /status response ─────────────────────────────────────────────────────
def status(
    *,
    uptime: str,
    bankroll: float,
    open_positions: int,
    open_cost: float,
    wins: int,
    losses: int,
    win_pct: float,
    realized_pnl: float,
    positions_block: str,
) -> str:
    return (
        f"📊 <b>Status</b> · up {uptime}\n"
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
def welcome() -> str:
    return (
        f"👋 <b>Welcome to Delfi</b>\n"
        f"\n"
        f"Delfi is an autonomous prediction market trader. It watches "
        f"Polymarket, forecasts outcomes, and backs every forecast with a "
        f"small, confidence-scaled stake.\n"
        f"\n"
        f"You will get a message for:\n"
        f"• every new position opened\n"
        f"• every resolution, with P&amp;L\n"
        f"• daily and weekly summaries\n"
        f"• any risk event (loss cap, drawdown halt, cooldown)\n"
        f"\n"
        f"Send /status any time for balance, open positions, and win rate. "
        f"Send /help for the full command list."
    )


# ── 16. Startup (full) ───────────────────────────────────────────────────────
def startup_full(
    *,
    balance: float,
    open_n: int,
    at_risk: float,
    win_pct: float,
    resolved: int,
    simulated: bool,
) -> str:
    sim = " (simulated)" if simulated else ""
    return (
        f"✅ <b>Delfi is online</b>{sim}\n"
        f"\n"
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
