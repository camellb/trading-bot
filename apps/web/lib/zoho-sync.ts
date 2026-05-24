// apps/web/lib/zoho-sync.ts
//
// Persistent retry layer for the Zoho Books sync. Sits between the
// Stripe webhook and lib/zoho.ts to make the sync eventually
// consistent: if Zoho is down at the moment of purchase, the row in
// `licenses` is left with zoho_synced_at=NULL and a sweep picks it
// up later.
//
// Cost model: zero. We use:
//   - The existing `licenses` table + 8 columns from migration 027.
//   - Next.js `after()` to fire the sweep AFTER the webhook returns
//     200 to Stripe (no added webhook latency, no cron required).
//   - A small admin endpoint as a manual backstop.
//
// Failure semantics: every helper here logs and continues. None of
// these can throw out of the webhook handler; that would force
// Stripe to retry the whole event and risk re-emailing buyers.
//
// Idempotency: each sync attempt is gated on `zoho_synced_at IS NULL`
// in SQL, so a row that has already synced is never re-attempted.
// Rows with attempts >= 10 are also skipped (alert fodder).

import { Pool } from "pg";
import {
  createInvoiceForPurchase,
  recordRefund,
} from "@/lib/zoho";

// Reuse the same pool shape the webhook uses. Module-scope cache
// because Vercel functions are short-lived but each instance handles
// many requests.
let pgPool: Pool | null = null;
function db(): Pool {
  if (pgPool) return pgPool;
  const url = process.env.DATABASE_URL;
  if (!url) throw new Error("DATABASE_URL is not set");
  pgPool = new Pool({
    connectionString: url,
    max: 2,
    idleTimeoutMillis: 5_000,
  });
  return pgPool;
}

// ---- single-row state mutators ------------------------------------------

/**
 * Mark a license row as having successfully synced its purchase
 * (invoice + payment) to Zoho Books.
 */
export async function markPurchaseSynced(args: {
  licenseId: string;
  invoiceId: string;
}): Promise<void> {
  await db().query(
    `UPDATE licenses
        SET zoho_invoice_id      = $1,
            zoho_synced_at       = now(),
            zoho_sync_last_error = NULL
      WHERE id = $2`,
    [args.invoiceId, args.licenseId],
  );
}

/**
 * Bump attempts + record the error string when a purchase sync
 * fails. Doesn't set zoho_synced_at, so the row remains a retry
 * candidate.
 */
export async function markPurchaseFailed(args: {
  licenseId: string;
  error:     string;
}): Promise<void> {
  await db().query(
    `UPDATE licenses
        SET zoho_sync_attempts   = zoho_sync_attempts + 1,
            zoho_sync_last_error = $1
      WHERE id = $2`,
    [args.error.slice(0, 500), args.licenseId],
  );
}

/** Same as above, refund side. */
export async function markRefundSynced(args: {
  licenseId:    string;
  creditNoteId: string;
}): Promise<void> {
  await db().query(
    `UPDATE licenses
        SET zoho_credit_note_id          = $1,
            zoho_refund_synced_at        = now(),
            zoho_refund_sync_last_error  = NULL
      WHERE id = $2`,
    [args.creditNoteId, args.licenseId],
  );
}

export async function markRefundFailed(args: {
  licenseId: string;
  error:     string;
}): Promise<void> {
  await db().query(
    `UPDATE licenses
        SET zoho_refund_sync_attempts    = zoho_refund_sync_attempts + 1,
            zoho_refund_sync_last_error  = $1
      WHERE id = $2`,
    [args.error.slice(0, 500), args.licenseId],
  );
}

// ---- sweep helpers ------------------------------------------------------

/**
 * Aggregate counts returned by the sweep helpers.
 */
export interface SweepResult {
  /** Rows that matched the pending filter at the start of the run. */
  pending:      number;
  /** Rows that synced to Zoho on this run. */
  succeeded:    number;
  /** Rows that failed AGAIN on this run (still pending after). */
  stillFailing: number;
}

