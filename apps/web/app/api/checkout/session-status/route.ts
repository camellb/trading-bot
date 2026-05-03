// apps/web/app/api/checkout/session-status/route.ts
//
// Read-only companion to /api/checkout/create-session. The
// /checkout/return page hits this with the session_id Stripe
// substituted in the return URL, gets back a small status
// payload, and uses it to decide whether to show "thanks, your
// license is on the way" or "payment didn't go through, try
// again."
//
// We can't expose STRIPE_SECRET_KEY to the browser, so the
// return page can't call Stripe directly. This route is the
// thinnest possible proxy: it pulls only the three fields the
// return page needs and never touches the customer's card
// details (Stripe wouldn't return them anyway).
//
// Idempotent. Safe to retry. The webhook is what actually
// issues the license; this route only reflects state.

import { NextResponse } from "next/server";
import Stripe from "stripe";

export const runtime = "nodejs";

let stripeClient: Stripe | null = null;
function stripe(): Stripe {
  if (stripeClient) return stripeClient;
  const key = process.env.STRIPE_SECRET_KEY;
  if (!key) throw new Error("STRIPE_SECRET_KEY is not set");
  stripeClient = new Stripe(key);
  return stripeClient;
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

  try {
    const session = await stripe().checkout.sessions.retrieve(sessionId);
    return NextResponse.json({
      status:        session.status,         // "complete" | "open" | "expired"
      paymentStatus: session.payment_status, // "paid" | "unpaid" | "no_payment_required"
      email:         session.customer_details?.email ?? null,
    });
  } catch (e) {
    console.error("[session-status] stripe error", {
      sessionId,
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      { error: "could not load session" },
      { status: 500 },
    );
  }
}
