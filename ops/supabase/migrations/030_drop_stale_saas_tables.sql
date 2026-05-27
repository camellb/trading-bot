-- ops/supabase/migrations/030_drop_stale_saas_tables.sql
--
-- Drop the 15 SaaS-era tables that pre-date the 2026-04-25 local-first
-- pivot. The bot's authoritative database is now per-user SQLite at
-- <app-data>/delfi.db (see Delfibot/bot/db/models.py). The only table
-- Supabase still needs is `licenses` (purchase ledger written by the
-- Stripe webhook + read by /api/license/check and the new
-- /api/admin/issue-license endpoint).
--
-- Code references for each dropped table live exclusively in dead
-- auth-gated routes under apps/web/app/{dashboard,admin,auth,
-- onboarding,api/profile,api/admin/geoblock} and helpers in
-- apps/web/lib/{view-mode,supabase/proxy,geoblock}, all of which
-- redirect to /auth (no real user can pass) and were never plumbed
-- to the local-first desktop app. They will be removed in a follow-up
-- PR.
--
-- Volume snapshot before drop (psql output 2026-05-27 13:50 UTC):
--     news_event_log         6,469 rows  1392 kB
--     markouts               1,443 rows   408 kB
--     predictions              925 rows  2648 kB
--     market_evaluations       481 rows  1360 kB
--     feed_health_log          428 rows   120 kB
--     pm_positions              32 rows   304 kB
--     pending_suggestions        9 rows    80 kB
--     geoblock_rules             7 rows    64 kB
--     learning_reports           4 rows   128 kB
--     user_config                2 rows    80 kB
--     sentiment_scores           0 rows    16 kB
--     performance_snapshots      0 rows    24 kB
--     config_change_history      0 rows    24 kB
--     macro_context_log          0 rows    16 kB
--     event_log                  0 rows    24 kB
--                                          ~6.4 MB total
--
-- No foreign-key constraints touch any of these from `licenses`
-- (verified via information_schema.table_constraints earlier).
-- Rollback: Supabase Project Settings -> Database -> Backups ->
-- restore the most recent daily snapshot (taken at 00:00 UTC).
--
-- Run with:
--   psql 'postgresql://postgres:<pw>@db.hvlbvbocgncgkkmawigu.supabase.co:5432/postgres' \
--        -v ON_ERROR_STOP=1 \
--        -f ops/supabase/migrations/030_drop_stale_saas_tables.sql

BEGIN;

DROP TABLE IF EXISTS public.config_change_history CASCADE;
DROP TABLE IF EXISTS public.event_log             CASCADE;
DROP TABLE IF EXISTS public.feed_health_log       CASCADE;
DROP TABLE IF EXISTS public.geoblock_rules        CASCADE;
DROP TABLE IF EXISTS public.learning_reports      CASCADE;
DROP TABLE IF EXISTS public.macro_context_log     CASCADE;
DROP TABLE IF EXISTS public.market_evaluations    CASCADE;
DROP TABLE IF EXISTS public.markouts              CASCADE;
DROP TABLE IF EXISTS public.news_event_log        CASCADE;
DROP TABLE IF EXISTS public.pending_suggestions   CASCADE;
DROP TABLE IF EXISTS public.performance_snapshots CASCADE;
DROP TABLE IF EXISTS public.pm_positions          CASCADE;
DROP TABLE IF EXISTS public.predictions           CASCADE;
DROP TABLE IF EXISTS public.sentiment_scores      CASCADE;
DROP TABLE IF EXISTS public.user_config           CASCADE;

-- Sanity check: only `licenses` should remain in public schema.
SELECT relname FROM pg_class
 WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace
 ORDER BY relname;

COMMIT;
