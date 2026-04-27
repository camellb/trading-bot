"""
Local SQLite schema for the Delfi desktop app.

Single-user. All `user_id` columns carry the constant string 'local' and
exist purely so the engine modules transferred from the multi-tenant
codebase keep working without a 666-call refactor.

Postgres-isms removed: JSONB → JSON, NOW() → CURRENT_TIMESTAMP,
TIMESTAMP(timezone=True) → DateTime, RLS / GRANT / NOTIFY pgrst /
DO $$ blocks all stripped. SQLite only.

Call create_all_tables() once on startup. Idempotent.
"""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text as sa_text,
)

LOCAL_USER_ID = "local"

metadata = MetaData()


# ── Calibration ──────────────────────────────────────────────────────────────
predictions = Table(
    "predictions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("user_id",        Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("source",         Text, nullable=False),
    Column("subject_key",    Text, nullable=False),
    Column("category",       Text, nullable=True),
    Column("probability",    Float, nullable=False),
    Column("confidence",     Float, nullable=True),
    Column("horizon_hours",  Float, nullable=True),
    Column("reasoning",      Text, nullable=True),
    Column("metadata",       Text, nullable=True),  # JSON-as-text for legacy callers
    Column("trade_id",       Integer, nullable=True),
    Column("resolved_at",       DateTime, nullable=True),
    Column("resolved_outcome",  Integer, nullable=True),
    Column("resolved_pnl_usd",  Float,   nullable=True),
    Column("resolved_note",     Text,    nullable=True),
    Column("venue",          Text, nullable=False,
           server_default=sa_text("'polymarket'")),
)


# ── Polymarket positions ─────────────────────────────────────────────────────
pm_positions = Table(
    "pm_positions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("user_id",       Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("prediction_id", Integer, nullable=True),
    Column("market_id",     Text,    nullable=False),
    Column("condition_id",  Text,    nullable=True),
    Column("slug",          Text,    nullable=True),
    Column("question",      Text,    nullable=False),
    Column("category",      Text,    nullable=True),
    Column("side",          String(3), nullable=False),
    Column("shares",        Float,   nullable=False),
    Column("entry_price",   Float,   nullable=False),
    Column("cost_usd",      Float,   nullable=False),
    Column("claude_probability", Float, nullable=True),
    Column("ev_bps",        Float,   nullable=True),
    Column("confidence",    Float,   nullable=True),
    Column("mode",          String(10), nullable=False),
    Column("status",        String(20), nullable=False,
           server_default=sa_text("'open'")),
    Column("expected_resolution_at", DateTime, nullable=True),
    Column("settled_at",    DateTime, nullable=True),
    Column("settlement_outcome", String(10), nullable=True),
    Column("settlement_price", Float, nullable=True),
    Column("realized_pnl_usd", Float, nullable=True),
    Column("event_slug",    Text, nullable=True),
    Column("market_archetype", Text, nullable=True),
    Column("clob_order_id", Text, nullable=True),
    Column("tx_hash",       Text, nullable=True),
    Column("reasoning",     Text, nullable=True),
    Column("venue",         Text, nullable=False,
           server_default=sa_text("'polymarket'")),
)


