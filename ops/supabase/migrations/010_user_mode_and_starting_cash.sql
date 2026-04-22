-- Migration 010 — per-user mode + starting cash.
--
-- Multi-tenancy: every user runs their own executor with their own mode
-- (simulation vs. live) and their own bankroll starting point. The global
-- PM_MODE constant and PM_SIMULATION_STARTING_CASH / PM_LIVE_STARTING_CASH
-- defaults are being retired. A user who hasn't set these has no executor
-- state — the dashboard shows zero and the bot places no trades for them.
--
-- NULL starting_cash means "user hasn't chosen yet" — the bot will refuse
-- to size or execute for that user until they do. Onboarding writes both
-- columns as part of the flow.

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS mode TEXT;

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS starting_cash NUMERIC(12, 2);

-- Guard: mode must be one of the two legal values when set.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'user_config_mode_check'
      AND conrelid = 'public.user_config'::regclass
  ) THEN
    ALTER TABLE user_config
      ADD CONSTRAINT user_config_mode_check
      CHECK (mode IS NULL OR mode IN ('simulation', 'live'));
  END IF;
END $$;
