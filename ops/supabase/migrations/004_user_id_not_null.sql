-- Phase D migration 004 — enforce NOT NULL on every per-user table's user_id.
--
-- 003 backfilled every row; this migration makes the invariant
-- permanent and drops the legacy TEXT columns.

ALTER TABLE pm_positions          ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE predictions           ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE market_evaluations    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE markouts              ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE performance_snapshots ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE config_change_history ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE event_log             ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE news_event_log        ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE user_config           ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE pending_suggestions   ALTER COLUMN user_id SET NOT NULL;

-- user_config had unique(user_id) under the legacy TEXT column. Recreate
-- the constraint on the new UUID column, then drop the legacy column.
ALTER TABLE user_config  ADD CONSTRAINT user_config_user_id_key UNIQUE (user_id);

ALTER TABLE user_config          DROP COLUMN IF EXISTS user_id_legacy;
ALTER TABLE pending_suggestions  DROP COLUMN IF EXISTS user_id_legacy;
