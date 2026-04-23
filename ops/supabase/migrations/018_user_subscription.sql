-- Migration 018 - subscription paywall on user_config.
--
-- Every user must have an active subscription before they can reach the
-- onboarding flow or the dashboard. Admins bypass the paywall via is_admin.
--
-- subscription_status:
--   'none'      default. no subscription, no access to dashboard/onboarding.
--   'active'    paid and current. full access.
--   'canceled'  was active, ended. no access, can resubscribe.
--   'past_due'  payment failed. grace behaviour is a later decision.
--
-- subscription_plan:
--   'monthly'   $69.99 / month.
--   'annual'    $52.50 / month, billed yearly. 25% off monthly.
--   NULL        never subscribed.
--
-- The rest of the Stripe columns (customer id, subscription id, current period
-- end) land in a follow-up migration when real Stripe is wired up. For now the
-- paper-pay flow only needs status + plan to gate access.

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'none';

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS subscription_plan TEXT;

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS subscription_started_at TIMESTAMPTZ;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'user_config_subscription_status_check'
      AND conrelid = 'public.user_config'::regclass
  ) THEN
    ALTER TABLE user_config
      ADD CONSTRAINT user_config_subscription_status_check
      CHECK (subscription_status IN ('none', 'active', 'canceled', 'past_due'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'user_config_subscription_plan_check'
      AND conrelid = 'public.user_config'::regclass
  ) THEN
    ALTER TABLE user_config
      ADD CONSTRAINT user_config_subscription_plan_check
      CHECK (subscription_plan IS NULL OR subscription_plan IN ('monthly', 'annual'));
  END IF;
END $$;

-- Grandfather existing onboarded users. They signed up before the paywall
-- was introduced, so we honour their access. New signups hit the default
-- 'none' and have to subscribe before the dashboard unlocks.
UPDATE user_config
  SET subscription_status = 'active',
      subscription_started_at = COALESCE(subscription_started_at, created_at)
  WHERE subscription_status = 'none'
    AND onboarded_at IS NOT NULL;

NOTIFY pgrst, 'reload schema';
