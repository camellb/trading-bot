"""
DB Logger — writes all events to PostgreSQL using SQLAlchemy Core.

All functions read DATABASE_URL from environment, handle exceptions without
raising, and log errors to stderr.
"""

import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from db.models import ticks, trades, positions, daily_pnl, event_log, feed_health_log, reconciliation_log


def _get_engine():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return create_engine(database_url)


def log_feed_health(feed_name: str, state: str, detail: str | None) -> None:
    """Insert a row into feed_health_log."""
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                feed_health_log.insert().values(
                    timestamp=datetime.now(timezone.utc),
                    feed_name=feed_name,
                    state=state,
                    detail=detail,
                )
            )
    except Exception as exc:
        print(f"[logger] log_feed_health error: {exc}", file=sys.stderr)


def log_event(
    event_type: str, severity: int | None, description: str, source: str
) -> None:
    """Insert a row into event_log."""
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                event_log.insert().values(
                    timestamp=datetime.now(timezone.utc),
                    event_type=event_type,
                    severity=severity,
                    description=description,
                    source=source,
                )
            )
    except Exception as exc:
        print(f"[logger] log_event error: {exc}", file=sys.stderr)


def log_tick(tick_data: dict) -> None:
    """Insert a row into ticks. tick_data keys match column names."""
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(ticks.insert().values(**tick_data))
    except Exception as exc:
        print(f"[logger] log_tick error: {exc}", file=sys.stderr)


def log_trade_open(trade_data: dict) -> int:
    """Insert a row into trades. Return the new trade id."""
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            result = conn.execute(
                trades.insert().values(**trade_data).returning(trades.c.id)
            )
            row = result.fetchone()
            return row[0] if row else -1
    except Exception as exc:
        print(f"[logger] log_trade_open error: {exc}", file=sys.stderr)
        return -1


def load_open_trades(paper: bool) -> list[dict]:
    """
    Return all open trades (timestamp_close IS NULL) for the given paper mode.
    Used by PositionMonitor on startup to restore in-memory position state.

    Each dict contains the fields needed to reconstruct a position entry:
    trade_id, pair, direction, entry_price, size_usd, stop_loss, take_profit,
    timestamp_open, paper.

    Raises on DB error — does NOT return [] silently.
    Returning [] on error would let the risk kernel treat a DB failure as
    "no open positions" and bypass all position limit checks.
    """
    engine = _get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            trades.select().where(
                (trades.c.paper == paper)
                & trades.c.timestamp_close.is_(None)
            )
        )
        return [dict(row._mapping) for row in result.fetchall()]


def log_trade_close(
    trade_id: int,
    exit_price: float,
    pnl_usd: float,
    close_reason: str,
    timestamp_close: datetime | None = None,
) -> bool:
    """
    Update trades row: set timestamp_close, exit_price, pnl_usd, close_reason.

    timestamp_close: actual exchange close time when known (ghost/auto-recovery).
                     Defaults to now() so existing callers need no change.
    Returns True on success, False on any exception (never swallows silently).
    """
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                trades.update()
                .where(trades.c.id == trade_id)
                .values(
                    timestamp_close=timestamp_close or datetime.now(timezone.utc),
                    exit_price=exit_price,
                    pnl_usd=pnl_usd,
                    close_reason=close_reason,
                )
            )
        return True
    except Exception as exc:
        print(f"[logger] log_trade_close error: {exc}", file=sys.stderr)
        return False


def log_regime(pair: str, regime_data: dict) -> None:
    """
    Insert a row into the ticks table from a classify() output dict.
    layer_b_signal, layer_c_signal, layer_d_result, layer_d_reason are set to
    None here — filled in by later milestones (M3).
    """
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                ticks.insert().values(
                    timestamp=datetime.now(timezone.utc),
                    pair=pair,
                    regime=regime_data.get("regime", "NO_TRADE"),
                    adx=regime_data.get("adx"),
                    realized_vol_pct=regime_data.get("realized_vol_pct"),
                    funding_pct=regime_data.get("funding_pct"),
                    oi_delta=regime_data.get("oi_delta"),
                    layer_b_signal=None,
                    layer_c_signal=None,
                    layer_d_result=None,
                    layer_d_reason=None,
                    decision=regime_data.get("regime", "NO_TRADE"),
                    decision_reason=regime_data.get("reason", ""),
                )
            )
    except Exception as exc:
        print(f"[logger] log_regime error: {exc}", file=sys.stderr)


