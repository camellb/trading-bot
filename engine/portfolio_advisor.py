"""
Portfolio Advisor — weekly advisory messages based on account growth/drawdown.

Called every Monday at 08:30 MYT (00:30 UTC Monday) after the weekly summary.
Checks whether the account is up significantly (suggest withdrawing profits)
or down significantly (suggest adding funds), and sends a Telegram message.

No automated action is taken — purely advisory.
"""

import os
import sys
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, text

import config

if TYPE_CHECKING:
    from execution.order_manager import OrderManager
    from feeds.telegram_notifier import TelegramNotifier


class PortfolioAdvisor:
    """
    Sends a weekly Telegram advisory when the account deviates significantly
    from STARTING_CAPITAL_USD (up 20%+ or down 15%+).
    """

    def __init__(self, notifier: "TelegramNotifier") -> None:
        self._notifier = notifier

    async def check_and_advise(self, order_manager: "OrderManager") -> None:
        """
        Check current portfolio state and send an advisory message if warranted.

        Advice is sent only when:
          - pnl_pct >= PORTFOLIO_ADVISORY_PROFIT_PCT  (up 20%+ → withdrawal suggestion)
          - pnl_pct <= PORTFOLIO_ADVISORY_REFILL_PCT  (down 15%+ → refill suggestion)

        No message is sent when the account is within the normal ±15%–20% band.
        """
        try:
            # ── Step 1: Portfolio state ───────────────────────────────────────
            current_balance = order_manager.get_portfolio_value()
            starting_capital = config.STARTING_CAPITAL_USD
            total_pnl = current_balance - starting_capital
            pnl_pct = total_pnl / starting_capital if starting_capital > 0 else 0.0

            # Weekly P&L from daily_pnl table
            weekly_pnl = self._get_weekly_pnl()

            # ── Step 2: Determine advice ──────────────────────────────────────
            if pnl_pct >= config.PORTFOLIO_ADVISORY_PROFIT_PCT:
                await self._send_profit_message(
                    current_balance, starting_capital, total_pnl, pnl_pct, weekly_pnl
                )
            elif pnl_pct <= config.PORTFOLIO_ADVISORY_REFILL_PCT:
                await self._send_refill_message(
                    current_balance, starting_capital, total_pnl, pnl_pct, weekly_pnl
                )
            # else: account is healthy — no message

        except Exception as exc:
            print(
                f"[portfolio_advisor] check_and_advise error: {exc}",
                file=sys.stderr,
            )

    # ── DB helper ─────────────────────────────────────────────────────────────

    def _get_weekly_pnl(self) -> float:
        """Query daily_pnl table for last 7 days. Returns 0.0 on error."""
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            return 0.0
        try:
            engine = create_engine(database_url)
            with engine.begin() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT SUM(pnl_usd) AS weekly_pnl
                        FROM daily_pnl
                        WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                          AND paper = :paper
                        """
                    ),
                    {"paper": config.PAPER_MODE},
                ).fetchone()
                return float(row._mapping["weekly_pnl"] or 0.0) if row else 0.0
        except Exception as exc:
            print(
                f"[portfolio_advisor] _get_weekly_pnl error: {exc}",
                file=sys.stderr,
            )
            return 0.0

    # ── Telegram messages ─────────────────────────────────────────────────────

    async def _send_profit_message(
        self,
        current_balance: float,
        starting_capital: float,
        total_pnl: float,
        pnl_pct: float,
        weekly_pnl: float,
    ) -> None:
        """Send profit withdrawal advisory."""
        if not self._notifier.enabled:
            return
        withdrawal_amount = total_pnl * 0.5
        new_base = current_balance - withdrawal_amount

        msg = (
            f"💰 <b>PORTFOLIO ADVISORY</b>\n"
            f"Your account is up {pnl_pct * 100:.1f}% since start.\n"
            f"\n"
            f"Current balance: ${current_balance:.2f}\n"
            f"Starting capital: ${starting_capital:.2f}\n"
            f"Profit: +${total_pnl:.2f}\n"
            f"\n"
            f"<b>Suggestion:</b> Consider withdrawing ${withdrawal_amount:.2f} "
            f"(50% of profits) and leaving ${new_base:.2f} "
            f"as your new trading base. This locks in gains while keeping "
            f"the bot funded.\n"
            f"\n"
            f"Position sizes will automatically scale with your balance."
        )
        await self._notifier.send(msg)

    async def _send_refill_message(
        self,
        current_balance: float,
        starting_capital: float,
        total_pnl: float,
        pnl_pct: float,
        weekly_pnl: float,
    ) -> None:
        """Send refill / drawdown advisory."""
        if not self._notifier.enabled:
            return
        refill_amount = abs(total_pnl)

        msg = (
            f"⚠️ <b>PORTFOLIO ADVISORY</b>\n"
            f"Your account is down {abs(pnl_pct) * 100:.1f}% since start.\n"
            f"\n"
            f"Current balance: ${current_balance:.2f}\n"
            f"Starting capital: ${starting_capital:.2f}\n"
            f"Drawdown: -${refill_amount:.2f}\n"
            f"\n"
            f"<b>Suggestion:</b> Consider adding ${refill_amount:.2f} to restore "
            f"your original ${starting_capital:.2f} trading base, or review "
            f"strategy settings before adding more funds.\n"
            f"\n"
            f"This week's P&L: {weekly_pnl:+.2f} USD"
        )
        await self._notifier.send(msg)
