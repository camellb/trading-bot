-- Migration 020 - allow 'legacy' on subscription_plan.
--
-- Legacy is a one-time-pay lifetime plan pitched as a scarcity offer for
-- founding users ($999 flat, pay-once, keep Delfi forever). It's a
-- subscription_plan value alongside 'monthly' and 'annual' so the paywall
-- logic does not need a new column; check current access via
-- subscription_status = 'active' regardless of plan.
--
-- We drop the old constraint then recreate with the expanded set.
-- Guarded with DO $$ ... EXISTS so re-runs are idempotent.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'user_config_subscription_plan_check'
      AND conrelid = 'public.user_config'::regclass
  ) THEN
    ALTER TABLE user_config
      DROP CONSTRAINT user_config_subscription_plan_check;
  END IF;

  ALTER TABLE user_config
    ADD CONSTRAINT user_config_subscription_plan_check
    CHECK (subscription_plan IS NULL
            OR subscription_plan IN ('monthly', 'annual', 'legacy'));
END $$;

NOTIFY pgrst, 'reload schema';
