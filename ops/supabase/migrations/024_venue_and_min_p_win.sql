-- Migration 024 - add venue to user_config and per-row tables, plus
-- US-venue credential columns. Also bumps min_p_win default to 0.55 and
-- backfills users still sitting on the previous 0.50 default.
--
-- Background
-- ----------
-- Delfi is going multi-venue to support US users. Polymarket.com (offshore)
-- blocks US IPs; Polymarket US is a CFTC-regulated DCM that accepts them.
-- They are separate platforms with separate liquidity pools, separate APIs,
-- and separate auth mechanisms. Each user picks exactly one at onboarding.
--
-- Design
-- ------
-- 1. `venue` column on user_config (single source of truth for a user's
--    assigned venue), plus on every per-row table (pm_positions,
--    market_evaluations, predictions) so rows are filterable by venue for
--    dashboard, admin, and learning queries.
-- 2. Three new credential columns for Polymarket US. Existing
--    polymarket_api_key / polymarket_api_secret / polymarket_passphrase /
--    wallet_address continue to serve the offshore venue. The executor
--    picks the credential set matching user.venue.
-- 3. Check constraint enforces venue IN ('polymarket', 'polymarket_us').
--    New venues (Kalshi, etc.) extend this CHECK in a future migration.
-- 4. No cross-venue defaults. Existing rows default to 'polymarket' which
--    preserves behaviour for every user created before this migration.
--
-- min_p_win default change
-- ------------------------
-- The sizer's Gate 2 default moves from 0.50 to 0.55. Users still on the
-- old default (0.50 exactly) get bumped; users who tuned it explicitly
-- are left alone. New rows pick up 0.55 via the altered column DEFAULT.

BEGIN;

-- ── 1. Venue column on user_config ──────────────────────────────────────────
ALTER TABLE user_config
    ADD COLUMN IF NOT EXISTS venue TEXT NOT NULL DEFAULT 'polymarket';

-- Check constraint. Wrapped in DO so a re-run doesn't fail on duplicate name.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'user_config_venue_check'
           AND conrelid = 'user_config'::regclass
    ) THEN
        ALTER TABLE user_config
            ADD CONSTRAINT user_config_venue_check
            CHECK (venue IN ('polymarket', 'polymarket_us'));
    END IF;
END $$;

-- ── 2. Polymarket US credentials ────────────────────────────────────────────
-- Nullable, independent of the offshore creds. A user on venue='polymarket_us'
-- needs these populated to trade live; simulation mode works with no creds
-- on either venue.
ALTER TABLE user_config
    ADD COLUMN IF NOT EXISTS polymarket_us_api_key    TEXT,
    ADD COLUMN IF NOT EXISTS polymarket_us_api_secret TEXT,
    ADD COLUMN IF NOT EXISTS polymarket_us_passphrase TEXT;

-- ── 3. Venue column on per-row tables ───────────────────────────────────────
-- Every position, evaluation, and prediction belongs to exactly one venue.
-- Default 'polymarket' is correct for historical rows (everything pre-this
-- migration was offshore).
ALTER TABLE pm_positions
    ADD COLUMN IF NOT EXISTS venue TEXT NOT NULL DEFAULT 'polymarket';

ALTER TABLE market_evaluations
    ADD COLUMN IF NOT EXISTS venue TEXT NOT NULL DEFAULT 'polymarket';

ALTER TABLE predictions
    ADD COLUMN IF NOT EXISTS venue TEXT NOT NULL DEFAULT 'polymarket';

-- ── 4. Indexes ──────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_user_config_venue
    ON user_config(venue);

CREATE INDEX IF NOT EXISTS idx_pm_positions_venue
    ON pm_positions(venue);

CREATE INDEX IF NOT EXISTS idx_market_evaluations_venue
    ON market_evaluations(venue);

CREATE INDEX IF NOT EXISTS idx_predictions_venue
    ON predictions(venue);

-- Partial unique index for open positions: scope by venue too so the same
-- market_id shape couldn't collide across venues. Drop then recreate inside
-- the transaction so there's no window where duplicates can slip through.
DROP INDEX IF EXISTS uq_pm_positions_open_market;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pm_positions_open_market
    ON pm_positions(market_id, mode, venue) WHERE status = 'open';

-- ── 5. min_p_win default bump (0.50 -> 0.55) ────────────────────────────────
-- Only touch rows still sitting on the old default value. Users who
-- deliberately picked something else (including 0.51-0.54 or 0.60+) keep
-- their setting untouched.
UPDATE user_config
   SET min_p_win = 0.55, updated_at = NOW()
 WHERE min_p_win = 0.50;

ALTER TABLE user_config ALTER COLUMN min_p_win SET DEFAULT 0.55;

-- ── 6. Tell PostgREST to pick up the new schema ─────────────────────────────
NOTIFY pgrst, 'reload schema';

COMMIT;
