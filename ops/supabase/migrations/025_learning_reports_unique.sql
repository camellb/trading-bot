-- Migration 025 - defence-in-depth UNIQUE constraint on learning_reports.
--
-- Background
-- ----------
-- The learning cadence in `apps/bot/engine/learning_cadence.py` fires a
-- review report every LEARNING_CYCLE_TRADE_INTERVAL settled trades. The
-- gate uses a bookmark of "settled_count at the last cycle". Before the
-- bookmark fix in this same change set, that bookmark only advanced when
-- the cycle produced at least one config proposal (it read MAX from
-- `pending_suggestions`). When a cycle produced zero proposals (e.g. the
-- user's config was already well-tuned), the bookmark stayed put and the
-- next single settlement re-fired the cycle, so the user received two
-- review reports back to back.
--
-- The application-side fix reads the bookmark from `learning_reports`
-- instead, which is always written. This migration adds a database-level
-- guard so even a future regression of the bookmark logic cannot insert
-- two report rows with the same (user_id, mode, settled_count) tuple. If
-- the application tries to write a duplicate, the INSERT errors out and
-- the duplicate Telegram send never happens.
--
-- Idempotent: wrapped in DO blocks that no-op on re-run.

BEGIN;

-- ── Drop any pre-existing duplicates so the unique index can build ──────────
-- Keep the earliest row per (user_id, mode, settled_count). Later rows
-- with identical bookmarks were almost certainly the duplicate-fire bug
-- this migration is closing the door on.
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id, mode, settled_count
               ORDER BY created_at ASC, id ASC
           ) AS rn
    FROM learning_reports
)
DELETE FROM learning_reports
 WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- ── Add the unique constraint ───────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'learning_reports_user_mode_count_unique'
    ) THEN
        ALTER TABLE learning_reports
            ADD CONSTRAINT learning_reports_user_mode_count_unique
            UNIQUE (user_id, mode, settled_count);
    END IF;
END$$;

COMMIT;

-- PostgREST schema cache reload so the web app sees the new constraint
-- without waiting for the watchdog.
NOTIFY pgrst, 'reload schema';
