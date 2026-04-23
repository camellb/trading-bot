-- 016_clean_slate_post_rule_change.sql
--
-- Clean-slate reset of trading data after the 2026-04-22 "Back the Forecast"
-- doctrine change (direction-agreement + min_p_win gates, confidence softener,
-- removal of the minimum-expected-return gate).
--
-- Pre-rule-change rows mix data generated under different gating logic and
-- different sizer behaviour. Keeping them would contaminate learning cadence
-- aggregates, calibration reports, and per-archetype ROI. User instruction:
-- "Scrape it, we adjusted the rules recently so I want only the new stuff
-- to be there - clean slate".
--
-- What we truncate:
--   - pm_positions            live + simulated positions
--   - predictions             legacy calibration rows
--   - market_evaluations      per-scan reasoning
--   - markouts                post-resolution markouts
--   - performance_snapshots   daily equity snapshots
--   - pending_suggestions     proposals from learning cadence
--   - macro_context_log       macro-event context captures
--   - news_event_log          news-feed captures
--   - sentiment_scores        sentiment snapshots
--   - learning_reports        50-trade cycle reports (if table exists)
--
-- What we KEEP (explicitly not touched):
--   - user_config             per-user risk/mode/credentials
--   - config_change_history   audit trail of user edits
--   - event_log               system operational log
--   - feed_health_log         feed uptime history
--   - auth / users tables     Supabase-owned
--
-- Idempotent: TRUNCATE IF EXISTS guard, RESTART IDENTITY resets sequences,
-- CASCADE follows FKs into dependent tables. Safe to re-run.

BEGIN;

DO $$
DECLARE
  tbl text;
  tables_to_wipe text[] := ARRAY[
    'pm_positions',
    'predictions',
    'market_evaluations',
    'markouts',
    'performance_snapshots',
    'pending_suggestions',
    'macro_context_log',
    'news_event_log',
    'sentiment_scores',
    'learning_reports'
  ];
BEGIN
  FOREACH tbl IN ARRAY tables_to_wipe LOOP
    IF EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = tbl
    ) THEN
      EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', tbl);
      RAISE NOTICE 'truncated %', tbl;
    ELSE
      RAISE NOTICE 'skipped % (does not exist)', tbl;
    END IF;
  END LOOP;
END $$;

COMMIT;
