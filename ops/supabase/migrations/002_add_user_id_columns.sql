-- Phase D migration 002 — add user_id column to every per-user table.
--
-- Strategy: nullable-then-NOT-NULL. This migration only adds the columns
-- (and renames the legacy TEXT user_id columns out of the way on tables
-- that already have one). Migration 003 backfills; 004 flips to NOT NULL.
--
-- Tables that are per-user and get a user_id UUID column:
--   - pm_positions, predictions, market_evaluations,
--     markouts, performance_snapshots, config_change_history,
--     event_log, news_event_log
--
-- Tables that already have a TEXT user_id (single-tenant 'default'):
--   - user_config, pending_suggestions
--   These are migrated by renaming the legacy column to user_id_legacy
--   and adding a fresh user_id UUID — 003 backfills both tables the
--   same way as every other per-user table.
--
-- Global tables (no user_id): feed_health_log, macro_context_log,
--   sentiment_scores — skipped.

-- Legacy TEXT → fresh UUID for tables that already have user_id.
ALTER TABLE user_config           RENAME COLUMN user_id TO user_id_legacy;
ALTER TABLE pending_suggestions   RENAME COLUMN user_id TO user_id_legacy;

-- New UUID columns, all nullable for now.
ALTER TABLE pm_positions          ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE predictions           ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE market_evaluations    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE markouts              ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE performance_snapshots ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE config_change_history ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE event_log             ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE news_event_log        ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE user_config           ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE pending_suggestions   ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
