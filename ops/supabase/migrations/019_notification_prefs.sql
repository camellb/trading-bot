-- Migration 019 - per-category Telegram notification preferences.
--
-- Users were asking for finer control over what Delfi pings them about.
-- One JSONB column holds a map {category: bool}. Missing keys default to
-- TRUE (send it), matching the pre-migration behaviour so an un-migrated
-- row continues to receive everything. This mirrors the archetype_stake
-- _multipliers pattern: JSONB with graceful degradation.
--
-- Categories (v1):
--   position_opened      every new position
--   position_settled     every resolution (win/loss)
--   daily_summary        end-of-day recap
--   weekly_summary       end-of-week recap
--   calibration          proposed calibration change + apply/reject
--   risk_event           circuit-breaker trips (daily cap, drawdown, cooldown)
--
-- UI writes only the keys the user has toggled off; the decoder treats
-- anything missing as "on". This keeps the JSON tidy and makes future
-- additions backwards-compatible.

ALTER TABLE user_config
  ADD COLUMN IF NOT EXISTS notification_prefs JSONB NOT NULL DEFAULT '{}'::jsonb;

NOTIFY pgrst, 'reload schema';