# ── Market evaluations cache ─────────────────────────────────────────────────
market_evaluations = Table(
    "market_evaluations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("evaluated_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("user_id",          Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("market_id",        Text, nullable=False),
    Column("condition_id",     Text, nullable=True),
    Column("slug",             Text, nullable=True),
    Column("question",         Text, nullable=False),
    Column("category",         Text, nullable=True),
    Column("market_price_yes", Float, nullable=False),
    Column("claude_probability", Float, nullable=False),
    Column("confidence",       Float, nullable=True),
    Column("ev_bps",           Float, nullable=True),
    Column("recommendation",   Text, nullable=True),
    Column("reasoning",        Text, nullable=True),
    Column("reasoning_short",  Text, nullable=True),
    Column("research_sources", Text, nullable=True),
    Column("prediction_id",    Integer, nullable=True),
    Column("pm_position_id",   Integer, nullable=True),
    Column("market_archetype", Text, nullable=True),
    Column("event_slug",       Text, nullable=True),
    Column("venue",            Text, nullable=False,
           server_default=sa_text("'polymarket'")),
)


# ── Cross-cutting: feed health, events, config, macro, sentiment ─────────────
event_log = Table(
    "event_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("timestamp", DateTime, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("severity", Integer, nullable=True),
    Column("description", Text, nullable=False),
    Column("source", Text, nullable=False),
)

feed_health_log = Table(
    "feed_health_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp", DateTime, nullable=False),
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
    Column("generated_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
)

config_change_history = Table(
    "config_change_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id",    Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("changed_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("param_name", Text, nullable=True),
    Column("old_value", Text, nullable=True),
    Column("new_value", Text, nullable=True),
    Column("reason", Text, nullable=True),
    Column("suggested_by", Text, nullable=True),
    Column("week_start", Date, nullable=True),
    Column("outcome", Text, server_default=sa_text("'pending'"), nullable=True),
)

performance_snapshots = Table(
    "performance_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id",       Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("snapshot_date", Date, nullable=False),
    Column("snapshot_type", Text, nullable=False),
    Column("total_predictions", Integer, nullable=True),
    Column("resolved", Integer, nullable=True),
    Column("brier", Float, nullable=True),
    Column("accuracy", Float, nullable=True),
    Column("realized_pnl_usd", Float, nullable=True),
    Column("by_category", Text, nullable=True),
    Column("config_snapshot", Text, nullable=True),
    Column("notes", Text, nullable=True),
)

news_event_log = Table(
    "news_event_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id",   Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("logged_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("headline",       Text),
    Column("urgency",        Text),
    Column("direction",      Text),
    Column("catalyst_score", Float),
    Column("expires_at",     DateTime),
    Column("source",         Text),
)

markouts = Table(
    "markouts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id",       Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("evaluation_id", Integer, nullable=False),
    Column("market_id", Text, nullable=False),
    Column("checked_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("hours_after", Integer, nullable=False),
    Column("price_yes_at_check", Float, nullable=False),
    Column("price_yes_at_eval", Float, nullable=False),
    Column("claude_probability", Float, nullable=False),
    Column("direction_correct", Boolean, nullable=False),
)


sentiment_scores = Table(
    "sentiment_scores",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scored_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("composite_score", Float),
    Column("composite_label", Text),
    Column("confidence",      Float),
    Column("detail",          Text),
    Column("claude_summary",  Text),
)


# ── Pending learning suggestions ─────────────────────────────────────────────
pending_suggestions = Table(
    "pending_suggestions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("user_id", Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("param_name", Text, nullable=False),
    Column("current_value", Float, nullable=True),
    Column("proposed_value", Float, nullable=True),
    Column("evidence", Text, nullable=True),
    Column("backtest_delta", Float, nullable=True),
    Column("backtest_trades", Integer, nullable=True),
    Column("settled_count_at_creation", Integer, nullable=True),
    Column("status", Text, nullable=False,
           server_default=sa_text("'pending'")),
    Column("resolved_at", DateTime, nullable=True),
    Column("resolved_by", Text, nullable=True),
    Column("metadata", JSON, nullable=True),
)


# ── Learning-cycle review reports ────────────────────────────────────────────
learning_reports = Table(
    "learning_reports",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("user_id", Text, nullable=False,
           server_default=sa_text("'local'")),
    Column("mode", Text, nullable=False,
           server_default=sa_text("'simulation'")),
    Column("settled_count", Integer, nullable=False),
    Column("thesis", Text, nullable=True),
    Column("summary_user", Text, nullable=False),
    Column("summary_admin", Text, nullable=True),
    Column("data", JSON, nullable=True),
)


# ── Single-row local config ──────────────────────────────────────────────────
# Multi-tenant SaaS had a row per user; local has exactly one row keyed
# by user_id='local'. Subscription / Telegram / Polymarket-US columns
# from the SaaS schema are gone. Polymarket private key + Anthropic API
# key live in the OS keychain (`engine/user_config.py`), not the DB.
user_config = Table(
    "user_config",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Text, nullable=False, unique=True,
           server_default=sa_text("'local'")),

    # Sizer-side. V1 doctrine (locked 2026-04-27): side = market
    # favourite, single Delfi-disagreement skip gate, flat archetype-
    # multiplied stake. The V0 columns min_p_win, confidence_full_stake,
    # confidence_override_threshold were dropped when V1 shipped.
    Column("base_stake_pct",         Float, nullable=False,
           server_default=sa_text("0.02")),
    Column("max_stake_pct",          Float, nullable=False,
           server_default=sa_text("0.05")),

    # Circuit breakers.
    Column("daily_loss_limit_pct",   Float, nullable=False,
           server_default=sa_text("0.10")),
    Column("weekly_loss_limit_pct",  Float, nullable=False,
           server_default=sa_text("0.20")),
    Column("drawdown_halt_pct",      Float, nullable=False,
           server_default=sa_text("0.40")),
    Column("streak_cooldown_losses", Integer, nullable=False,
           server_default=sa_text("3")),
    Column("dry_powder_reserve_pct", Float, nullable=False,
           server_default=sa_text("0.20")),

    # Diagnostic-driven overrides.
    Column("cost_assumption_override",     Float, nullable=True),
    Column("archetype_skip_list",          Text,  nullable=True),
    Column("archetype_stake_multipliers",  JSON,  nullable=False,
           server_default=sa_text("'{}'")),

    # Mode + bankroll. Mode: 'simulation' | 'live'.
    Column("mode",            Text, nullable=False,
           server_default=sa_text("'simulation'")),
    Column("starting_cash",   Float, nullable=False,
           server_default=sa_text("1000.0")),
    Column("bot_enabled",     Boolean, nullable=False,
           server_default=sa_text("0")),

    # Polymarket EIP-712 wallet address (the matching private key lives
    # in the OS keychain). Empty string until the user pastes one in.
    Column("wallet_address",  Text, nullable=True),

    # Onboarding.
    Column("tour_completed_at", DateTime, nullable=True),

    Column("created_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
    Column("updated_at", DateTime,
           server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False),
)


def create_all_tables() -> None:
    """Create all tables + indexes if they do not already exist.

    Safe to call on every startup. SQLite-only; the Postgres-specific
    migrations (DO blocks, RLS, grants, NOTIFY pgrst, ALTER TABLE
    rename / drop column) from the SaaS codebase are intentionally not
    here. A fresh local install starts on this schema; if you edit a
    column type you reset your local data file.
    """
    from db.engine import get_engine
    engine = get_engine()
    metadata.create_all(engine, checkfirst=True)

    index_statements = (
        # Calibration.
        "CREATE INDEX IF NOT EXISTS idx_predictions_source "
        "ON predictions(source)",
        "CREATE INDEX IF NOT EXISTS idx_predictions_trade_id "
        "ON predictions(trade_id)",
        "CREATE INDEX IF NOT EXISTS idx_predictions_resolved "
        "ON predictions(resolved_at) WHERE resolved_at IS NOT NULL",
        # PM positions.
        "CREATE INDEX IF NOT EXISTS idx_pm_positions_status "
        "ON pm_positions(status)",
        "CREATE INDEX IF NOT EXISTS idx_pm_positions_market "
        "ON pm_positions(market_id)",
        "CREATE INDEX IF NOT EXISTS idx_pm_positions_mode "
        "ON pm_positions(mode)",
        "CREATE INDEX IF NOT EXISTS idx_pm_positions_prediction "
        "ON pm_positions(prediction_id)",
        "CREATE INDEX IF NOT EXISTS idx_pm_positions_event_slug "
        "ON pm_positions(event_slug) WHERE event_slug IS NOT NULL",
        # Prevent duplicate open positions on the same market within a mode.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_positions_open_market "
        "ON pm_positions(market_id, mode) WHERE status = 'open'",
        # Market evaluations.
        "CREATE INDEX IF NOT EXISTS idx_market_evaluations_market "
        "ON market_evaluations(market_id, evaluated_at DESC)",
        # Markouts.
        "CREATE INDEX IF NOT EXISTS idx_markouts_evaluation "
        "ON markouts(evaluation_id, hours_after)",
        "CREATE INDEX IF NOT EXISTS idx_markouts_checked "
        "ON markouts(checked_at DESC)",
        # Pending suggestions.
        "CREATE INDEX IF NOT EXISTS idx_pending_suggestions_status "
        "ON pending_suggestions(status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pending_suggestions_user "
        "ON pending_suggestions(user_id, status)",
        # Learning reports.
        "CREATE INDEX IF NOT EXISTS idx_learning_reports_user "
        "ON learning_reports(user_id, created_at DESC)",
        # User config singleton lookup.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_config_user "
        "ON user_config(user_id)",
    )

    with engine.begin() as conn:
        for stmt in index_statements:
            conn.execute(sa_text(stmt))

        # Seed the singleton row if absent. Local install always has
        # exactly one row; doing this here means the engine modules can
        # SELECT user_config WHERE user_id='local' on first boot without
        # a separate setup step.
        conn.execute(sa_text(
            "INSERT INTO user_config (user_id) "
            "SELECT 'local' "
            "WHERE NOT EXISTS (SELECT 1 FROM user_config WHERE user_id = 'local')"
        ))
