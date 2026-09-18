# License database: what production needs

The website (apps/web) reads and writes one Postgres database through
`DATABASE_URL`. Everything a buyer touches depends on it: the Stripe
webhook inserts the license row before it emails the key, the checkout
return page reads that row, and the desktop app claims its device slot
and polls for revocation against it.

Tables, in order:

1. `026_licenses.sql` - the `licenses` table.
2. `027_zoho_sync_state.sql` - accounting sync columns on `licenses`.
3. `031_license_activations.sql` - one device slot per license.
4. `032_licenses_email_sent_at.sql` - email delivery stamp (retry on Stripe redelivery).

026 and 027 were deleted from the repo on 2026-05-19 with the rest of the
SaaS-era schema and restored on 2026-09-18, the day the production
database was found unreachable (the hosted project no longer resolved in
DNS, consistent with a free-tier project paused for inactivity).

## If the database is down

1. Restore the existing project from the Supabase dashboard. That keeps
   the `licenses` rows of every customer.
2. Only if it cannot be restored: create a new Postgres, apply the four
   files above in order, then re-insert every issued license before
   pointing `DATABASE_URL` at it. Desktop builds before v1.5.86 treat
   "license id not found" as a revoke and wipe the customer's key.
3. Health probe (expect HTTP 200 with `"valid": false` and
   `"revoke_reason": "license id not found"`):

       curl -s "https://delfibot.com/api/license/check?id=00000000-0000-4000-8000-000000000000"

4. In the Stripe dashboard, resend any failed `checkout.session.completed`
   deliveries from the outage window.

A daily Vercel cron (apps/web/vercel.json -> /api/cron/keepalive) reads the
`licenses` table so the database always has activity and cannot be paused
for being idle. It answers 503 when the database is unreachable.
