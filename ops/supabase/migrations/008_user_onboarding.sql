-- Migration 008 — track onboarding completion.
--
-- First-time users are routed to /onboarding after auth. Once they finish
-- the flow we stamp onboarded_at, and subsequent logins skip straight to
-- /dashboard. NULL means the user has not completed onboarding yet.

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMPTZ;
