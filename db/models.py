"""
PostgreSQL schema definitions using SQLAlchemy Core (not ORM).

Reads DATABASE_URL from environment. Call create_all_tables() to create all
tables if they do not already exist. Safe to call on every startup.
"""

import os
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    Float,
    Boolean,
    Date,
    TIMESTAMP,
    ForeignKey,
    UniqueConstraint,
    text as sa_text,
)

metadata = MetaData()

ticks = Table(
    "ticks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp", TIMESTAMP(timezone=True), nullable=False),
    Column("pair", Text, nullable=False),
    Column("regime", Text, nullable=False),
    Column("adx", Float),
    Column("realized_vol_pct", Float),
    Column("funding_pct", Float),
    Column("oi_delta", Float),
    Column("layer_b_signal", Text),
    Column("layer_c_signal", Text),
    Column("layer_d_result", Boolean),
    Column("layer_d_reason", Text),
    Column("decision", Text),
    Column("decision_reason", Text),
    Column("conviction_score", Float),
    Column("conviction_label", Text),
    Column("iv", Float),
    Column("iv_spike", Boolean),
)

trades = Table(
    "trades",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp_open", TIMESTAMP(timezone=True), nullable=False),
    Column("timestamp_close", TIMESTAMP(timezone=True), nullable=True),
    Column("pair", Text, nullable=False),
    Column("direction", Text, nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("exit_price", Float, nullable=True),
    Column("size_usd", Float, nullable=False),
    Column("stop_loss", Float, nullable=False),
    Column("take_profit", Float, nullable=False),
    Column("regime_at_entry", Text, nullable=False),
    Column("pnl_usd", Float, nullable=True),
    Column("close_reason", Text, nullable=True),
    Column("paper", Boolean, nullable=False),
    Column("thesis", Text, nullable=True),
    Column("trigger_event", Text, nullable=True),
    Column("filled_qty", Float, nullable=True),
    Column("playbook", String(30), nullable=True),
    Column("time_horizon_days", Float, nullable=True),
    Column("catalyst", Text, nullable=True),
    Column("invalidation", Text, nullable=True),
    Column("primary_signal", Text, nullable=True),
    Column("risk_reward", Float, nullable=True),
    Column("market_condition", String(30), nullable=True),
    Column("exit_type", String(30), nullable=True),
    Column("what_happened", Text, nullable=True),
    Column("reconciliation_pending", Boolean, nullable=True, default=False),
    Column("client_order_id", String(100), nullable=True),
    Column("close_client_order_id", String(100), nullable=True),
)

positions = Table(
    "positions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trade_id", Integer, ForeignKey("trades.id"), nullable=False),
    Column("pair", Text, nullable=False),
    Column("direction", Text, nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("size_usd", Float, nullable=False),
    Column("stop_loss", Float, nullable=False),
    Column("take_profit", Float, nullable=False),
    Column("open_at", TIMESTAMP(timezone=True), nullable=False),
    Column("paper", Boolean, nullable=False),
)

daily_pnl = Table(
    "daily_pnl",
    metadata,
    Column("date", Date, primary_key=True),
    Column("pnl_usd", Float, nullable=False),
    Column("trade_count", Integer, nullable=False),
    Column("paper", Boolean, primary_key=True, nullable=False),
    UniqueConstraint("date", "paper", name="uq_daily_pnl_date_paper"),
)

event_log = Table(
    "event_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp", TIMESTAMP(timezone=True), nullable=False),
    Column("event_type", Text, nullable=False),
    Column("severity", Integer, nullable=True),
    Column("description", Text, nullable=False),
    Column("source", Text, nullable=False),
)

feed_health_log = Table(
    "feed_health_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp", TIMESTAMP(timezone=True), nullable=False),
    Column("feed_name", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("detail", Text, nullable=True),
)


macro_context_log = Table(
    "macro_context_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("date", Date, nullable=False),
    Column("sentiment", Text, nullable=True),
    Column("confidence", Float, nullable=True),
    Column("risk_multiplier", Float, nullable=True),
    Column("key_events", Text, nullable=True),
    Column("reasoning", Text, nullable=True),
    Column("watch_for", Text, nullable=True),
    Column("suggested_cap_adjustment", Float, nullable=True),
    Column(
        "generated_at",
        TIMESTAMP(timezone=True),
        server_default=sa_text("NOW()"),
        nullable=False,
    ),
)

strategy_performance = Table(
    "strategy_performance",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("week_start", Date, nullable=False),
    Column("playbook", Text, nullable=False),
    Column("trades", Integer, nullable=False),
    Column("wins", Integer, nullable=False),
    Column("avg_pnl", Float, nullable=True),
    Column("no_trade_pct", Float, nullable=True),
    Column("recommendation", Text, nullable=True),
    Column(
        "recorded_at",
        TIMESTAMP(timezone=True),
        server_default=sa_text("NOW()"),
        nullable=False,
    ),
)


config_change_history = Table(
    "config_change_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "changed_at",
        TIMESTAMP(timezone=True),
        server_default=sa_text("NOW()"),
        nullable=False,
    ),
    Column("param_name", Text, nullable=True),
    Column("old_value", Text, nullable=True),
    Column("new_value", Text, nullable=True),
    Column("reason", Text, nullable=True),
    Column("suggested_by", Text, nullable=True),   # 'claude' | 'manual'
    Column("week_start", Date, nullable=True),
    Column("outcome", Text, server_default=sa_text("'pending'"), nullable=True),
)

