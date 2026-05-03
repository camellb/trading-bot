// apps/web/app/api/webhooks/stripe/route.ts
//
// Stripe webhook for the local-first one-time-fee model.
//
// Flow on every successful checkout:
//
//   1. Stripe POSTs `checkout.session.completed` here with an HMAC
//      signature in `Stripe-Signature`.
//   2. We verify the signature against STRIPE_WEBHOOK_SECRET. If it
//      doesn't match, return 400 -- Stripe will retry, and we never
//      issue a license without a valid signature.
//   3. Sign a fresh license blob with the Ed25519 key in
//      LICENSE_SIGNING_KEY (see lib/license.ts).
//   4. Insert a row into `licenses` (migration 026). The
//      stripe_session_id UNIQUE constraint makes the insert
//      idempotent: if Stripe redelivers the same session, the second
//      insert raises 23505, we read the existing row, and we still
//      return 200 so Stripe stops retrying. The buyer is NOT
//      re-emailed in that case (the original delivery already went
//      out, and we don't want duplicates if Stripe retries days
//      later).
//   5. Send the license email via Resend (see lib/email/license-issued.ts).
//   6. Return 200.
//
// We also handle `charge.refunded`: stamp `revoked_at` on the row so
// admin tooling can later refuse to resend that license. The desktop
// app does not currently consult a revocation list (offline first);
// V2 may add an opt-in periodic revocation check.
//
// Wire-up steps (do these once when setting up Stripe in Vercel):
//
//   1. Stripe dashboard -> Developers -> Webhooks -> Add endpoint:
//        URL: https://delfibot.com/api/webhooks/stripe
//        Events: checkout.session.completed, charge.refunded
//        Signing secret: copy whatever Stripe gives you
//   2. Vercel project -> Environment Variables -> add:
//        STRIPE_SECRET_KEY        = sk_live_... (or sk_test_...)
//        STRIPE_WEBHOOK_SECRET    = whsec_...
//        LICENSE_SIGNING_KEY      = (PEM from generate-license-keypair.mjs)
//        DATABASE_URL             = (Supabase pooled connection string)
//        RESEND_API_KEY           = (Resend API key)
//      Optional:
//        DOWNLOAD_URL_MAC         = https://...
//        DOWNLOAD_URL_WIN         = https://...
//   3. Re-deploy. Stripe will start delivering events.
//
// The webhook is fast on purpose -- Stripe expects a 2xx within 30s
// or it retries with exponential backoff. The DB insert and the
// email send are both inline because they're both <2s; if either ever
// gets slow we move them to a queue.

import { NextResponse } from "next/server";
import Stripe from "stripe";
import { Pool } from "pg";
import {
  buildPayload,
  signLicense,
  loadSigningKey,
  DELFI_SKU_PERSONAL_V1,
} from "@/lib/license";
import { sendLicenseEmail } from "@/lib/email/license-issued";

export const runtime = "nodejs"; // crypto + pg need Node, not edge

// ---- shared singletons -------------------------------------------------

let stripeClient: Stripe | null = null;
function stripe(): Stripe {
  if (stripeClient) return stripeClient;
  const key = process.env.STRIPE_SECRET_KEY;
  if (!key) {
    throw new Error("STRIPE_SECRET_KEY is not set");
  }
  stripeClient = new Stripe(key);
  return stripeClient;
}

let pgPool: Pool | null = null;
function db(): Pool {
  if (pgPool) return pgPool;
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error("DATABASE_URL is not set");
  }
  pgPool = new Pool({
    connectionString: url,
    // Vercel functions are short-lived; keep the pool small.
    max: 2,
    idleTimeoutMillis: 5_000,
  });
  return pgPool;
}

// ---- handlers ----------------------------------------------------------

interface InsertedLicense {
  id: string;
  email: string;
  blob: string;
  /** True if this is a fresh insert; false if we hit the unique
   *  constraint on stripe_session_id and read back the existing row. */
  fresh: boolean;
}

