// apps/web/app/api/admin/test-zoho/route.ts
//
// Operator-only smoke-test endpoint for the Zoho Books integration.
// Hits the same code path the Stripe webhook will use on a real
// purchase, but with a synthetic email + Stripe-style ids so the
// resulting Zoho invoice is clearly identifiable as a test.
//
// Verifies in one request:
//   1. Token refresh works (env vars are correct + DC is right)
//   2. Contact lookup-or-create works
//   3. Invoice creation + draft → sent transition works
//   4. Customer payment recording works
//
// On success the buyer-facing equivalent of a real $X invoice
// will appear in your Zoho Books at /app/<org>/invoices, marked
// PAID. Delete it manually after testing.
//
// Usage:
//
//   curl -X POST https://delfibot.com/api/admin/test-zoho \
//     -H "Authorization: Bearer $ADMIN_TOKEN" \
//     -H "Content-Type: application/json" \
//     -d '{"to":"you+zoho-test@example.com","amount":1}'
//
// `amount` defaults to 1 (single unit of your Zoho base currency)
// so the test invoice is tiny and easy to delete.
//
// Auth: identical scheme to the test-email endpoint - requires
// ADMIN_TOKEN env var set in Vercel and a matching Bearer header.

import { NextResponse } from "next/server";
import {
  getAccessToken,
  findOrCreateContact,
  createInvoiceForPurchase,
} from "@/lib/zoho";

export const runtime = "nodejs";

interface Body {
  to?:       string;
  amount?:   number;
  currency?: string;
}

function unauthorized() {
  return NextResponse.json(
    { error: "unauthorized" },
    { status: 401, headers: { "WWW-Authenticate": "Bearer" } },
  );
}

export async function POST(req: Request): Promise<NextResponse> {
  // ── Auth ─────────────────────────────────────────────────────────
  const adminToken = process.env.ADMIN_TOKEN;
  if (!adminToken) {
    console.error("[test-zoho] ADMIN_TOKEN is not set; refusing");
    return NextResponse.json(
      { error: "test-zoho route not configured (ADMIN_TOKEN unset)" },
      { status: 503 },
    );
  }
  const auth = req.headers.get("authorization") ?? "";
  const m = auth.match(/^Bearer\s+(.+)$/i);
  if (!m || m[1] !== adminToken) {
    return unauthorized();
  }

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

  const to = (body.to ?? "").trim().toLowerCase();
  if (!to || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(to)) {
    return NextResponse.json(
      { error: 'body must be {"to":"<email>"} (optional: amount, currency)' },
      { status: 400 },
    );
  }
  const amount = typeof body.amount === "number" && body.amount > 0
    ? body.amount
    : 1;
  const currency = (body.currency ?? "USD").toUpperCase();

  // Synthetic Stripe-style ids so the resulting invoice is clearly
  // marked as a test in the Zoho books (and the refund handler can
  // find it later if you want to test recordRefund as well).
  const ts = Date.now();
  const sessionId       = `cs_test_synthetic_${ts}`;
  const paymentIntentId = `pi_test_synthetic_${ts}`;

  // ── Step 1: token refresh ───────────────────────────────────────
  let tokenOk = false;
  try {
    await getAccessToken();
    tokenOk = true;
  } catch (e) {
    return NextResponse.json(
      {
        ok:    false,
        step:  "token_refresh",
        error: e instanceof Error ? e.message : String(e),
        hint:
          "Most likely cause: one of ZOHO_DC, ZOHO_CLIENT_ID, " +
          "ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN is wrong in Vercel " +
          "env. Confirm the values match the Self Client you " +
          "generated the refresh token from.",
      },
      { status: 502 },
    );
  }

  // ── Step 2: contact lookup-or-create ────────────────────────────
  let contactId: string;
  try {
    contactId = await findOrCreateContact(to);
  } catch (e) {
    return NextResponse.json(
      {
        ok:      false,
        step:    "find_or_create_contact",
        tokenOk,
        error:   e instanceof Error ? e.message : String(e),
        hint:
          "Token worked but the contacts API rejected. Most likely " +
          "ZOHO_ORG_ID is wrong - verify against Settings → " +
          "Organization Profile in Zoho Books.",
      },
      { status: 502 },
    );
  }

  // ── Step 3: invoice + payment ──────────────────────────────────
  try {
    const { invoiceId } = await createInvoiceForPurchase({
      email:     to,
      amount,
      currency,
      sessionId,
      paymentIntentId,
    });
    console.log("[test-zoho] success", { to, invoiceId, contactId });
    return NextResponse.json({
      ok:        true,
      tokenOk:   true,
      contactId,
      invoiceId,
      sessionId,
      paymentIntentId,
      amount,
      currency,
      hint:
        "Open Zoho Books → Invoices and look for the line that " +
        "matches the invoiceId above. It should be marked PAID. " +
        "Delete it manually after verifying.",
    });
  } catch (e) {
    return NextResponse.json(
      {
        ok:        false,
        step:      "create_invoice_or_payment",
        tokenOk,
        contactId,
        error:     e instanceof Error ? e.message : String(e),
        hint:
          "Token + contact creation worked, but invoice/payment " +
          "creation failed. Common causes: (a) the currency you " +
          "passed isn't enabled in Zoho's multi-currency settings; " +
          "(b) Zoho payment_mode 'creditcard' isn't valid in your " +
          "org (rare).",
      },
      { status: 502 },
    );
  }
}