performance_snapshots = Table(
    "performance_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("snapshot_date", Date, nullable=False),
    Column("snapshot_type", Text, nullable=False),  # 'weekly' | 'monthly' | 'quarterly'
    Column("total_trades", Integer, nullable=True),
    Column("win_rate", Float, nullable=True),
    Column("total_pnl", Float, nullable=True),
    Column("avg_pnl_per_trade", Float, nullable=True),
    Column("sharpe_ratio", Float, nullable=True),
    Column("max_drawdown", Float, nullable=True),
    Column("trend_win_rate", Float, nullable=True),
    Column("range_win_rate", Float, nullable=True),
    Column("dominant_regime", Text, nullable=True),
    Column("no_trade_pct", Float, nullable=True),
    Column("config_snapshot", Text, nullable=True),  # JSON of key config values
    Column("notes", Text, nullable=True),
)


backtest_runs = Table(
    "backtest_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "run_at",
        TIMESTAMP(timezone=True),
        server_default=sa_text("NOW()"),
        nullable=False,
    ),
    Column("pairs", Text, nullable=False),           # JSON array of pair strings
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=False),
    Column("initial_capital", Float, nullable=False),
    Column("total_trades", Integer, nullable=True),
    Column("win_rate", Float, nullable=True),
    Column("total_pnl", Float, nullable=True),
    Column("max_drawdown", Float, nullable=True),
    Column("sharpe_ratio", Float, nullable=True),
    Column("no_trade_pct", Float, nullable=True),    # fraction of bars with NO_TRADE
    Column("notes", Text, nullable=True),
)

backtest_trades = Table(
    "backtest_trades",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("backtest_runs.id"), nullable=False),
    Column("pair", Text, nullable=False),
    Column("direction", Text, nullable=False),
    Column("entry_time", TIMESTAMP(timezone=True), nullable=False),
    Column("exit_time", TIMESTAMP(timezone=True), nullable=True),
    Column("entry_price", Float, nullable=False),
    Column("exit_price", Float, nullable=True),
    Column("size_usd", Float, nullable=False),
    Column("stop_loss", Float, nullable=False),
    Column("take_profit", Float, nullable=False),
    Column("pnl_usd", Float, nullable=True),
    Column("close_reason", Text, nullable=True),     # 'TP' | 'SL' | 'EOD'
    Column("regime_at_entry", Text, nullable=True),
)

backtest_signals = Table(
    "backtest_signals",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("backtest_runs.id"), nullable=False),
    Column("timestamp", TIMESTAMP(timezone=True), nullable=False),
    Column("pair", Text, nullable=False),
    Column("regime", Text, nullable=True),
    Column("layer_b_signal", Text, nullable=True),
    Column("layer_c_confirmed", Boolean, nullable=True),
    Column("layer_d_passed", Boolean, nullable=True),
    Column("decision", Text, nullable=False),        # 'TRADE' | 'REJECT_*'
    Column("reason", Text, nullable=True),
)


sentiment_scores = Table(
    "sentiment_scores",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scored_at", TIMESTAMP(timezone=True), server_default=sa_text("NOW()"), nullable=False),
    Column("price_momentum_score", Float),
    Column("derivatives_score",    Float),
    Column("fear_greed_score",     Float),
    Column("macro_regime_score",   Float),
    Column("news_catalyst_score",  Float),
    Column("composite_score",      Float),
    Column("composite_label",      Text),
    Column("confidence",           Float),
    Column("price_momentum_detail", Text),
    Column("derivatives_detail",    Text),
    Column("fear_greed_detail",     Text),
    Column("macro_regime_detail",   Text),
    Column("news_catalyst_detail",  Text),
    Column("claude_summary",        Text),
)

news_event_log = Table(
    "news_event_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("logged_at", TIMESTAMP(timezone=True), server_default=sa_text("NOW()"), nullable=False),
    Column("headline",       Text),
    Column("urgency",        Text),
    Column("direction",      Text),
    Column("catalyst_score", Float),
    Column("expires_at",     TIMESTAMP(timezone=True)),
    Column("source",         Text),
)


reconciliation_log = Table(
    "reconciliation_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", TIMESTAMP(timezone=True), server_default=sa_text("NOW()"), nullable=False),
    Column("pair", String(30), nullable=True),
    Column("direction", String(10), nullable=True),
    Column("entry_price", Float, nullable=True),
    Column("exit_price", Float, nullable=True),
    Column("filled_qty", Float, nullable=True),
    Column("size_usd", Float, nullable=True),
    Column("pnl_usd", Float, nullable=True),
    # fill_confirmed_pending_log → logged → reconciled
    # fill_confirmed_pending_log → emergency_close_required → reconciled
    Column("status", String(50), nullable=True),
    Column("client_order_id", String(100), nullable=True),
    Column("trade_id", Integer, nullable=True),   # set when log_trade_open succeeds
    Column("filled_at", TIMESTAMP(timezone=True), nullable=True),
    Column("notes", Text, nullable=True),
)


def create_all_tables() -> None:
    """Create all tables if they do not already exist. Safe to call on every startup."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    engine = create_engine(database_url)
    metadata.create_all(engine, checkfirst=True)
    # Add columns introduced after initial schema — safe no-ops on fresh DBs.
    with engine.begin() as conn:
        conn.execute(sa_text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS "
            "reconciliation_pending BOOLEAN DEFAULT FALSE"
        ))
        conn.execute(sa_text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS "
            "client_order_id VARCHAR(100)"
        ))
        conn.execute(sa_text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS "
            "close_client_order_id VARCHAR(100)"
        ))
