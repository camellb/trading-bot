-- Migration 026 - licenses table for the local-first one-time-fee model.
--
-- Each row is one paid license, generated server-side after a successful
-- Stripe checkout, signed with the Ed25519 key in LICENSE_SIGNING_KEY,
-- and emailed to the buyer. The desktop app verifies the signed blob
-- offline; this table only exists so we can:
--   * de-duplicate Stripe webhook deliveries (idempotency on session id)
--   * resend a license to a buyer who lost the email
--   * revoke a license if a charge gets refunded (revoked_at)
--   * count live installs for the launch dashboard
--
-- There is no FK to auth.users -- the local-first pivot dropped Supabase
-- Auth for product accounts. A license is keyed by buyer email + Stripe
-- session. The desktop app never reads this table; it only exists so we
-- can administer the issued set.

CREATE TABLE IF NOT EXISTS licenses (
  id                    UUID PRIMARY KEY,
  email                 TEXT NOT NULL,
  sku                   TEXT NOT NULL,
  -- The full signed blob exactly as emailed to the buyer.
  -- We retain it so we can resend on request.
  blob                  TEXT NOT NULL,
  -- Stripe references for idempotency + refund handling.
  stripe_session_id     TEXT UNIQUE,
  stripe_customer_id    TEXT,
  stripe_payment_intent TEXT,
  amount_cents          INTEGER,
  currency              TEXT,
  -- Audit timestamps.
  issued_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at            TIMESTAMPTZ,
  revoke_reason         TEXT
);

CREATE INDEX IF NOT EXISTS licenses_email_idx      ON licenses (email);
CREATE INDEX IF NOT EXISTS licenses_issued_at_idx  ON licenses (issued_at DESC);
CREATE INDEX IF NOT EXISTS licenses_revoked_at_idx ON licenses (revoked_at)
  WHERE revoked_at IS NOT NULL;

-- Default-deny: no policies = no access from anon/authenticated. Service
-- role bypasses RLS so the webhook still writes.
ALTER TABLE licenses ENABLE ROW LEVEL SECURITY;
