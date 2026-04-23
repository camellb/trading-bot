-- 017_learning_reports.sql
--
-- One row per learning cycle (every LEARNING_CYCLE_TRADE_INTERVAL settled
-- trades). Stores the user-facing report text (thesis + deterministic tables
-- + footer), the admin-facing text (adds raw model-reasoning excerpts for
-- audit), and the deterministic data block as JSONB so future dashboards can
-- re-render without re-querying diagnostics.
--
-- User rows are scoped by RLS policy user_id = auth.uid()::text. Admin views
-- go through the existing service-role proxy, not through Supabase RLS.

CREATE TABLE IF NOT EXISTS learning_reports (
    id               SERIAL PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id          TEXT NOT NULL,
    mode             TEXT NOT NULL DEFAULT 'simulation',
    settled_count    INTEGER NOT NULL,
    thesis           TEXT,
    summary_user     TEXT NOT NULL,
    summary_admin    TEXT,
    data             JSONB
);

CREATE INDEX IF NOT EXISTS idx_learning_reports_user
    ON learning_reports(user_id, created_at DESC);

-- PostgREST schema cache reload so the web app sees the new table without
-- waiting for the 30s watchdog.
NOTIFY pgrst, 'reload schema';

-- ── Grants for Supabase's `authenticated` role ──────────────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON learning_reports TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE learning_reports_id_seq TO authenticated;

-- ── Row-level security ──────────────────────────────────────────────────────
ALTER TABLE learning_reports ENABLE ROW LEVEL SECURITY;

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
