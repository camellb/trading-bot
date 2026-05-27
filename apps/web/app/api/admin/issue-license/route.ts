// apps/web/app/api/admin/issue-license/route.ts
//
// Operator-only: reissue a license for a buyer whose Stripe webhook
// failed to land. Does exactly what the webhook would have done on a
// successful checkout.session.completed delivery:
//
//   1. Look up the Stripe checkout session (by id, or by buyer email
//      via Stripe's session list).
//   2. Refuse if the session is not paid + complete.
//   3. Sign a real license payload with LICENSE_SIGNING_KEY (same key
//      the webhook uses; same blob the desktop app verifies offline).
//   4. INSERT into the licenses table, tied to stripe_session_id /
//      stripe_customer_id / stripe_payment_intent so refund handling
//      keeps working.
//   5. Send the real "Welcome to Delfi" email via Resend. NOT the
//      [TEST]-prefixed subject from /api/admin/test-email -- this
//      goes to actual paying buyers.
//
// Idempotent: if a row already exists for the session_id (Stripe's
// retry won the race, or you ran this twice), we don't re-email.
//
// Auth: Bearer ADMIN_TOKEN, same as the other /api/admin/* routes.
//
// Usage:
//   curl -X POST https://delfibot.com/api/admin/issue-license \
//     -H "Authorization: Bearer $ADMIN_TOKEN" \
//     -H "Content-Type: application/json" \
//     -d '{"email":"buyer@example.com"}'
//
//   # or by session id directly
//   curl ... -d '{"sessionId":"cs_live_xxxxx"}'

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

export const runtime = "nodejs";

// ---- shared singletons -------------------------------------------------

let stripeClient: Stripe | null = null;
function stripe(): Stripe {
  if (stripeClient) return stripeClient;
  const key = process.env.STRIPE_SECRET_KEY;
  if (!key) throw new Error("STRIPE_SECRET_KEY is not set");
  stripeClient = new Stripe(key);
  return stripeClient;
}

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

// ---- auth --------------------------------------------------------------

function unauthorized() {
  return NextResponse.json(
    { error: "unauthorized" },
    { status: 401, headers: { "WWW-Authenticate": "Bearer" } },
  );
}

interface Body {
  /** Stripe session id (cs_live_... / cs_test_...). Preferred. */
  sessionId?: string;
  /** Buyer email. We list recent sessions and pick the most recent
   *  paid one matching this email. Use when you don't have the id
   *  handy (e.g. the buyer just emailed you saying nothing arrived). */
  email?: string;
  /** If true and a license row already exists for this session, re-send
   *  the email using the existing blob instead of returning the
   *  alreadyIssued no-op. Useful when (a) the buyer never received
   *  the original email or (b) the email template was updated and we
   *  want to push the new copy to an existing buyer. Idempotent on
   *  the DB - we never insert a duplicate row. */
  resend?: boolean;
}

// ---- session lookup ----------------------------------------------------

async function findSession(args: {
  sessionId?: string;
  email?: string;
}): Promise<Stripe.Checkout.Session | null> {
  if (args.sessionId) {
    try {
      return await stripe().checkout.sessions.retrieve(args.sessionId);
    } catch (e) {
      console.error("[issue-license] stripe retrieve failed", {
        sessionId: args.sessionId,
        err: e instanceof Error ? e.message : String(e),
      });
      return null;
    }
  }
  if (args.email) {
    const target = args.email.trim().toLowerCase();
    // Walk a couple of pages back. 100 is the API max per page; at
    // current sales volume this covers many days. If we ever need
    // deeper history, switch to `created[gte]=...` filtering.
    let starting_after: string | undefined = undefined;
    for (let page = 0; page < 3; page++) {
      const list: Stripe.ApiList<Stripe.Checkout.Session> =
        await stripe().checkout.sessions.list({
          limit: 100,
          ...(starting_after ? { starting_after } : {}),
        });
      const hit = list.data.find((s) => {
        const email = (
          s.customer_details?.email ||
          s.customer_email ||
          ""
        ).toLowerCase();
        return (
          email === target &&
          s.status === "complete" &&
          s.payment_status === "paid"
        );
      });
      if (hit) return hit;
      if (!list.has_more) break;
      starting_after = list.data[list.data.length - 1]?.id;
      if (!starting_after) break;
    }
    return null;
  }
  return null;
}

// ---- main --------------------------------------------------------------

