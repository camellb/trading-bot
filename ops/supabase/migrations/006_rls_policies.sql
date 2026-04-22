-- Phase D migration 006 — enable Row-Level Security and owner-scoped policies.
--
-- Every per-user table is locked down so a session can only see and mutate
-- rows where user_id = auth.uid(). The synthetic default user owns all
-- legacy rows; a real signed-in user only sees their own.
--
-- Global tables (feed_health_log, macro_context_log, sentiment_scores) are
-- intentionally NOT under RLS — they are shared reference data.
--
-- Service-role keys bypass RLS by design (Supabase sets bypassrls on the
-- service_role), so the Railway bot process — which authenticates with the
-- service-role key — continues to read and write every user's rows. End
-- users hitting the API with an anon/JWT session are constrained to their
-- own rows.

-- 1. Enable RLS on every per-user table.
ALTER TABLE pm_positions          ENABLE ROW LEVEL SECURITY;
ALTER TABLE predictions           ENABLE ROW LEVEL SECURITY;
ALTER TABLE market_evaluations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE markouts              ENABLE ROW LEVEL SECURITY;
ALTER TABLE performance_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE config_change_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_log             ENABLE ROW LEVEL SECURITY;
ALTER TABLE news_event_log        ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_config           ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_suggestions   ENABLE ROW LEVEL SECURITY;

-- 2. Owner-scoped policies. One policy per (table, command) pair so the
--    intent is explicit in \d output. USING covers read/update/delete
--    visibility; WITH CHECK covers the new/updated row's user_id.

-- pm_positions
CREATE POLICY pm_positions_select ON pm_positions
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY pm_positions_insert ON pm_positions
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY pm_positions_update ON pm_positions
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY pm_positions_delete ON pm_positions
    FOR DELETE USING (user_id = auth.uid());

-- predictions
CREATE POLICY predictions_select ON predictions
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY predictions_insert ON predictions
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY predictions_update ON predictions
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY predictions_delete ON predictions
    FOR DELETE USING (user_id = auth.uid());

-- market_evaluations
CREATE POLICY market_evaluations_select ON market_evaluations
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY market_evaluations_insert ON market_evaluations
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY market_evaluations_update ON market_evaluations
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY market_evaluations_delete ON market_evaluations
    FOR DELETE USING (user_id = auth.uid());

-- markouts
CREATE POLICY markouts_select ON markouts
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY markouts_insert ON markouts
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY markouts_update ON markouts
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY markouts_delete ON markouts
    FOR DELETE USING (user_id = auth.uid());

-- performance_snapshots
CREATE POLICY performance_snapshots_select ON performance_snapshots
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY performance_snapshots_insert ON performance_snapshots
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY performance_snapshots_update ON performance_snapshots
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY performance_snapshots_delete ON performance_snapshots
    FOR DELETE USING (user_id = auth.uid());

-- config_change_history
CREATE POLICY config_change_history_select ON config_change_history
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY config_change_history_insert ON config_change_history
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY config_change_history_update ON config_change_history
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY config_change_history_delete ON config_change_history
    FOR DELETE USING (user_id = auth.uid());

-- event_log
CREATE POLICY event_log_select ON event_log
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY event_log_insert ON event_log
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY event_log_update ON event_log
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY event_log_delete ON event_log
    FOR DELETE USING (user_id = auth.uid());

-- news_event_log
CREATE POLICY news_event_log_select ON news_event_log
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY news_event_log_insert ON news_event_log
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY news_event_log_update ON news_event_log
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY news_event_log_delete ON news_event_log
    FOR DELETE USING (user_id = auth.uid());

-- user_config
CREATE POLICY user_config_select ON user_config
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY user_config_insert ON user_config
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY user_config_update ON user_config
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY user_config_delete ON user_config
    FOR DELETE USING (user_id = auth.uid());

-- pending_suggestions
CREATE POLICY pending_suggestions_select ON pending_suggestions
    FOR SELECT USING (user_id = auth.uid());
CREATE POLICY pending_suggestions_insert ON pending_suggestions
    FOR INSERT WITH CHECK (user_id = auth.uid());
CREATE POLICY pending_suggestions_update ON pending_suggestions
    FOR UPDATE USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY pending_suggestions_delete ON pending_suggestions
    FOR DELETE USING (user_id = auth.uid());