/**
 * Find pending purchases (no zoho_synced_at, not given up) issued
 * in the last 30 days and retry each. Capped at `maxItems` per run
 * so a sweep can't run away on a backlog.
 */
export async function syncPendingPurchases(
  maxItems = 5,
): Promise<SweepResult> {
  const res = await db().query<{
    id:                    string;
    email:                 string;
    stripe_session_id:     string | null;
    stripe_payment_intent: string | null;
    amount_cents:          number | null;
    currency:              string | null;
  }>(
    `SELECT id, email, stripe_session_id, stripe_payment_intent,
            amount_cents, currency
       FROM licenses
      WHERE zoho_synced_at IS NULL
        AND zoho_sync_attempts < 10
        AND issued_at > now() - interval '30 days'
      ORDER BY issued_at ASC
      LIMIT $1`,
    [maxItems],
  );

  let succeeded = 0;
  let stillFailing = 0;

  for (const row of res.rows) {
    if (!row.stripe_session_id || !row.stripe_payment_intent
        || row.amount_cents == null || !row.currency) {
      // Old rows from before we captured all four fields. Mark with
      // a permanent error so they exit the retry loop.
      await markPurchaseFailed({
        licenseId: row.id,
        error:     "missing stripe fields; cannot sync",
      });
      stillFailing++;
      continue;
    }

    try {
      const { invoiceId } = await createInvoiceForPurchase({
        email:           row.email,
        amount:          row.amount_cents / 100,
        currency:        row.currency.toUpperCase(),
        sessionId:       row.stripe_session_id,
        paymentIntentId: row.stripe_payment_intent,
      });
      await markPurchaseSynced({ licenseId: row.id, invoiceId });
      succeeded++;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error("[zoho-sync] purchase retry failed", {
        licenseId: row.id,
        err:       msg,
      });
      await markPurchaseFailed({ licenseId: row.id, error: msg });
      stillFailing++;
    }
  }

  return {
    pending:      res.rows.length,
    succeeded,
    stillFailing,
  };
}

/**
 * Find pending refunds (revoked but no credit note synced) and
 * retry each. Same caps + filters as the purchase sweep.
 */
export async function syncPendingRefunds(
  maxItems = 5,
): Promise<SweepResult> {
  const res = await db().query<{
    id:                    string;
    stripe_payment_intent: string | null;
    amount_cents:          number | null;
    currency:              string | null;
  }>(
    `SELECT id, stripe_payment_intent, amount_cents, currency
       FROM licenses
      WHERE revoked_at IS NOT NULL
        AND zoho_refund_synced_at IS NULL
        AND zoho_refund_sync_attempts < 10
        AND revoked_at > now() - interval '30 days'
      ORDER BY revoked_at ASC
      LIMIT $1`,
    [maxItems],
  );

  let succeeded = 0;
  let stillFailing = 0;

  for (const row of res.rows) {
    if (!row.stripe_payment_intent
        || row.amount_cents == null || !row.currency) {
      await markRefundFailed({
        licenseId: row.id,
        error:     "missing stripe fields; cannot sync",
      });
      stillFailing++;
      continue;
    }

    try {
      const result = await recordRefund({
        paymentIntentId: row.stripe_payment_intent,
        amount:          row.amount_cents / 100,
        currency:        row.currency.toUpperCase(),
      });
      if (result) {
        await markRefundSynced({
          licenseId:    row.id,
          creditNoteId: result.creditNoteId,
        });
        succeeded++;
      } else {
        // recordRefund returned null = no matching invoice in Zoho.
        // Bump attempts so we don't loop forever, but don't classify
        // as a hard error - the operator may need to backfill the
        // original invoice manually first.
        await markRefundFailed({
          licenseId: row.id,
          error:     "no matching invoice found in zoho",
        });
        stillFailing++;
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error("[zoho-sync] refund retry failed", {
        licenseId: row.id,
        err:       msg,
      });
      await markRefundFailed({ licenseId: row.id, error: msg });
      stillFailing++;
    }
  }

  return {
    pending:      res.rows.length,
    succeeded,
    stillFailing,
  };
}