async function insertLicense(args: {
  email: string;
  session: Stripe.Checkout.Session;
}): Promise<InsertedLicense> {
  const payload = buildPayload({ email: args.email });
  const blob = signLicense(payload, loadSigningKey());

  const sessionId = args.session.id;
  const customerId =
    typeof args.session.customer === "string"
      ? args.session.customer
      : args.session.customer?.id ?? null;
  const paymentIntent =
    typeof args.session.payment_intent === "string"
      ? args.session.payment_intent
      : args.session.payment_intent?.id ?? null;
  const amount = args.session.amount_total ?? null;
  const currency = args.session.currency ?? null;

  const insert = await db().query(
    `INSERT INTO licenses
       (id, email, sku, blob,
        stripe_session_id, stripe_customer_id, stripe_payment_intent,
        amount_cents, currency)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
     ON CONFLICT (stripe_session_id) DO NOTHING
     RETURNING id, email, blob`,
    [
      payload.id,
      payload.email,
      payload.sku,
      blob,
      sessionId,
      customerId,
      paymentIntent,
      amount,
      currency,
    ],
  );

  if (insert.rowCount && insert.rowCount > 0) {
    return { ...insert.rows[0], fresh: true };
  }

  // Conflict: Stripe redelivered an event we've already processed.
  // Return the original row so we can log it but don't re-email.
  const existing = await db().query(
    `SELECT id, email, blob FROM licenses WHERE stripe_session_id = $1 LIMIT 1`,
    [sessionId],
  );
  if (!existing.rowCount) {
    // Theoretically unreachable -- conflict means the row exists.
    throw new Error(`license row vanished for session=${sessionId}`);
  }
  return { ...existing.rows[0], fresh: false };
}

async function markRefunded(paymentIntentId: string): Promise<number> {
  const r = await db().query(
    `UPDATE licenses
        SET revoked_at = COALESCE(revoked_at, now()),
            revoke_reason = COALESCE(revoke_reason, 'stripe charge.refunded')
      WHERE stripe_payment_intent = $1
        AND revoked_at IS NULL`,
    [paymentIntentId],
  );
  return r.rowCount ?? 0;
}

/**
 * Stamp a license as revoked because the buyer disputed the charge
 * with their bank. Treated as more serious than a refund: the money
 * may not be coming back yet, and chargebacks usually correlate with
 * abuse or shared accounts. Operator should follow up manually in
 * the Stripe dashboard to submit evidence within the deadline.
 */
async function markDisputed(paymentIntentId: string): Promise<number> {
  const r = await db().query(
    `UPDATE licenses
        SET revoked_at = COALESCE(revoked_at, now()),
            revoke_reason = COALESCE(revoke_reason, 'stripe charge.dispute.created')
      WHERE stripe_payment_intent = $1
        AND revoked_at IS NULL`,
    [paymentIntentId],
  );
  return r.rowCount ?? 0;
}

/**
 * Async payment methods (SEPA, BACS, some bank debits) clear AFTER
 * the buyer sees the success page. If the payment subsequently fails
 * the buyer may already have a license they didn't actually pay for.
 * Revoke by session id since at this stage the payment_intent may
 * not yet have a captured charge.
 */
async function markAsyncPaymentFailed(sessionId: string): Promise<number> {
  const r = await db().query(
    `UPDATE licenses
        SET revoked_at = COALESCE(revoked_at, now()),
            revoke_reason = COALESCE(revoke_reason, 'stripe checkout.session.async_payment_failed')
      WHERE stripe_session_id = $1
        AND revoked_at IS NULL`,
    [sessionId],
  );
  return r.rowCount ?? 0;
}

// ---- entrypoint --------------------------------------------------------

