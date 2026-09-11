// apps/web/app/api/checkout/create-session/route.ts
//
// Server-side endpoint that mints a Stripe Checkout Session in
// "embedded" UI mode and returns its client_secret. The browser
// then mounts <EmbeddedCheckout /> with that secret and Stripe
// renders the card form inside our /checkout page.
//
// Why embedded (not Payment Links): the buyer never leaves
// delfibot.com, the page keeps our typography and dark theme,
// and Stripe still owns the card field iframe so we stay in
// PCI-DSS SAQ A scope (same as Payment Links). Conversion lift
// over a redirect-out flow is typically 5-15%.
//
// Same `checkout.session.completed` event fires whether the
// buyer used a Payment Link or this embedded session, so the
// webhook handler at app/api/webhooks/stripe/route.ts works
// unchanged for both paths.
//
// Env vars (set in Vercel):
//   STRIPE_SECRET_KEY  - sk_live_... (or sk_test_... in Preview)
//   STRIPE_PRICE_ID    - price_... from Stripe Products
//
// Acquisition forwarding: the client passes campaign, click, browser,
// and consent data. We keep the approved fields in Stripe metadata so
// a paid order can be attributed and deduplicated with Meta.

import { NextResponse } from "next/server";
import Stripe from "stripe";
import type { AttributionData } from "@/lib/attribution";
import { consentRequiredForCountry } from "@/lib/regions";

export const runtime = "nodejs";

let stripeClient: Stripe | null = null;
function stripe(): Stripe {
  if (stripeClient) return stripeClient;
  const key = process.env.STRIPE_SECRET_KEY;
  if (!key) throw new Error("STRIPE_SECRET_KEY is not set");
  stripeClient = new Stripe(key);
  return stripeClient;
}

interface CreateSessionBody {
  attribution?: AttributionData;
  trackingConsent?: "accepted" | "rejected" | null;
}

const ATTRIBUTION_FIELDS = [
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_content",
  "utm_term",
  "utm_id",
  "fbclid",
  "gclid",
  "msclkid",
  "landing_path",
  "referrer_origin",
  "delfi_cta",
  "fbc",
  "fbp",
] as const;

function metadataValue(value: unknown): string {
  return typeof value === "string" ? value.trim().slice(0, 500) : "";
}

function clientIp(req: Request): string {
  return (
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    req.headers.get("x-real-ip")?.trim() ||
    ""
  ).slice(0, 100);
}

function originFromRequest(req: Request): string {
  // Vercel sets x-forwarded-host + x-forwarded-proto; locally we
  // fall back to req.headers.host. Both end up as a clean
  // <protocol>://<host> string we can hand to Stripe as a return
  // URL prefix.
  const proto =
    req.headers.get("x-forwarded-proto") ||
    (process.env.NODE_ENV === "production" ? "https" : "http");
  const host =
    req.headers.get("x-forwarded-host") ||
    req.headers.get("host") ||
    "delfibot.com";
  return `${proto}://${host}`;
}

export async function POST(req: Request): Promise<NextResponse> {
  const priceId = process.env.STRIPE_PRICE_ID;
  if (!priceId) {
    console.error("[create-session] STRIPE_PRICE_ID is not set");
    return NextResponse.json(
      { error: "checkout not configured" },
      { status: 503 },
    );
  }

  let body: CreateSessionBody = {};
  try {
    if (req.headers.get("content-length") !== "0") {
      body = (await req.json()) as CreateSessionBody;
    }
  } catch {
    // Empty / malformed body is fine; we just won't get UTM tags.
    body = {};
  }

  const origin = originFromRequest(req);
  const country = req.headers.get("x-vercel-ip-country");
  const requiresConsent = consentRequiredForCountry(country);
  const trackingAllowed = body.trackingConsent === "accepted" || (
    !requiresConsent && body.trackingConsent !== "rejected"
  );
  const attribution = body.attribution ?? {};
  const metadata: Record<string, string> = {
    meta_tracking_allowed: trackingAllowed ? "true" : "false",
  };

  for (const field of ATTRIBUTION_FIELDS) {
    const isMetaIdentifier = field === "fbclid" || field === "fbc" || field === "fbp";
    metadata[field] = isMetaIdentifier && !trackingAllowed
      ? ""
      : metadataValue(attribution[field]);
  }
  if (trackingAllowed) {
    metadata.meta_client_ip = clientIp(req);
    metadata.meta_client_user_agent = metadataValue(req.headers.get("user-agent"));
  }

  try {
    const session = await stripe().checkout.sessions.create({
      // Stripe v22 SDK renamed the ui_mode value `"embedded"` to
      // `"embedded_page"`; the API accepts both but the type is
      // strict. The resulting session is still consumed by
      // <EmbeddedCheckoutProvider> from @stripe/react-stripe-js
      // exactly the same way.
      ui_mode: "embedded_page",
      mode: "payment",
      line_items: [{ price: priceId, quantity: 1 }],
      // The buyer types their email into the embedded form; Stripe
      // populates `customer_details.email` on the resulting session
      // and the webhook reads it from there. We require it because
      // the post-purchase license email has nowhere else to go.
      customer_creation: "if_required",
      // After a successful payment Stripe redirects the iframe to
      // this URL with `{CHECKOUT_SESSION_ID}` substituted. The
      // /checkout/return page reads the id, queries the session
      // status, and shows the confirmation copy.
      return_url: `${origin}/checkout/return?session_id={CHECKOUT_SESSION_ID}`,
      metadata,
      // Surfaces an "Add promotion code" affordance on Stripe's
      // embedded checkout. Off by default in the API; without this
      // the field never renders even when valid Promotion Codes
      // exist in the dashboard. Codes themselves still need to be
      // created in Stripe Dashboard -> Products -> Coupons ->
      // Promotion Codes for any entry to validate.
      allow_promotion_codes: true,
      // Stripe Tax: turn it on per environment in the dashboard;
      // the SDK respects the dashboard toggle automatically and we
      // don't need to pass tax-related fields here.
    });

    if (!session.client_secret) {
      console.error("[create-session] session has no client_secret", {
        sessionId: session.id,
      });
      return NextResponse.json(
        { error: "session created without a client secret" },
        { status: 502 },
      );
    }

    return NextResponse.json({
      clientSecret: session.client_secret,
      sessionId:    session.id,
    });
  } catch (e) {
    console.error("[create-session] stripe error", {
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      { error: "could not create checkout session" },
      { status: 500 },
    );
  }
}
