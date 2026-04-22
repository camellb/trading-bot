-- Migration 015 — per-user bot on/off switch + first-run tour completion.
--
-- bot_enabled:
--   Brand-new users land on /dashboard with the bot paused. They explicitly
--   click "Start bot" once they've learned what the product does. PMAnalyst
--   checks this before ever opening a position for a user.
--
-- tour_completed_at:
--   The first-run product tour (7 cards: welcome -> sim vs live -> risk ->
--   self-improvement/Intelligence -> Telegram -> Polymarket) shows once per
--   user. Timestamp is written when they dismiss the final card.

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS bot_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS tour_completed_at TIMESTAMPTZ;
