-- Migration 007 — per-user Telegram credentials.
--
-- In the single-tenant era, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID were
-- process-global env vars consumed at TelegramNotifier construction. For
-- multi-tenant SaaS every user brings their own bot (via @BotFather) and
-- their own chat_id. Both columns are nullable — Telegram notifications
-- are opt-in. When either is NULL the notifier silently no-ops for that
-- user.
--
-- Secrets in Postgres: the columns are plain TEXT. RLS (see migration 006)
-- restricts SELECT/UPDATE to rows where auth.uid() = user_id, so signed-in
-- users only ever see their own token. The service-role bot bypasses RLS
-- to read every user's creds when sending notifications.

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS telegram_bot_token TEXT,
  ADD COLUMN IF NOT EXISTS telegram_chat_id   TEXT;