export async function POST(req: Request): Promise<NextResponse> {
  // ── Auth ─────────────────────────────────────────────────────────
  const adminToken = process.env.ADMIN_TOKEN;
  if (!adminToken) {
    console.error("[issue-license] ADMIN_TOKEN is not set; refusing");
    return NextResponse.json(
      { error: "issue-license route not configured (ADMIN_TOKEN unset)" },
      { status: 503 },
    );
  }
  const auth = req.headers.get("authorization") ?? "";
  const m = auth.match(/^Bearer\s+(.+)$/i);
  if (!m || m[1] !== adminToken) return unauthorized();

  // ── Body ─────────────────────────────────────────────────────────
  let body: Body = {};
  try {
    body = (await req.json()) as Body;
  } catch {
    return NextResponse.json(
      { error: "invalid JSON body" },
      { status: 400 },
    );
  }
  if (!body.sessionId && !body.email) {
    return NextResponse.json(
      { error: "body must be {sessionId} or {email}" },
      { status: 400 },
    );
  }

  // ── Find session ─────────────────────────────────────────────────
  let session: Stripe.Checkout.Session | null;
  try {
    session = await findSession(body);
  } catch (e) {
    console.error("[issue-license] stripe lookup threw", {
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      { error: "stripe lookup failed" },
      { status: 502 },
    );
  }
  if (!session) {
    return NextResponse.json(
      { error: "no matching paid Stripe session found" },
      { status: 404 },
    );
  }
  if (session.status !== "complete" || session.payment_status !== "paid") {
    return NextResponse.json(
      {
        error:
          `session is ${session.status} / ${session.payment_status}; refusing to issue`,
        sessionId: session.id,
      },
      { status: 409 },
    );
  }

  const email =
    session.customer_details?.email ||
    session.customer_email ||
    null;
  if (!email) {
    return NextResponse.json(
      { error: "session has no email on it", sessionId: session.id },
      { status: 400 },
    );
  }

  // ── Idempotency: already issued? ─────────────────────────────────
  // When `resend: true` is set we still hit the DB but use the
  // existing blob to push a fresh email instead of bailing out.
  try {
    const existing = await db().query<{ id: string; email: string; blob: string }>(
      `SELECT id, email, blob FROM licenses WHERE stripe_session_id = $1 LIMIT 1`,
      [session.id],
    );
    if (existing.rowCount && existing.rowCount > 0) {
      if (!body.resend) {
        console.log("[issue-license] already issued", {
          licenseId: existing.rows[0].id,
          sessionId: session.id,
        });
        return NextResponse.json({
          ok: true,
          alreadyIssued: true,
          licenseId: existing.rows[0].id,
          email: existing.rows[0].email,
          sessionId: session.id,
        });
      }
      // Resend path: re-send the original email with the existing blob.
      const existingRow = existing.rows[0];
      try {
        const messageId = await sendLicenseEmail({
          to:    existingRow.email,
          blob:  existingRow.blob,
          email: existingRow.email,
        });
        console.log("[issue-license] resent existing license", {
          licenseId: existingRow.id,
          sessionId: session.id,
          email:     existingRow.email,
          messageId,
        });
        return NextResponse.json({
          ok: true,
          alreadyIssued: true,
          resent: true,
          licenseId: existingRow.id,
          email:     existingRow.email,
          sessionId: session.id,
          messageId,
        });
      } catch (e) {
        console.error("[issue-license] resend email failed", {
          licenseId: existingRow.id,
          err: e instanceof Error ? e.message : String(e),
        });
        return NextResponse.json(
          {
            ok: false,
            alreadyIssued: true,
            resent: false,
            licenseId: existingRow.id,
            error: "resend failed; try again",
          },
          { status: 502 },
        );
      }
    }
  } catch (e) {
    console.error("[issue-license] db existence check failed", {
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      { error: "db check failed" },
      { status: 500 },
    );
  }

  // ── Sign + insert + email ────────────────────────────────────────
  let blob: string;
  let licenseId: string;
  try {
    const payload = buildPayload({ email });
    licenseId = payload.id;
    blob = signLicense(payload, loadSigningKey());
  } catch (e) {
    console.error("[issue-license] sign failed", {
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      {
        error:
          "could not sign license. LICENSE_SIGNING_KEY likely missing or malformed.",
      },
      { status: 500 },
    );
  }

  const customerId =
    typeof session.customer === "string"
      ? session.customer
      : session.customer?.id ?? null;
  const paymentIntent =
    typeof session.payment_intent === "string"
      ? session.payment_intent
      : session.payment_intent?.id ?? null;

  try {
    await db().query(
      `INSERT INTO licenses
         (id, email, sku, blob,
          stripe_session_id, stripe_customer_id, stripe_payment_intent,
          amount_cents, currency)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
      [
        licenseId,
        email.trim().toLowerCase(),
        DELFI_SKU_PERSONAL_V1,
        blob,
        session.id,
        customerId,
        paymentIntent,
        session.amount_total ?? null,
        session.currency ?? null,
      ],
    );
  } catch (e) {
    // The most likely cause here is the unique constraint on
    // stripe_session_id (Stripe's auto-retry won the race). Re-check
    // and return the existing row.
    console.warn("[issue-license] insert failed; checking for race", {
      err: e instanceof Error ? e.message : String(e),
    });
    try {
      const r = await db().query<{ id: string; email: string }>(
        `SELECT id, email FROM licenses WHERE stripe_session_id = $1 LIMIT 1`,
        [session.id],
      );
      if (r.rowCount && r.rowCount > 0) {
        return NextResponse.json({
          ok: true,
          alreadyIssued: true,
          raced: true,
          licenseId: r.rows[0].id,
          email: r.rows[0].email,
          sessionId: session.id,
        });
      }
    } catch {
      // fall through to error
    }
    return NextResponse.json(
      { error: "db insert failed" },
      { status: 500 },
    );
  }

  let messageId = "";
  try {
    messageId = await sendLicenseEmail({
      to: email,
      blob,
      email,
    });
  } catch (e) {
    // Row is already in the DB. Surface the email failure so the
    // operator can retry, but don't leave the caller in the dark.
    console.error("[issue-license] email send failed", {
      licenseId,
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      {
        ok:        false,
        rowInserted: true,
        emailSent: false,
        licenseId,
        sessionId: session.id,
        error:     "license row inserted but email send failed; rerun to retry",
      },
      { status: 502 },
    );
  }

  console.log("[issue-license] reissued", {
    licenseId,
    sessionId: session.id,
    email,
    messageId,
  });
  return NextResponse.json({
    ok:        true,
    licenseId,
    sessionId: session.id,
    email,
    messageId,
    amountCents: session.amount_total,
    currency:    session.currency,
  });
}
