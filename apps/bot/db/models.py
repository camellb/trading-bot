"""
PostgreSQL schema - Polymarket prediction-market bot.

Reads DATABASE_URL from environment. Call create_all_tables() to create all
tables if they do not already exist. Safe to call on every startup.

Legacy crypto tables (trades, positions, ticks, daily_pnl, backtest_*) are
intentionally no longer declared here - they remain in the database from
prior runs but are orphaned from the application. Drop them manually once
you're sure you no longer want the history:

    DROP TABLE IF EXISTS trades, positions, ticks, daily_pnl,
        backtest_runs, backtest_trades, backtest_signals CASCADE;
"""

import sys

from sqlalchemy import (
    MetaData,
    Table,
    Column,
    Integer,
    String,
    Text,
    Float,
    Boolean,
    Date,
    TIMESTAMP,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()


# ── Calibration ──────────────────────────────────────────────────────────────
# Every prediction Claude makes - Polymarket evaluations, future Kalshi/Manifold
# evaluations, backtest decisions. Scored against resolved outcomes to produce
# Brier score, reliability diagrams, per-category attribution.
predictions = Table(
    "predictions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
    # 'polymarket' | 'polymarket_live' | 'polymarket_simulation' | 'backtest' | …
    Column("source",         Text, nullable=False),
    # Stable key per prediction (e.g. 'polymarket:0xabc…').
    Column("subject_key",    Text, nullable=False),
    # Market category (politics, macro, sports, crypto, etc.).
    Column("category",       Text, nullable=True),
    # Probability YES resolves true (0..1) - the calibrated probability.
    Column("probability",    Float, nullable=False),
    # Claude's self-stated confidence in the estimate.
    Column("confidence",     Float, nullable=True),
    # Hours until expected resolution.
    Column("horizon_hours",  Float, nullable=True),
    Column("reasoning",      Text, nullable=True),
    # JSON: market snapshot at prediction time (price, volume, end_date, …).
    Column("metadata",       Text, nullable=True),
    # Optional link to a realised position (pm_positions.id).
    Column("trade_id",       Integer, nullable=True),
    # Resolution (null = unresolved yet).
    Column("resolved_at",       TIMESTAMP(timezone=True), nullable=True),
    # 1 = correct (took a side that matched resolution), 0 = incorrect.
    Column("resolved_outcome",  Integer, nullable=True),
    Column("resolved_pnl_usd",  Float,   nullable=True),
    Column("resolved_note",     Text,    nullable=True),
)


# ── Polymarket positions ─────────────────────────────────────────────────────
# A realised bet (simulation or live) on a Polymarket market.
# Simulation rows simulate fills at observed mid/ask prices; live rows have
# real order ids and tx hashes from the CLOB.
pm_positions = Table(
    "pm_positions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
    # Link back to the prediction that generated this position.
    Column("prediction_id", Integer, nullable=True),
    Column("market_id",     Text,    nullable=False),
    Column("condition_id",  Text,    nullable=True),
    Column("slug",          Text,    nullable=True),
    Column("question",      Text,    nullable=False),
    Column("category",      Text,    nullable=True),
    # 'YES' or 'NO' - which side we bought.
    Column("side",          String(3), nullable=False),
    # Shares purchased (Polymarket shares pay $1 if winning, $0 if losing).
    Column("shares",        Float,   nullable=False),
    # Average fill price (0..1).
    Column("entry_price",   Float,   nullable=False),
    # USD cost = shares * entry_price.
    Column("cost_usd",      Float,   nullable=False),
    # Claude's probability for this side at entry.
    Column("claude_probability", Float, nullable=True),
    # Expected value at entry in basis points (ev × 10 000).
    Column("ev_bps",        Float,   nullable=True),
    Column("confidence",    Float,   nullable=True),
    # 'simulation' | 'live'
    Column("mode",          String(10), nullable=False),
    # 'open' | 'settled' | 'invalid' | 'closed_early'
    Column("status",        String(20), nullable=False,
           server_default=sa_text("'open'")),
    Column("expected_resolution_at", TIMESTAMP(timezone=True), nullable=True),
    # Settlement fields (null until resolved).
    Column("settled_at",    TIMESTAMP(timezone=True), nullable=True),
    # Winning outcome: 'YES' | 'NO' | 'INVALID'.
    Column("settlement_outcome", String(10), nullable=True),
    # $1.00 if our side won, $0.00 if lost, $0.50 if invalid.
    Column("settlement_price", Float, nullable=True),
    # shares * settlement_price - cost_usd.
    Column("realized_pnl_usd", Float, nullable=True),
    # Event group slug for correlation caps (markets in the same event).
    Column("event_slug",    Text, nullable=True),
    # Archetype tag used for category-level calibration analysis.
    Column("market_archetype", Text, nullable=True),
    # Live-mode metadata.
    Column("clob_order_id", Text, nullable=True),
    Column("tx_hash",       Text, nullable=True),
    Column("reasoning",     Text, nullable=True),
)


# ── Market evaluations cache ─────────────────────────────────────────────────
# Snapshot of every Claude evaluation of a Polymarket market. Kept separately
# from `predictions` so we can re-evaluate the same market multiple times
# without polluting the calibration dataset (only first evaluation flows to
# `predictions`).
market_evaluations = Table(
    "market_evaluations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("evaluated_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
    Column("market_id",        Text, nullable=False),
    Column("condition_id",     Text, nullable=True),
    Column("slug",             Text, nullable=True),
    Column("question",         Text, nullable=False),
    Column("category",         Text, nullable=True),
    Column("market_price_yes", Float, nullable=False),
    Column("claude_probability", Float, nullable=False),
    Column("confidence",       Float, nullable=True),
    Column("ev_bps",           Float, nullable=True),
    Column("recommendation",   Text, nullable=True),   # 'BUY_YES' | 'BUY_NO' | 'SKIP'
    Column("reasoning",        Text, nullable=True),
    Column("research_sources", Text, nullable=True),   # JSON array of urls/titles
    Column("prediction_id",    Integer, nullable=True),
    Column("pm_position_id",   Integer, nullable=True),
    Column("market_archetype", Text, nullable=True),
    Column("event_slug",       Text, nullable=True),
)


# ── Cross-cutting: feed health, events, config, macro, sentiment ─────────────
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
    Column("generated_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
)

config_change_history = Table(
    "config_change_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("changed_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
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
    Column("total_predictions", Integer, nullable=True),
    Column("resolved", Integer, nullable=True),
    Column("brier", Float, nullable=True),
    Column("accuracy", Float, nullable=True),
    Column("realized_pnl_usd", Float, nullable=True),
    Column("by_category", Text, nullable=True),      # JSON
    Column("config_snapshot", Text, nullable=True),  # JSON
    Column("notes", Text, nullable=True),
)

news_event_log = Table(
    "news_event_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("logged_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
    Column("headline",       Text),
    Column("urgency",        Text),
    Column("direction",      Text),
    Column("catalyst_score", Float),
    Column("expires_at",     TIMESTAMP(timezone=True)),
    Column("source",         Text),
)

markouts = Table(
    "markouts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("evaluation_id", Integer, nullable=False),
    Column("market_id", Text, nullable=False),
    Column("checked_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
    Column("hours_after", Integer, nullable=False),       # 1, 6, or 24
    Column("price_yes_at_check", Float, nullable=False),
    Column("price_yes_at_eval", Float, nullable=False),
    Column("claude_probability", Float, nullable=False),
    Column("direction_correct", Boolean, nullable=False),  # price moved toward Claude's estimate?
)


sentiment_scores = Table(
    "sentiment_scores",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scored_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
    Column("composite_score", Float),
    Column("composite_label", Text),
    Column("confidence",      Float),
    Column("detail",          Text),
    Column("claude_summary",  Text),
)


# ── Pending learning suggestions ─────────────────────────────────────────────
# Every 50 settled trades the learning cadence proposes user_config tweaks.
# Each suggestion includes the evidence and the backtester's hypothetical
# ROI delta. The dashboard surfaces these with Apply/Skip/Snooze buttons.
# No suggestion ever modifies runtime state on its own - a user Apply must
# flow through the user_config update path.
pending_suggestions = Table(
    "pending_suggestions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
    Column("user_id", Text, nullable=False,
           server_default=sa_text("'default'")),
    Column("param_name", Text, nullable=False),
    Column("current_value", Float, nullable=True),
    Column("proposed_value", Float, nullable=True),
    Column("evidence", Text, nullable=True),
    # Backtester's hypothetical ROI delta on historical evaluations
    # (expressed as a fraction, e.g. 0.02 = +2 pp ROI).
    Column("backtest_delta", Float, nullable=True),
    Column("backtest_trades", Integer, nullable=True),
    # settled_trade_count at the time of creation - used by the learning
    # cadence to decide whether enough new trades have accumulated.
    Column("settled_count_at_creation", Integer, nullable=True),
    # 'pending' | 'applied' | 'skipped' | 'snoozed'
    Column("status", Text, nullable=False,
           server_default=sa_text("'pending'")),
    Column("resolved_at", TIMESTAMP(timezone=True), nullable=True),
    Column("resolved_by", Text, nullable=True),
    # Proposal operation payload. Scalar overwrites need nothing here (the
    # `param_name` + `proposed_value` columns are enough). List-append
    # proposals require {"operation": "list_append", "target_field": <col>,
    # "items": [<label>, ...]} so the apply path knows what to merge into
    # the existing tuple. Missing/NULL == {"operation": "scalar_set"} for
    # backward compatibility with rows written before this column existed.
    Column("metadata", JSONB, nullable=True),
)


# ── Per-user risk configuration ──────────────────────────────────────────────
# Each user configures their own risk tolerance within system-defined bounds.
# The sizer and risk manager read from this table at decision time. The
# dashboard edits rows directly via /api/user-config; changes apply to the
# next evaluation. Defaults match the Delfi doctrine starting values.
user_config = Table(
    "user_config",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Text, nullable=False, unique=True),

    # Sizer-side.
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

    # Diagnostic-driven overrides. Nullable: the learning cadence only fills
    # these in when a proposal is applied; the sizer treats NULL / empty as
    # "use the built-in default".
    Column("cost_assumption_override", Float, nullable=True),
    Column("archetype_skip_list",      Text,  nullable=True),   # CSV

    # Per-user Telegram bot credentials. Opt-in: NULL on either column means
    # the notifier silently no-ops for that user. Every tenant brings their
    # own bot (via @BotFather) and chat_id.
    Column("telegram_bot_token", Text, nullable=True),
    Column("telegram_chat_id",   Text, nullable=True),

    Column("created_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True),
           server_default=sa_text("NOW()"), nullable=False),
)


def create_all_tables() -> None:
    """Create all tables if they do not already exist. Safe to call on every startup."""
    from db.engine import get_engine
    engine = get_engine()
    metadata.create_all(engine, checkfirst=True)

    with engine.begin() as conn:
        # Calibration indexes.
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_predictions_source "
            "ON predictions(source)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_predictions_trade_id "
            "ON predictions(trade_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_predictions_resolved "
            "ON predictions(resolved_at) WHERE resolved_at IS NOT NULL"
        ))
        # PM position indexes.
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_pm_positions_status "
            "ON pm_positions(status)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_pm_positions_market "
            "ON pm_positions(market_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_pm_positions_mode "
            "ON pm_positions(mode)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_pm_positions_prediction "
            "ON pm_positions(prediction_id)"
        ))
        # Migration: add event_slug to existing pm_positions tables.
        conn.execute(sa_text(
            "ALTER TABLE pm_positions ADD COLUMN IF NOT EXISTS event_slug TEXT"
        ))
        conn.execute(sa_text(
            "ALTER TABLE pm_positions ADD COLUMN IF NOT EXISTS market_archetype TEXT"
        ))
        conn.execute(sa_text(
            "ALTER TABLE market_evaluations ADD COLUMN IF NOT EXISTS market_archetype TEXT"
        ))
        conn.execute(sa_text(
            "ALTER TABLE market_evaluations ADD COLUMN IF NOT EXISTS event_slug TEXT"
        ))
        # Migration: historical rename edge_bps → ev_bps. The column stores
        # expected return × 10000 at entry, kept for diagnostic attribution.
        # Under the three-gate doctrine expected return is one gate, not
        # the primary signal. Idempotent - runs once on legacy databases.
        for tbl in ("pm_positions", "market_evaluations"):
            conn.execute(sa_text(
                f"DO $$ BEGIN "
                f"  IF EXISTS (SELECT 1 FROM information_schema.columns "
                f"             WHERE table_name = '{tbl}' "
                f"               AND column_name = 'edge_bps') "
                f"     AND NOT EXISTS (SELECT 1 FROM information_schema.columns "
                f"                     WHERE table_name = '{tbl}' "
                f"                       AND column_name = 'ev_bps') THEN "
                f"    ALTER TABLE {tbl} RENAME COLUMN edge_bps TO ev_bps; "
                f"  END IF; "
                f"END $$;"
            ))
        # Event-group correlation cap index (must come after ALTER TABLE).
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_pm_positions_event_slug "
            "ON pm_positions(event_slug) WHERE event_slug IS NOT NULL"
        ))
        # Partial unique index: prevent duplicate open positions on same market.
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_positions_open_market "
            "ON pm_positions(market_id, mode) WHERE status = 'open'"
        ))
        # Market evaluation index for history lookups.
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_market_evaluations_market "
            "ON market_evaluations(market_id, evaluated_at DESC)"
        ))
        # Markout indexes - fast lookups for pending checks and stats.
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_markouts_evaluation "
            "ON markouts(evaluation_id, hours_after)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_markouts_checked "
            "ON markouts(checked_at DESC)"
        ))
        # User config - unique user_id for multi-user support.
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_config_user "
            "ON user_config(user_id)"
        ))
        # Migration: three-gate doctrine columns + cleanup of deprecated
        # fields from the prior EV-as-primary-gate paradigm. Idempotent -
        # ADD ... IF NOT EXISTS and DROP ... IF EXISTS.
        for col_sql in (
            "ADD COLUMN IF NOT EXISTS cost_assumption_override DOUBLE PRECISION",
            "ADD COLUMN IF NOT EXISTS archetype_skip_list      TEXT",
            "ADD COLUMN IF NOT EXISTS min_p_win                      DOUBLE PRECISION NOT NULL DEFAULT 0.50",
            "ADD COLUMN IF NOT EXISTS confidence_full_stake          DOUBLE PRECISION NOT NULL DEFAULT 0.70",
            "ADD COLUMN IF NOT EXISTS confidence_override_threshold  DOUBLE PRECISION NOT NULL DEFAULT 0.75",
            "ADD COLUMN IF NOT EXISTS telegram_bot_token              TEXT",
            "ADD COLUMN IF NOT EXISTS telegram_chat_id                TEXT",
            "ADD COLUMN IF NOT EXISTS is_admin                        BOOLEAN NOT NULL DEFAULT FALSE",
            "ADD COLUMN IF NOT EXISTS bot_enabled                     BOOLEAN NOT NULL DEFAULT FALSE",
            "ADD COLUMN IF NOT EXISTS tour_completed_at               TIMESTAMPTZ",
            "DROP COLUMN IF EXISTS confidence_skip_floor",
            "DROP COLUMN IF EXISTS min_ev_threshold",
            "DROP COLUMN IF EXISTS probability_cap",
            "DROP COLUMN IF EXISTS ev_bucket_skip_list",
            # Doctrine: Gate 3 (minimum expected return) removed. It skipped
            # heavy-favourite bets where the math still favoured trading.
            "DROP COLUMN IF EXISTS min_expected_return",
            "ALTER COLUMN min_p_win SET DEFAULT 0.50",
        ):
            conn.execute(sa_text(f"ALTER TABLE user_config {col_sql}"))
        # Supabase's PostgREST caches the schema and won't surface newly added
        # columns until reloaded. Without this, server actions hitting the new
        # columns fail with `Could not find the '<col>' column ... in the
        # schema cache`. Safe to emit even off Supabase - any Postgres without
        # a pgrst listener treats the NOTIFY as a no-op.
        try:
            conn.execute(sa_text("NOTIFY pgrst, 'reload schema'"))
        except Exception as exc:
            print(f"[db.models] NOTIFY pgrst failed (ignored): {exc}",
                  file=sys.stderr)
        # Backfill default skip list for rows that pre-date the default.
        # Only touches NULL - an explicit empty string means the user
        # deliberately cleared the list and is respected as-is.
        conn.execute(sa_text(
            "UPDATE user_config "
            "SET archetype_skip_list = 'tennis_qualifier,tennis_lower_tier' "
            "WHERE archetype_skip_list IS NULL"
        ))
        # Migration: metadata JSONB column for list-append and future
        # non-scalar proposal operations. Idempotent.
        conn.execute(sa_text(
            "ALTER TABLE pending_suggestions "
            "ADD COLUMN IF NOT EXISTS metadata JSONB"
        ))
        # Pending suggestions - fast filter on status for the dashboard.
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_pending_suggestions_status "
            "ON pending_suggestions(status, created_at DESC)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_pending_suggestions_user "
            "ON pending_suggestions(user_id, status)"
        ))
        # Migration 013: grant table privileges to Supabase's `authenticated`
        # role. SQLAlchemy-created tables are owned by postgres and inherit
        # no privileges for PostgREST's authenticated role, so web-app
        # requests hit `42501 permission denied for table` before RLS even
        # evaluates. RLS (migration 006) still enforces owner scoping.
        # Wrapped in try/except: if the role doesn't exist (e.g. non-Supabase
        # Postgres), skip instead of failing startup.
        try:
            conn.execute(sa_text("GRANT USAGE ON SCHEMA public TO authenticated, anon"))
            for tbl in (
                "pm_positions",
                "predictions",
                "market_evaluations",
                "markouts",
                "performance_snapshots",
                "config_change_history",
                "event_log",
                "news_event_log",
                "user_config",
                "pending_suggestions",
            ):
                conn.execute(sa_text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO authenticated"
                ))
            conn.execute(sa_text(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated"
            ))
            conn.execute(sa_text(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated"
            ))
            conn.execute(sa_text(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO authenticated"
            ))
        except Exception as exc:
            print(f"[db] skipping authenticated grants (non-Supabase?): {exc}", file=sys.stderr)
        # Migration 014: RLS policies use auth.uid() (UUID) against user_id
        # (TEXT). Postgres has no text=uuid operator, so the policies either
        # error or silently deny. Cast to ::text so the predicate actually
        # evaluates. Drop-and-recreate is idempotent and cheap.
        try:
            per_user_tables = (
                "pm_positions",
                "predictions",
                "market_evaluations",
                "markouts",
                "performance_snapshots",
                "config_change_history",
                "event_log",
                "news_event_log",
                "user_config",
                "pending_suggestions",
            )
            for tbl in per_user_tables:
                for cmd in ("select", "insert", "update", "delete"):
                    conn.execute(sa_text(
                        f"DROP POLICY IF EXISTS {tbl}_{cmd} ON {tbl}"
                    ))
                conn.execute(sa_text(
                    f"CREATE POLICY {tbl}_select ON {tbl} "
                    f"FOR SELECT USING (user_id = auth.uid()::text)"
                ))
                conn.execute(sa_text(
                    f"CREATE POLICY {tbl}_insert ON {tbl} "
                    f"FOR INSERT WITH CHECK (user_id = auth.uid()::text)"
                ))
                conn.execute(sa_text(
                    f"CREATE POLICY {tbl}_update ON {tbl} "
                    f"FOR UPDATE USING (user_id = auth.uid()::text) "
                    f"WITH CHECK (user_id = auth.uid()::text)"
                ))
                conn.execute(sa_text(
                    f"CREATE POLICY {tbl}_delete ON {tbl} "
                    f"FOR DELETE USING (user_id = auth.uid()::text)"
                ))
        except Exception as exc:
            print(f"[db] skipping RLS text-cast refresh (non-Supabase?): {exc}", file=sys.stderr)
