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
    UniqueConstraint,
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
    # Polygon CTF.redeemPositions tx hash, set by the settler when the
    # bot auto-redeems a winning live position on resolution. Distinct
    # from `tx_hash` (which is the order-fill hash from open). NULL on
    # simulation rows, on losers, and on live winners that haven't been
    # redeemed yet (kill switch on, missing creds, RPC error).
    Column("redeem_tx_hash", Text, nullable=True),
    Column("reasoning",     Text, nullable=True),
    Column("venue",         Text, nullable=False,
           server_default=sa_text("'polymarket'")),
    # Per-trade richness (added 2026-05-06 for analysis-dimension
    # purposes). Nullable because rows from before the migration
    # don't have these. Both come straight off the PolyMarket
    # gamma row at trade time:
    #   volume_24h_at_entry: 24-hour CLOB dollar volume. Lets us
    #     slice ROI by thinness - thin markets eat spread.
    #   liquidity_at_entry: gamma's `liquidityNum`. Proxy for the
    #     orderbook depth at entry without a separate RPC.
    # True bid-ask spread requires a separate orderbook fetch and
    # is deferred to a later commit.
    Column("volume_24h_at_entry", Float, nullable=True),
    Column("liquidity_at_entry",  Float, nullable=True),
    # Current mark-to-market value of the position. Written by the
    # exit-policy job in polymarket_runner.evaluate_open_positions
    # every 60s: shares * current_bid (gamma outcomePrices midpoint
    # for the held side). NULL on rows from before this migration,
    # on positions whose market has gone closed/illiquid, and on the
    # first 60s after open. Read by pm_executor.get_portfolio_stats
    # via COALESCE(current_value_usd, cost_usd) so the Dashboard's
    # "Locked Capital" tile matches Polymarket's "Portfolio" number
    # instead of showing the cost basis.
    Column("current_value_usd", Float, nullable=True),
    # ── Early-exit tracking ─────────────────────────────────────────────
    # Filled when the exit-policy engine closes a position before its
    # natural settlement. `closed_at` is the timestamp of the SELL fill.
    # `close_reason` is a short code: 'take_profit' | 'stop_loss' |
    # 'time_decay'. `close_clob_order_id` is the Polymarket order id of
    # the SELL; `close_tx_hash` is the Polygon tx hash once the match
    # is mined. NULL on simulation rows and on every live position that
    # closed via natural settlement.
    Column("closed_at",              DateTime, nullable=True),
    Column("close_reason",           Text,     nullable=True),
    Column("close_clob_order_id",    Text,     nullable=True),
    Column("close_tx_hash",          Text,     nullable=True),
    # Counterfactual payout: when an early-exited position's market
    # eventually resolves naturally, the resolver fills this in with
    # what the position WOULD have paid out had we held to settlement
    # (shares × settlement_price - cost_usd). Diff against
    # realized_pnl_usd tells the Intelligence page whether the exit
    # was profitable or premature. NULL until the market resolves.
    Column("counterfactual_pnl_usd", Float,    nullable=True),
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
    # The trading mode the bot was in when this evaluation ran.
    # Without this column, the Dashboard / Positions / Performance
    # views can't show clean per-mode skipped counts — sim and live
    # data bleed together for any user who switches modes. Default
    # 'simulation' is the safe backfill for legacy rows: every
    # evaluation that existed before this column predates the
    # live-trading roll-out.
    Column("mode",             Text, nullable=False,
           server_default=sa_text("'simulation'")),
    # Resolved outcome of the underlying Polymarket market, filled
    # in by the skipped-eval resolver once the market closes.
    # NULL while the market is still trading. Values: 'YES', 'NO',
    # 'INVALID'. Used to surface "would Delfi have won?" counter-
    # factuals on the Intelligence page so the user can audit the
    # forecaster's skip decisions. Independent of pm_positions: a
    # skipped evaluation never opens a position, so the outcome
    # has to be back-filled from gamma rather than read from the
    # position-settler.
    Column("settlement_outcome", Text, nullable=True),
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
    # Bookmark constraint: one review per (user, mode, settled_count).
    # `engine.review_report.save_report` does INSERT ... ON CONFLICT
    # (user_id, mode, settled_count) DO NOTHING, which silently fails
    # against any DB that lacks this matching constraint.
    UniqueConstraint("user_id", "mode", "settled_count",
                     name="uq_learning_reports_bookmark"),
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
    # Whether to ENFORCE max_stake_pct as a hard per-trade cap. OFF by
    # default. At $1000+ bankrolls the cap protects against accidental
    # over-staking (a buggy archetype multiplier could 10x the bet); at
    # small bankrolls it makes trading impossible — Polymarket's $1-
    # and-5-share platform floors mean the minimum legal trade is
    # $2.50-$4.75, while a 5% cap on $8 bankroll is $0.40. With the
    # cap off, the sizer bumps each live order to whatever Polymarket
    # actually requires; users with bigger capital can switch it on
    # for the cap. User instruction 2026-05-18.
    Column("max_stake_pct_enabled",  Boolean, nullable=False,
           server_default=sa_text("0")),

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

    # ── Exit policy (early close before natural settlement) ─────────────
    # See engine/user_config.py UserConfig dataclass for field semantics.
    # Master switch defaults OFF so existing users see no behavior change
    # until they opt in via Settings → Risk → Exit policy.
    Column("exit_policy_enabled", Boolean, nullable=False,
           server_default=sa_text("0")),
    Column("take_profit_enabled", Boolean, nullable=False,
           server_default=sa_text("1")),
    Column("take_profit_threshold_pct", Float, nullable=False,
           server_default=sa_text("0.5")),
    Column("stop_loss_enabled", Boolean, nullable=False,
           server_default=sa_text("1")),
    Column("stop_loss_threshold_pct", Float, nullable=False,
           server_default=sa_text("0.3")),
    Column("stop_loss_min_time_remaining_pct", Float, nullable=False,
           server_default=sa_text("0.2")),
    Column("time_decay_enabled", Boolean, nullable=False,
           server_default=sa_text("0")),
    # Defaults retuned 2026-05-18: 120h matches the bot's 7-day market
    # horizon (was 72h), ±5% is the genuine flat band (was ±10%),
    # 15min safety floor avoids the spread+fees pothole near
    # settlement (was 5min). See UserConfig dataclass for rationale.
    Column("time_decay_max_hours", Integer, nullable=False,
           server_default=sa_text("120")),
    Column("time_decay_flat_band_pct", Float, nullable=False,
           server_default=sa_text("0.05")),
    Column("exit_min_time_to_resolution_minutes", Integer, nullable=False,
           server_default=sa_text("15")),

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
        # Backfills the UNIQUE constraint on (user_id, mode,
        # settled_count) for DBs created before that constraint was
        # added to the Table declaration. Without it, save_report's
        # INSERT ... ON CONFLICT (...) DO NOTHING raises
        # "ON CONFLICT clause does not match any PRIMARY KEY or
        # UNIQUE constraint" and every review-cycle save fails
        # silently.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_reports_bookmark "
        "ON learning_reports(user_id, mode, settled_count)",
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
            # ─────────────────────────────────────────────────────────
            # MIGRATION: legacy `min_market_favourite_price` -> bands
            # ─────────────────────────────────────────────────────────
            # INPUT axis : favourite price = max(p, 1-p), range [0.50, 1.00]
            # OUTPUT axis: raw market_p_yes,              range [0.00, 1.00]
            # INVARIANT  : "skip iff favourite price < f"
            #              <=> "skip iff (1 - f) < market_p_yes < f"
            #              <=> output bands cover ONLY the symmetric
            #                  middle (1 - f, f) on the raw axis.
            #
            # The two axes are NOT the same. Migration v1 silently
            # produced left-only bands [0, f] and turned the bot YES-only.
            # A 30s sanity-check would have caught it. See
            # 50_Feedback/be_critical_and_intentional.md.
            #
            # Concrete check for f=0.60:
            #   p=0.30 (NO favourite, fav=0.70) -> ALLOWED (✓ not in any band)
            #   p=0.55 (weak YES,    fav=0.55) -> SKIPPED (in [0.50, 0.60))
            #   p=0.85 (strong YES,  fav=0.85) -> ALLOWED (✓ not in any band)
            #
            # Algorithm: round f UP to nearest 0.10 (0.55 -> 0.60),
            # then disable every 10pp bucket inside (1 - f, f).
            #   f=0.60 -> middle [0.40, 0.60] -> [[0.40,0.50],[0.50,0.60]]
            #   f=0.70 -> middle [0.30, 0.70] -> 4 buckets covering 0.30..0.70
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
        if "max_stake_pct_enabled" not in existing_user_config_cols:
            # Default 0 (False): sizer bumps to platform minimum on small
            # bankrolls instead of skipping. Existing users with a
            # carefully-tuned max_stake_pct can flip this back on from
            # the Risk page.
            conn.execute(sa_text(
                "ALTER TABLE user_config ADD COLUMN "
                "max_stake_pct_enabled INTEGER NOT NULL DEFAULT 0"
            ))

        # ── pm_positions backfills ──────────────────────────────────────
        # Same PRAGMA-probe pattern as the user_config block above. Adds
        # any column that exists in the metadata definition but not in
        # the live SQLite file. Order matters: probe -> ALTER per column.
        existing_pm_positions_cols = {
            r[1] for r in conn.execute(
                sa_text("PRAGMA table_info(pm_positions)")
            ).fetchall()
        }
        if "redeem_tx_hash" not in existing_pm_positions_cols:
            conn.execute(sa_text(
                "ALTER TABLE pm_positions ADD COLUMN redeem_tx_hash TEXT"
            ))
        if "volume_24h_at_entry" not in existing_pm_positions_cols:
            conn.execute(sa_text(
                "ALTER TABLE pm_positions ADD COLUMN "
                "volume_24h_at_entry REAL"
            ))
        if "liquidity_at_entry" not in existing_pm_positions_cols:
            conn.execute(sa_text(
                "ALTER TABLE pm_positions ADD COLUMN "
                "liquidity_at_entry REAL"
            ))
        if "current_value_usd" not in existing_pm_positions_cols:
            conn.execute(sa_text(
                "ALTER TABLE pm_positions ADD COLUMN "
                "current_value_usd REAL"
            ))

        # ── Early-exit columns (added 2026-05-18, exit-policy feature) ──
        # See engine/user_config.py + db.models.pm_positions definition
        # for field semantics. All NULL for legacy rows. The exit-policy
        # engine writes closed_at/close_reason/close_clob_order_id/
        # close_tx_hash on SELL fill; the resolver writes
        # counterfactual_pnl_usd when an early-exited market eventually
        # settles naturally.
        for col, ddl in (
            ("closed_at",              "DATETIME"),
            ("close_reason",           "TEXT"),
            ("close_clob_order_id",    "TEXT"),
            ("close_tx_hash",          "TEXT"),
            ("counterfactual_pnl_usd", "REAL"),
        ):
            if col not in existing_pm_positions_cols:
                conn.execute(sa_text(
                    f"ALTER TABLE pm_positions ADD COLUMN {col} {ddl}"
                ))

        # ── Exit-policy columns on user_config (same release) ───────────
        # Idempotent re-probe (the earlier user_config probe was at the
        # top of this function; we re-read here because that scope is
        # gone). Defaults match engine/user_config.py UserConfig.
        existing_user_config_cols = {
            r[1] for r in conn.execute(
                sa_text("PRAGMA table_info(user_config)")
            ).fetchall()
        }
        for col, ddl in (
            ("exit_policy_enabled",
             "BOOLEAN NOT NULL DEFAULT 0"),
            ("take_profit_enabled",
             "BOOLEAN NOT NULL DEFAULT 1"),
            ("take_profit_threshold_pct",
             "REAL NOT NULL DEFAULT 0.5"),
            ("stop_loss_enabled",
             "BOOLEAN NOT NULL DEFAULT 1"),
            ("stop_loss_threshold_pct",
             "REAL NOT NULL DEFAULT 0.3"),
            ("stop_loss_min_time_remaining_pct",
             "REAL NOT NULL DEFAULT 0.2"),
            ("time_decay_enabled",
             "BOOLEAN NOT NULL DEFAULT 0"),
            # See user_config Table() defs above for the 2026-05-18
            # default retune. Keep these in sync.
            ("time_decay_max_hours",
             "INTEGER NOT NULL DEFAULT 120"),
            ("time_decay_flat_band_pct",
             "REAL NOT NULL DEFAULT 0.05"),
            ("exit_min_time_to_resolution_minutes",
             "INTEGER NOT NULL DEFAULT 15"),
        ):
            if col not in existing_user_config_cols:
                conn.execute(sa_text(
                    f"ALTER TABLE user_config ADD COLUMN {col} {ddl}"
                ))

        # ── market_evaluations backfills ────────────────────────────────
        # Add the `mode` column so per-mode skipped counts and Dashboard
        # views can be cleanly mode-scoped (2026-05-16: switching to
        # live for the first time exposed that sim/live data was
        # bleeding together on every screen — user fix required).
        # Legacy rows backfill to 'simulation' because every existing
        # evaluation predates live trading.
        existing_eval_cols = {
            r[1] for r in conn.execute(
                sa_text("PRAGMA table_info(market_evaluations)")
            ).fetchall()
        }
        if "mode" not in existing_eval_cols:
            conn.execute(sa_text(
                "ALTER TABLE market_evaluations ADD COLUMN mode TEXT "
                "NOT NULL DEFAULT 'simulation'"
            ))
        # Counterfactual outcomes for skipped evaluations. Filled in
        # asynchronously after the market resolves so the user can see
        # "would Delfi have won this trade if it hadn't skipped?" on
        # the Intelligence page. Nullable, no default — only the
        # resolver writes here.
        if "settlement_outcome" not in existing_eval_cols:
            conn.execute(sa_text(
                "ALTER TABLE market_evaluations ADD COLUMN settlement_outcome TEXT"
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
