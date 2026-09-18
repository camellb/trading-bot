-- Migration 027 - track Zoho Books sync state on the licenses table.
--
-- The Stripe webhook tries to create a Zoho invoice + customer payment
-- inline on every checkout.session.completed. If Zoho is down at the
-- moment of purchase, the row is left with zoho_synced_at = NULL.
--
-- A periodic retry sweep (lib/zoho-sync.ts, called via Next.js after()
-- on every webhook fire AND from the /api/admin/zoho-sync-pending
-- admin endpoint) finds those rows and re-attempts the call. On success
-- it stamps zoho_invoice_id + zoho_synced_at; on persistent failure
-- it bumps zoho_sync_attempts so we eventually stop retrying.
--
-- Same pattern, separate columns, for the refund side (Credit Notes).
--
-- All ALTER COLUMN clauses use IF NOT EXISTS so re-running is safe.

ALTER TABLE licenses
  ADD COLUMN IF NOT EXISTS zoho_invoice_id              TEXT,
  ADD COLUMN IF NOT EXISTS zoho_synced_at               TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS zoho_sync_attempts           INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS zoho_sync_last_error         TEXT,
  ADD COLUMN IF NOT EXISTS zoho_credit_note_id          TEXT,
  ADD COLUMN IF NOT EXISTS zoho_refund_synced_at        TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS zoho_refund_sync_attempts    INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS zoho_refund_sync_last_error  TEXT;

-- Partial index for the retry sweep: only non-synced rows that
-- haven't given up yet. We don't bound by date in the index because
-- the retry helper applies its own 30-day window in SQL; keeping the
-- index date-agnostic means it stays useful even after schema changes.
CREATE INDEX IF NOT EXISTS licenses_zoho_pending_idx
  ON licenses (issued_at DESC)
  WHERE zoho_synced_at IS NULL
    AND zoho_sync_attempts < 10;

-- Same pattern for refunds: rows that have a refund (revoked_at set)
-- but the credit note hasn't synced yet.
CREATE INDEX IF NOT EXISTS licenses_zoho_refund_pending_idx
  ON licenses (revoked_at DESC)
  WHERE revoked_at IS NOT NULL
    AND zoho_refund_synced_at IS NULL
    AND zoho_refund_sync_attempts < 10;
