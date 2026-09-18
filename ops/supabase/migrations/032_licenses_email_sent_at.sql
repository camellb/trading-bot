-- 032: stamp when the license email was delivered.
--
-- The Stripe webhook commits the licenses row and then sends the email.
-- When the send failed, the webhook answered 500 and Stripe redelivered,
-- but the redelivery hit ON CONFLICT (stripe_session_id) DO NOTHING and
-- skipped the email, so a buyer who had closed the return page never
-- received the key. The webhook now sends on redelivery while
-- email_sent_at IS NULL (apps/web/app/api/webhooks/stripe/route.ts).
--
-- Existing rows are backfilled so an old event that Stripe redelivers
-- does not re-send an email that went out long ago.

ALTER TABLE licenses
  ADD COLUMN IF NOT EXISTS email_sent_at TIMESTAMPTZ;

UPDATE licenses
   SET email_sent_at = issued_at
 WHERE email_sent_at IS NULL;