export async function POST(request: Request): Promise<NextResponse> {
  const sig = request.headers.get("stripe-signature");
  const whSecret = process.env.STRIPE_WEBHOOK_SECRET;
  if (!sig || !whSecret) {
    console.error("[stripe-webhook] missing signature or webhook secret");
    return NextResponse.json(
      { error: "webhook not configured" },
      { status: 503 },
    );
  }

  const rawBody = await request.text();

  let event: Stripe.Event;
  try {
    event = stripe().webhooks.constructEvent(rawBody, sig, whSecret);
  } catch (e) {
    console.warn("[stripe-webhook] signature verification failed", {
      err: String(e),
    });
    return NextResponse.json({ error: "invalid signature" }, { status: 400 });
  }

  try {
    switch (event.type) {
      case "checkout.session.completed": {
        const session = event.data.object as Stripe.Checkout.Session;
        const email =
          session.customer_details?.email ||
          session.customer_email ||
          (typeof session.customer === "object" && session.customer
            ? (session.customer as Stripe.Customer).email
            : null);

        if (!email) {
          // Stripe lets you create sessions without collecting email;
          // we configure ours to require it. Log loudly and fail closed:
          // a paid session without an email means we can't deliver the
          // license, and that's an operator problem (fix the Checkout
          // config), not something to silently swallow.
          console.error("[stripe-webhook] session has no email", {
            sessionId: session.id,
          });
          return NextResponse.json(
            { error: "no email on session; configure Checkout to require it" },
            { status: 400 },
          );
        }

        const license = await insertLicense({ email, session });

        if (license.fresh) {
          await sendLicenseEmail({
            to: email,
            blob: license.blob,
            email,
          });
          console.log("[stripe-webhook] license issued", {
            licenseId: license.id,
            sessionId: session.id,
            email,
            sku: DELFI_SKU_PERSONAL_V1,
          });
        } else {
          console.log("[stripe-webhook] duplicate session; skip email", {
            licenseId: license.id,
            sessionId: session.id,
          });
        }
        break;
      }

      case "charge.refunded": {
        const charge = event.data.object as Stripe.Charge;
        const piId =
          typeof charge.payment_intent === "string"
            ? charge.payment_intent
            : charge.payment_intent?.id;
        if (!piId) {
          console.warn("[stripe-webhook] refund without payment_intent", {
            chargeId: charge.id,
          });
          break;
        }
        const n = await markRefunded(piId);
        console.log("[stripe-webhook] license revoked on refund", {
          paymentIntentId: piId,
          rowsAffected: n,
        });
        break;
      }

      case "charge.dispute.created": {
        // Buyer disputed the charge with their bank. Revoke the
        // license now; operator should follow up in Stripe to
        // submit evidence before the response deadline.
        const dispute = event.data.object as Stripe.Dispute;
        const piId =
          typeof dispute.payment_intent === "string"
            ? dispute.payment_intent
            : dispute.payment_intent?.id;
        if (!piId) {
          console.warn("[stripe-webhook] dispute without payment_intent", {
            disputeId: dispute.id,
          });
          break;
        }
        const n = await markDisputed(piId);
        console.error("[stripe-webhook] license revoked on dispute", {
          disputeId: dispute.id,
          paymentIntentId: piId,
          reason: dispute.reason,
          amount: dispute.amount,
          dueBy: dispute.evidence_details?.due_by,
          rowsAffected: n,
        });
        break;
      }

      case "checkout.session.async_payment_failed": {
        // Async-clearing payment method failed after we already
        // issued a license. Revoke and log loudly; the buyer's
        // copy still validates offline so they could trade for a
        // little while, but the next desktop release with a
        // revocation check would lock them out. For now this is
        // primarily an operator alert.
        const session = event.data.object as Stripe.Checkout.Session;
        const n = await markAsyncPaymentFailed(session.id);
        console.error("[stripe-webhook] license revoked on async payment failure", {
          sessionId: session.id,
          email:     session.customer_details?.email,
          rowsAffected: n,
        });
        break;
      }

      case "checkout.session.expired": {
        // Buyer started checkout but never finished. No money
        // moved, no license to revoke; we just want the funnel
        // signal in the logs for now.
        const session = event.data.object as Stripe.Checkout.Session;
        console.log("[stripe-webhook] session expired", {
          sessionId: session.id,
        });
        break;
      }

      default:
        // Quietly accept any other event Stripe asks us to listen to.
        // We only return the names we actively handle so misconfigured
        // webhook subscriptions still 200 (Stripe stops retrying) but
        // we know about them in the logs.
        console.log("[stripe-webhook] unhandled event", { type: event.type });
        break;
    }
  } catch (e) {
    console.error("[stripe-webhook] handler threw", {
      type: event.type,
      err: e instanceof Error ? e.message : String(e),
    });
    // Return 500 so Stripe retries -- this typically means a transient
    // DB or Resend hiccup.
    return NextResponse.json({ error: "handler error" }, { status: 500 });
  }

  return NextResponse.json({ ok: true });
}
