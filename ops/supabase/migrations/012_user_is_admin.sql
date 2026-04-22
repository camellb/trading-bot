-- Migration 012 — flag admins on user_config.
--
-- Admin is additive: a normal signed-in user still has their own
-- /dashboard, their own Polymarket creds, their own positions. When
-- is_admin = TRUE they additionally reach /admin/* (gated server-side
-- in the web layout) and /api/admin/* (gated in bot_api via
-- _require_admin).
--
-- Bootstrap a user to admin:
--   UPDATE user_config SET is_admin = TRUE WHERE user_id = '<auth.users.id>';

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
