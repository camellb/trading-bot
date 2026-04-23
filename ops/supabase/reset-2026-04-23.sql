-- One-shot reset: starting Delfi from zero on 2026-04-23.
--
-- What this does:
--   1. Clears every per-user trading table (positions, predictions, markouts,
--      performance snapshots, suggestions, config history, event log, news log).
--   2. Resets every user_config row to starting_cash=1000, bot_enabled=false
--      so no one resumes live trading on stale settings.
--   3. Drops legacy crypto tables from the earlier iteration of the bot
--      (trades, positions, ticks, daily_pnl, backtest_*).
--
-- What it keeps:
--   - market_evaluations        (shared cost, visibility filtered by
--                                user.created_at at read time)
--   - feed_health_log           (infra diagnostics)
--   - macro_context_log         (infra diagnostics)
--   - sentiment_scores          (shared overlay feed)
--   - user_config rows          (updated, not deleted, to preserve accounts)
--
-- Run once:
--     psql "$DATABASE_URL" -f ops/supabase/reset-2026-04-23.sql
--
-- Wrapped in a single transaction so a failure anywhere rolls everything back.

BEGIN;

-- Per-user trading data.
TRUNCATE TABLE pm_positions              RESTART IDENTITY CASCADE;
TRUNCATE TABLE predictions               RESTART IDENTITY CASCADE;
TRUNCATE TABLE markouts                  RESTART IDENTITY CASCADE;
TRUNCATE TABLE performance_snapshots     RESTART IDENTITY CASCADE;
TRUNCATE TABLE pending_suggestions       RESTART IDENTITY CASCADE;
TRUNCATE TABLE config_change_history     RESTART IDENTITY CASCADE;
TRUNCATE TABLE event_log                 RESTART IDENTITY CASCADE;
TRUNCATE TABLE news_event_log            RESTART IDENTITY CASCADE;

-- Reset bankroll + bot toggle for every onboarded user.
UPDATE user_config
   SET starting_cash = 1000.00,
       bot_enabled   = FALSE;

-- Legacy crypto tables from the pre-Polymarket iteration.
DROP TABLE IF EXISTS backtest_signals CASCADE;
DROP TABLE IF EXISTS backtest_trades  CASCADE;
DROP TABLE IF EXISTS backtest_runs    CASCADE;
DROP TABLE IF EXISTS daily_pnl        CASCADE;
DROP TABLE IF EXISTS ticks            CASCADE;
DROP TABLE IF EXISTS positions        CASCADE;
DROP TABLE IF EXISTS trades           CASCADE;

COMMIT;
