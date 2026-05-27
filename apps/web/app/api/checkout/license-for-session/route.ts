// apps/web/app/api/checkout/license-for-session/route.ts
//
// Returns the signed license blob for a paid checkout session so the
// /checkout/return page can display it directly to the buyer (in
// addition to emailing it). Lets a buyer paste the key into the
// desktop app immediately after purchase instead of context-switching
// to their inbox.
//
// Access control: the only thing protecting this endpoint is
// knowledge of the Stripe session id. That's the same threat
// surface as the post-purchase email itself - both rely on the
// secrecy of the session id, which Stripe only ever returned to the
// buyer's own browser via the embedded checkout return URL. We
// still verify the session is `complete + paid` against Stripe
// before exposing anything, so a guessed-but-unpaid session id
// returns 404.
//
// Idempotent: same blob comes back on every call.

import { NextResponse } from "next/server";
import Stripe from "stripe";
import { Pool } from "pg";

export const runtime = "nodejs";

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

export async function GET(req: Request): Promise<NextResponse> {
  const url = new URL(req.url);
  const sessionId = url.searchParams.get("session_id");
  if (!sessionId) {
    return NextResponse.json(
      { error: "session_id is required" },
      { status: 400 },
    );
  }

  // 1. Verify the session is paid + complete before exposing the
  //    blob. Without this gate, anyone could brute-force session ids
  //    and harvest licenses (highly unlikely given the id entropy,
  //    but a one-line check costs us nothing).
  let session: Stripe.Checkout.Session;
  try {
    session = await stripe().checkout.sessions.retrieve(sessionId);
  } catch (e) {
    console.warn("[license-for-session] stripe retrieve failed", {
      sessionId,
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      { error: "session not found" },
      { status: 404 },
    );
  }
  if (session.status !== "complete" || session.payment_status !== "paid") {
    return NextResponse.json(
      {
        error: "session is not paid yet",
        status: session.status,
        paymentStatus: session.payment_status,
      },
      { status: 404 },
    );
  }

  // 2. Look up the license row written by the webhook (or by
  //    /api/admin/issue-license backfill). If the webhook is still
  //    in flight on a fresh purchase, the row may not exist yet;
  //    return 202 so the client can retry.
  try {
    const r = await db().query<{ id: string; email: string; blob: string }>(
      `SELECT id, email, blob FROM licenses WHERE stripe_session_id = $1 LIMIT 1`,
      [sessionId],
    );
    if (!r.rowCount) {
      return NextResponse.json(
        { error: "license not issued yet; retry shortly" },
        { status: 202 },
      );
    }
    return NextResponse.json({
      licenseId: r.rows[0].id,
      email:     r.rows[0].email,
      blob:      r.rows[0].blob,
    });
  } catch (e) {
    console.error("[license-for-session] db query failed", {
      sessionId,
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      { error: "could not load license" },
      { status: 500 },
    );
  }
}