def log_signal(cycle_result: dict) -> None:
    """
    Write a full decision cycle result to the ticks table.

    Accepts either a TRADE or REJECT dict from DecisionLoop.run_cycle().
    Maps available fields to ticks columns; fields not yet reached in the
    pipeline are stored as None. Called on every cycle regardless of outcome.

    Field mapping:
      layer_b_signal  ← cycle_result["layer_b"]["signal"] (str or None)
      layer_c_signal  ← str(cycle_result["layer_c"]["confirmed"]) or None
      layer_d_result  ← cycle_result["layer_d"][0] (bool or None)
      layer_d_reason  ← cycle_result["layer_d"][1] (str or None)
      decision        ← cycle_result["decision"] ('TRADE' or 'REJECT')
      decision_reason ← cycle_result["reject_reason"] for REJECT,
                        sizing reason for TRADE (or empty string)
    """
    try:
        engine = _get_engine()

        # Safely extract nested layer results
        layer_b: dict = cycle_result.get("layer_b") or {}
        layer_c: dict = cycle_result.get("layer_c") or {}
        layer_d: tuple = cycle_result.get("layer_d") or (None, None)

        layer_b_signal: str | None = layer_b.get("signal")
        layer_c_confirmed = layer_c.get("confirmed")
        layer_c_signal: str | None = (
            str(layer_c_confirmed) if layer_c_confirmed is not None else None
        )
        layer_d_result: bool | None = layer_d[0] if layer_d and layer_d[0] is not None else None
        layer_d_reason: str | None = layer_d[1] if layer_d else None

        decision = cycle_result.get("decision", "REJECT")
        if decision == "TRADE":
            decision_reason = cycle_result.get("reject_reason") or ""
        else:
            decision_reason = cycle_result.get("reject_reason") or ""

        with engine.begin() as conn:
            conn.execute(
                ticks.insert().values(
                    timestamp=datetime.now(timezone.utc),
                    pair=cycle_result.get("pair", ""),
                    regime=cycle_result.get("regime", "NO_TRADE"),
                    adx=cycle_result.get("adx"),
                    realized_vol_pct=cycle_result.get("realized_vol_pct"),
                    funding_pct=cycle_result.get("funding_pct"),
                    oi_delta=cycle_result.get("oi_delta"),
                    layer_b_signal=layer_b_signal,
                    layer_c_signal=layer_c_signal,
                    layer_d_result=layer_d_result,
                    layer_d_reason=layer_d_reason,
                    decision=decision,
                    decision_reason=decision_reason,
                    conviction_score=cycle_result.get("conviction_score"),
                    conviction_label=cycle_result.get("conviction_label"),
                    iv=cycle_result.get("iv"),
                    iv_spike=cycle_result.get("iv_spike"),
                )
            )
    except Exception as exc:
        print(f"[logger] log_signal error: {exc}", file=sys.stderr)


def log_reconciliation_record(data: dict) -> int:
    """
    Insert a row into reconciliation_log immediately after a live fill is confirmed
    but before SL/TP placement or log_trade_open().  Returns the new record id.
    Returns -1 on any exception (non-fatal — record is best-effort audit trail).
    """
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            result = conn.execute(
                reconciliation_log.insert().values(
                    pair=data.get("pair"),
                    direction=data.get("direction"),
                    entry_price=data.get("entry_price"),
                    exit_price=data.get("exit_price"),
                    filled_qty=data.get("filled_qty"),
                    size_usd=data.get("size_usd"),
                    pnl_usd=data.get("pnl_usd"),
                    status=data.get("status", "fill_confirmed_pending_log"),
                    client_order_id=data.get("client_order_id"),
                    trade_id=data.get("trade_id"),
                    filled_at=data.get("filled_at"),
                    notes=data.get("notes"),
                ).returning(reconciliation_log.c.id)
            )
            row = result.fetchone()
            return row[0] if row else -1
    except Exception as exc:
        print(f"[logger] log_reconciliation_record error: {exc}", file=sys.stderr)
        return -1


def set_close_client_order_id(trade_id: int, close_client_order_id: str) -> bool:
    """
    Persist the close order's clientOrderId on the trades row BEFORE the close
    order is submitted.  This is the only durable identifier for ghost recovery:
    if the bot crashes after the close fills but before the DB close is written,
    restart can match the exact close order rather than guessing from timestamps.
    Returns True on success, False on any exception.
    """
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                trades.update()
                .where(trades.c.id == trade_id)
                .values(close_client_order_id=close_client_order_id)
            )
        return True
    except Exception as exc:
        print(f"[logger] set_close_client_order_id error: {exc}", file=sys.stderr)
        return False


def update_trade_reconciliation_pending(trade_id: int, pending: bool) -> bool:
    """
    Set reconciliation_pending flag on a trades row.
    Used when a ghost trade's exit price cannot be safely recovered at startup
    so the /resume gate can block trading until the operator resolves it.
    Returns True on success, False on any exception.
    """
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                trades.update()
                .where(trades.c.id == trade_id)
                .values(reconciliation_pending=pending)
            )
        return True
    except Exception as exc:
        print(f"[logger] update_trade_reconciliation_pending error: {exc}", file=sys.stderr)
        return False


def update_reconciliation_record(record_id: int, updates: dict) -> bool:
    """
    Update fields on a reconciliation_log row by id.
    Returns True on success, False on any exception.
    """
    if record_id <= 0:
        return False
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                reconciliation_log.update()
                .where(reconciliation_log.c.id == record_id)
                .values(**updates)
            )
        return True
    except Exception as exc:
        print(f"[logger] update_reconciliation_record error: {exc}", file=sys.stderr)
        return False


def upsert_daily_pnl(date, pnl_delta: float, paper: bool) -> bool:
    """
    Atomically upsert into daily_pnl using INSERT ... ON CONFLICT DO UPDATE.

    Uses a single SQL statement so two concurrent closes on the same day
    cannot both see an absent row and race to INSERT (which would cause one
    to fail on the unique key and silently undercount daily P&L).

    Returns True on success, False on any exception.
    """
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO daily_pnl (date, pnl_usd, trade_count, paper)
                    VALUES (:date, :pnl, 1, :paper)
                    ON CONFLICT (date, paper)
                    DO UPDATE SET
                        pnl_usd     = daily_pnl.pnl_usd     + EXCLUDED.pnl_usd,
                        trade_count = daily_pnl.trade_count  + 1
                    """
                ),
                {"date": date, "pnl": pnl_delta, "paper": paper},
            )
        return True
    except Exception as exc:
        print(f"[logger] upsert_daily_pnl error: {exc}", file=sys.stderr)
        return False
