-- Migration 021 - reconcile RLS casts with actual column types.
--
-- Background
-- ----------
-- Migration 014 (rls_text_uid_cast) was written under the assumption
-- that every per-user `user_id` column was TEXT. It drops each table's
-- policies and recreates them as `user_id = auth.uid()::text`.
--
-- The live schema (verified 2026-04-24 via information_schema.columns)
-- is actually mixed:
--
--   UUID (10 tables): pm_positions, predictions, market_evaluations,
--                     markouts, performance_snapshots,
--                     config_change_history, event_log, news_event_log,
--                     user_config, pending_suggestions
--   TEXT (1 table):   learning_reports
--
-- Applying migration 014 to the current DB would fail on every UUID
-- table with `operator does not exist: text = uuid`, aborting the whole
-- migration and leaving those tables with NO policies (i.e. RLS on,
-- zero rows visible). Prod survived because 014 never succeeded; the
-- policies that actually exist today still match migration 006 plus
-- 017's learning_reports addition.
--
-- This migration canonicalises that state: drop any leftover policy
-- (so we're not sensitive to prior partial runs) and recreate with the
-- correct cast per column type. Idempotent, safe to re-run.

BEGIN;

-- UUID tables: compare auth.uid() (UUID) directly.
DO $$
DECLARE
    t TEXT;
    uuid_tables TEXT[] := ARRAY[
        'pm_positions',
        'predictions',
        'market_evaluations',
        'markouts',
        'performance_snapshots',
        'config_change_history',
        'event_log',
        'news_event_log',
        'user_config',
        'pending_suggestions'
    ];
BEGIN
    FOREACH t IN ARRAY uuid_tables LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I_select ON %I', t, t);
        EXECUTE format('DROP POLICY IF EXISTS %I_insert ON %I', t, t);
        EXECUTE format('DROP POLICY IF EXISTS %I_update ON %I', t, t);
        EXECUTE format('DROP POLICY IF EXISTS %I_delete ON %I', t, t);

        EXECUTE format(
            'CREATE POLICY %I_select ON %I '
            'FOR SELECT USING (user_id = auth.uid())',
            t, t);
        EXECUTE format(
            'CREATE POLICY %I_insert ON %I '
            'FOR INSERT WITH CHECK (user_id = auth.uid())',
            t, t);
        EXECUTE format(
            'CREATE POLICY %I_update ON %I '
            'FOR UPDATE USING (user_id = auth.uid()) '
            'WITH CHECK (user_id = auth.uid())',
            t, t);
        EXECUTE format(
            'CREATE POLICY %I_delete ON %I '
            'FOR DELETE USING (user_id = auth.uid())',
            t, t);
    END LOOP;
END$$;

-- TEXT tables: cast auth.uid() to text.
DROP POLICY IF EXISTS learning_reports_select ON learning_reports;
DROP POLICY IF EXISTS learning_reports_insert ON learning_reports;
DROP POLICY IF EXISTS learning_reports_update ON learning_reports;
DROP POLICY IF EXISTS learning_reports_delete ON learning_reports;

CREATE POLICY learning_reports_select ON learning_reports
    FOR SELECT USING (user_id = auth.uid()::text);
CREATE POLICY learning_reports_insert ON learning_reports
    FOR INSERT WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY learning_reports_update ON learning_reports
    FOR UPDATE USING (user_id = auth.uid()::text)
    WITH CHECK (user_id = auth.uid()::text);
CREATE POLICY learning_reports_delete ON learning_reports
    FOR DELETE USING (user_id = auth.uid()::text);

COMMIT;
