-- Migration 011 — per-user Polymarket credentials.
--
-- Moves Polymarket API key / secret / passphrase and the user's Polygon
-- wallet address off localStorage (where they lived in the single-tenant
-- era) and onto user_config. Each user holds their own credentials; the
-- bot executor refuses to operate in 'live' mode if any of the required
-- creds (api_key, api_secret, wallet_address) are missing for that user.
--
-- Passphrase is optional (Polymarket only issues one when the user sets
-- one at key-creation time).
--
-- NOTE on storage: credentials are stored in plaintext today. The bot
-- already treats the database as trusted (it holds telegram_bot_token
-- via migration 007). Column-level encryption (pgsodium) is a later
-- migration; this one doesn't preclude it.

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS polymarket_api_key TEXT;

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS polymarket_api_secret TEXT;

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS polymarket_passphrase TEXT;

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS wallet_address TEXT;
