-- Migration 009 — capture the user's display name from onboarding.
--
-- First-time users are now required to enter their name before the
-- mode / bankroll / risk steps. We persist it on user_config so the
-- dashboard can greet them and the weekly review emails have a name
-- to address.

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS display_name TEXT;
