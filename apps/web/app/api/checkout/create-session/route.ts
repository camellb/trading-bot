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
// Optional UTM forwarding: the client may pass a `utm` object
// in the POST body; we stash it in `metadata` on the session
// so the webhook log can attribute the conversion.

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

interface CreateSessionBody {
  /** Optional UTM tags from the click that opened the page. We
   *  stash them as metadata on the session so the webhook log
   *  can tie a conversion back to a CTA location. */
  utm?: {
    source?: string;
    medium?: string;
    content?: string;
  };
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
      metadata: {
        // Stash UTM tags so they land in the webhook log alongside
        // the license issue. Empty strings are filtered by Stripe
        // automatically; missing keys are simply omitted.
        utm_source:  body.utm?.source  ?? "",
        utm_medium:  body.utm?.medium  ?? "",
        utm_content: body.utm?.content ?? "",
      },
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
