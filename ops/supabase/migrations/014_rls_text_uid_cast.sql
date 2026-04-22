-- Migration 014 — cast auth.uid() to text in every RLS policy.
--
-- `user_config.user_id` (and every other per-user table) is declared as TEXT
-- in the SQLAlchemy models. Supabase's `auth.uid()` returns UUID. Postgres
-- has no implicit cast between text and uuid, so the policy predicate
-- `user_id = auth.uid()` is ill-typed.
--
-- Symptom before this migration:
--   - upsert WITH CHECK evaluates to false/errors  -> row never persists
--   - subsequent SELECT USING also denies           -> cfg?.onboarded_at is
--     null, /auth/callback redirects to /onboarding forever.
--
-- This migration drops every 006-era policy and re-creates each one with
-- `auth.uid()::text`, which is type-compatible with the TEXT column.

DROP POLICY IF EXISTS pm_positions_select ON pm_positions;
DROP POLICY IF EXISTS pm_positions_insert ON pm_positions;
DROP POLICY IF EXISTS pm_positions_update ON pm_positions;
DROP POLICY IF EXISTS pm_positions_delete ON pm_positions;
CREATE POLICY pm_positions_select ON pm_positions
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY pm_positions_insert ON pm_positions
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY pm_positions_update ON pm_positions
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY pm_positions_delete ON pm_positions
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS predictions_select ON predictions;
DROP POLICY IF EXISTS predictions_insert ON predictions;
DROP POLICY IF EXISTS predictions_update ON predictions;
DROP POLICY IF EXISTS predictions_delete ON predictions;
CREATE POLICY predictions_select ON predictions
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY predictions_insert ON predictions
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY predictions_update ON predictions
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY predictions_delete ON predictions
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS market_evaluations_select ON market_evaluations;
DROP POLICY IF EXISTS market_evaluations_insert ON market_evaluations;
DROP POLICY IF EXISTS market_evaluations_update ON market_evaluations;
DROP POLICY IF EXISTS market_evaluations_delete ON market_evaluations;
CREATE POLICY market_evaluations_select ON market_evaluations
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY market_evaluations_insert ON market_evaluations
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY market_evaluations_update ON market_evaluations
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY market_evaluations_delete ON market_evaluations
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS markouts_select ON markouts;
DROP POLICY IF EXISTS markouts_insert ON markouts;
DROP POLICY IF EXISTS markouts_update ON markouts;
DROP POLICY IF EXISTS markouts_delete ON markouts;
CREATE POLICY markouts_select ON markouts
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY markouts_insert ON markouts
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY markouts_update ON markouts
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY markouts_delete ON markouts
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS performance_snapshots_select ON performance_snapshots;
DROP POLICY IF EXISTS performance_snapshots_insert ON performance_snapshots;
DROP POLICY IF EXISTS performance_snapshots_update ON performance_snapshots;
DROP POLICY IF EXISTS performance_snapshots_delete ON performance_snapshots;
CREATE POLICY performance_snapshots_select ON performance_snapshots
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY performance_snapshots_insert ON performance_snapshots
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY performance_snapshots_update ON performance_snapshots
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY performance_snapshots_delete ON performance_snapshots
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS config_change_history_select ON config_change_history;
DROP POLICY IF EXISTS config_change_history_insert ON config_change_history;
DROP POLICY IF EXISTS config_change_history_update ON config_change_history;
DROP POLICY IF EXISTS config_change_history_delete ON config_change_history;
CREATE POLICY config_change_history_select ON config_change_history
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY config_change_history_insert ON config_change_history
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY config_change_history_update ON config_change_history
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY config_change_history_delete ON config_change_history
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS event_log_select ON event_log;
DROP POLICY IF EXISTS event_log_insert ON event_log;
DROP POLICY IF EXISTS event_log_update ON event_log;
DROP POLICY IF EXISTS event_log_delete ON event_log;
CREATE POLICY event_log_select ON event_log
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY event_log_insert ON event_log
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY event_log_update ON event_log
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY event_log_delete ON event_log
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS news_event_log_select ON news_event_log;
DROP POLICY IF EXISTS news_event_log_insert ON news_event_log;
DROP POLICY IF EXISTS news_event_log_update ON news_event_log;
DROP POLICY IF EXISTS news_event_log_delete ON news_event_log;
CREATE POLICY news_event_log_select ON news_event_log
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY news_event_log_insert ON news_event_log
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY news_event_log_update ON news_event_log
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY news_event_log_delete ON news_event_log
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS user_config_select ON user_config;
DROP POLICY IF EXISTS user_config_insert ON user_config;
DROP POLICY IF EXISTS user_config_update ON user_config;
DROP POLICY IF EXISTS user_config_delete ON user_config;
CREATE POLICY user_config_select ON user_config
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY user_config_insert ON user_config
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY user_config_update ON user_config
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY user_config_delete ON user_config
    FOR DELETE USING (user_id = auth.uid()::text);

DROP POLICY IF EXISTS pending_suggestions_select ON pending_suggestions;
DROP POLICY IF EXISTS pending_suggestions_insert ON pending_suggestions;
DROP POLICY IF EXISTS pending_suggestions_update ON pending_suggestions;
DROP POLICY IF EXISTS pending_suggestions_delete ON pending_suggestions;
CREATE POLICY pending_suggestions_select ON pending_suggestions
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY pending_suggestions_insert ON pending_suggestions
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY pending_suggestions_update ON pending_suggestions
    FOR UPDATE USING (user_id = auth.uid()::text) WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY pending_suggestions_delete ON pending_suggestions
    FOR DELETE USING (user_id = auth.uid()::text);
