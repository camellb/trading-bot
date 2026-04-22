-- Migration 013 — grant table privileges to Supabase's authenticated role.
--
-- Tables created by the Railway bot via SQLAlchemy are owned by the postgres
-- role and inherit no privileges for the `authenticated` role that PostgREST
-- uses when a signed-in user hits the Supabase REST API with their JWT.
-- Without these grants, any INSERT/UPDATE/SELECT from the web app fails with
-- `42501 permission denied for table <name>` before RLS is even consulted.
--
-- RLS (migration 006) still enforces owner-scoped visibility; these grants
-- just let the role reach the table at all.

GRANT USAGE ON SCHEMA public TO authenticated, anon;

-- Per-user tables (RLS-protected) — authenticated can read/write, filtered by RLS.
GRANT SELECT, INSERT, UPDATE, DELETE ON pm_positions          TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON predictions           TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON market_evaluations    TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON markouts              TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON performance_snapshots TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON config_change_history TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON event_log             TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON news_event_log        TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_config           TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON pending_suggestions   TO authenticated;

-- Sequences used by serial/identity columns on the above tables.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;

-- Future tables created in public inherit the same grants automatically,
-- so we don't have to remember this on every new table.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO authenticated;
