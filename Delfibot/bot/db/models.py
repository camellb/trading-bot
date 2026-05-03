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

    # Per-user time-to-resolution filter, in DAYS (matches the
    # day-based "By horizon" buckets on the Performance page so
    # users think in one unit). NULL means "no constraint on this
    # side"; the frontend exposes 0 as the null sentinel. When both
    # are set the validator enforces max >= min.
    Column("min_days_to_resolution", Integer, nullable=True),
    Column("max_days_to_resolution", Integer, nullable=True),

    # Legacy: minimum market favourite price required to enter. Kept
    # in the schema for backward compatibility (rolling-upgrade DBs
    # will still have a value here) but no longer read by the engine.
    # Superseded by `skip_market_price_bands` below, which expresses
    # the same gate as a list of disabled bands and supports asymmetric
    # cuts (e.g. block 90-100% YES favourites without blocking 0-10%
    # NO favourites).
    Column("min_market_favourite_price", Float, nullable=True),

    # Disabled market-price bands, JSON-encoded list of [lo, hi] pairs
    # in market_price_yes space (0..1). The sizer skips any market
    # whose `market_price_yes` falls into any band. NULL or empty list
    # = no bands disabled (default). UI exposes 10 10pp toggles in
    # [0.0, 1.0]; the schema accepts arbitrary bands so a future UI
    # could go finer.
    Column("skip_market_price_bands", Text, nullable=True),

    # Per-archetype price band overrides. JSON-encoded dict mapping
    # archetype id -> list of [lo, hi] pairs. The sizer's band gate
    # checks the global skip_market_price_bands list FIRST, then the
    # archetype-specific list (if any), and skips on first hit. So
    # per-archetype bands ADD to the global list rather than replacing
    # it. NULL or empty dict = no archetype-specific overrides.
    Column("archetype_skip_market_price_bands", Text, nullable=True),

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

    # Telegram. Bot token lives in the OS keychain (it's a secret); the
    # chat id is just the recipient identifier so it stays in the DB.
    # Notification prefs is a JSON object of {category: bool} where the
    # categories are NOTIFICATION_CATEGORIES from engine/user_config.py.
    Column("telegram_chat_id",  Text, nullable=True),
    Column("notification_prefs", JSON, nullable=False,
           server_default=sa_text("'{}'")),

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

        # ── In-place column backfills for existing DBs ───────────────────
        # SQLite has no ALTER TABLE ADD COLUMN IF NOT EXISTS, so we probe
        # via PRAGMA table_info and add only the missing columns. This
        # keeps existing local databases working when the user upgrades
        # the desktop bundle without forcing a wipe.
        existing_user_config_cols = {
            r[1] for r in conn.execute(sa_text("PRAGMA table_info(user_config)")).fetchall()
        }
        if "telegram_chat_id" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config ADD COLUMN telegram_chat_id TEXT"
            ))
        if "notification_prefs" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config ADD COLUMN notification_prefs JSON "
                "NOT NULL DEFAULT '{}'"
            ))
        # Per-user time-to-resolution filter (DAYS). The first cut of
        # this feature stored HOURS (column suffix _hours_); we
        # switched to DAYS to match the "By horizon" Performance
        # buckets the user reads in. SQLite >= 3.25 supports RENAME
        # COLUMN, which preserves existing values; we then divide by
        # 24 to convert. If the user had a sub-day value (rounds to
        # 0 days) we set NULL because zero days is the no-constraint
        # sentinel and would otherwise silently invert the filter.
        if "min_hours_to_resolution" in existing_user_config_cols and \
                "min_days_to_resolution" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config RENAME COLUMN "
                "min_hours_to_resolution TO min_days_to_resolution"
            ))
            conn.execute(sa_text(
                "UPDATE user_config "
                "SET min_days_to_resolution = "
                "    CAST(ROUND(min_days_to_resolution / 24.0) AS INTEGER) "
                "WHERE min_days_to_resolution IS NOT NULL"
            ))
            conn.execute(sa_text(
                "UPDATE user_config SET min_days_to_resolution = NULL "
                "WHERE min_days_to_resolution = 0"
            ))
        if "max_hours_to_resolution" in existing_user_config_cols and \
                "max_days_to_resolution" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config RENAME COLUMN "
                "max_hours_to_resolution TO max_days_to_resolution"
            ))
            conn.execute(sa_text(
                "UPDATE user_config "
                "SET max_days_to_resolution = "
                "    CAST(ROUND(max_days_to_resolution / 24.0) AS INTEGER) "
                "WHERE max_days_to_resolution IS NOT NULL"
            ))
            conn.execute(sa_text(
                "UPDATE user_config SET max_days_to_resolution = NULL "
                "WHERE max_days_to_resolution = 0"
            ))
        # Re-probe after the optional RENAME above so the ADD COLUMN
        # branches below see the post-migration column set.
        existing_user_config_cols = {
            r[1] for r in conn.execute(sa_text("PRAGMA table_info(user_config)")).fetchall()
        }
        if "min_days_to_resolution" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config ADD COLUMN "
                "min_days_to_resolution INTEGER"
            ))
        if "max_days_to_resolution" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config ADD COLUMN "
                "max_days_to_resolution INTEGER"
            ))
        if "min_market_favourite_price" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config ADD COLUMN "
                "min_market_favourite_price REAL"
            ))
        if "skip_market_price_bands" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config ADD COLUMN "
                "skip_market_price_bands TEXT"
            ))
            # One-shot migration: legacy `min_market_favourite_price`
            # floor -> equivalent skip bands.
            #
            # SEMANTICS: min_favourite_price = f means "skip markets
            # where max(p, 1-p) < f" - i.e. the COIN-FLIP middle band
            # [1-f, f] in raw market_price_yes space. This rule is
            # SYMMETRIC: a NO bet at market_p_yes=0.30 has favourite
            # price 0.70 and should be ALLOWED at f=0.60, not skipped.
            #
            # Algorithm: round f UP to nearest 0.10 (0.55 -> 0.60),
            # then disable every 10pp bucket whose [lo, hi) range
            # falls entirely inside the symmetric middle [1-f, f].
            #   f=0.60 -> middle [0.40, 0.60] -> [[0.40,0.50],[0.50,0.60]]
            #   f=0.70 -> middle [0.30, 0.70] -> 4 buckets [0.30..0.70]
            rows = conn.execute(sa_text(
                "SELECT user_id, min_market_favourite_price "
                "FROM user_config "
                "WHERE min_market_favourite_price IS NOT NULL "
                "  AND skip_market_price_bands IS NULL"
            )).fetchall()
            for r in rows:
                uid, floor = r[0], float(r[1])
                # Round UP to nearest 0.10 in integer-percent space to
                # avoid float drift (0.6 stored as 0.5999... naively
                # rounds to 0.50).
                pct      = max(50, min(100, int(round(floor * 100))))
                fav_pct  = ((pct + 9) // 10) * 10   # 60, 70, 80, ...
                lo_bound = 100 - fav_pct            # 40, 30, 20, ...
                hi_bound = fav_pct
                bands = []
                for lo_pct in range(lo_bound, hi_bound, 10):
                    bands.append([lo_pct / 100.0, (lo_pct + 10) / 100.0])
                import json as _json
                conn.execute(sa_text(
                    "UPDATE user_config "
                    "SET skip_market_price_bands = :v "
                    "WHERE user_id = :uid"
                ), {"v": _json.dumps(bands), "uid": uid})
        if "archetype_skip_market_price_bands" not in existing_user_config_cols:
            conn.execute(sa_text(
                "ALTER TABLE user_config ADD COLUMN "
                "archetype_skip_market_price_bands TEXT"
            ))

        # Seed the singleton row if absent. Local install always has
        # exactly one row; doing this here means the engine modules can
        # SELECT user_config WHERE user_id='local' on first boot without
        # a separate setup step.
        conn.execute(sa_text(
            "INSERT INTO user_config (user_id) "
            "SELECT 'local' "
            "WHERE NOT EXISTS (SELECT 1 FROM user_config WHERE user_id = 'local')"
        ))
