import { NextResponse } from "next/server";
import crypto from "node:crypto";

// Lemon Squeezy webhook stub.
//
// LS sends POSTs here on every order event (created, refunded,
// updated). We verify the HMAC-SHA256 signature in the
// `X-Signature` header against the signing secret stored as
// LEMONSQUEEZY_SIGNING_SECRET in Vercel, log the event, and
// return 200. Anything more (DB write, email forward, license
// generation) is intentionally NOT here yet — LS's own
// configuration handles license-key issuance and the
// post-purchase email, and we don't have a use for our own copy
// of the order record yet.
//
// Wire-up steps when you set this up in Vercel:
//   1. Lemon Squeezy dashboard -> Settings -> Webhooks ->
//      Add endpoint:
//        URL: https://delfibot.com/api/webhooks/lemonsqueezy
//        Events: order_created, order_refunded (at minimum)
//        Signing secret: copy whatever LS gives you
//   2. Vercel project -> Settings -> Environment Variables ->
//      add LEMONSQUEEZY_SIGNING_SECRET = <that secret>
//   3. Re-deploy. From this point every order in LS produces a
//      log line in the Vercel Function logs that you can grep.
//
// To extend: when you want the webhook to do more, branch on
// `event.meta.event_name` and add the relevant handler. Keep
// this stub fast (return 200 within ~5s) so LS doesn't retry.

export const runtime = "nodejs";

interface LemonSqueezeEvent {
  meta?: {
    event_name?: string;
    custom_data?: Record<string, unknown>;
  };
  data?: {
    id?: string;
    type?: string;
    attributes?: Record<string, unknown>;
  };
}

function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  try {
    return crypto.timingSafeEqual(Buffer.from(a, "hex"), Buffer.from(b, "hex"));
  } catch {
    return false;
  }
}

export async function POST(request: Request): Promise<NextResponse> {
  const signingSecret = process.env.LEMONSQUEEZY_SIGNING_SECRET;
  if (!signingSecret) {
    // Fail closed: if the secret isn't configured we can't verify
    // anything, so we refuse rather than silently accept. Logged
    // once per request so Vercel logs show the misconfig.
    console.error(
      "[ls-webhook] LEMONSQUEEZY_SIGNING_SECRET is not set; rejecting",
    );
    return NextResponse.json(
      { error: "webhook not configured" },
      { status: 503 },
    );
  }

  const sigHeader = request.headers.get("x-signature") ?? "";
  const rawBody = await request.text();

  const expected = crypto
    .createHmac("sha256", signingSecret)
    .update(rawBody)
    .digest("hex");

  if (!timingSafeEqualHex(sigHeader.toLowerCase(), expected)) {
    console.warn("[ls-webhook] signature mismatch; rejecting");
    return NextResponse.json({ error: "invalid signature" }, { status: 401 });
  }

  let event: LemonSqueezeEvent;
  try {
    event = JSON.parse(rawBody) as LemonSqueezeEvent;
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }

  const eventName = event.meta?.event_name ?? "unknown";
  const data = event.data ?? {};
  const attrs = (data.attributes ?? {}) as Record<string, unknown>;

  // Conservative log: enough to diagnose problems without dumping
  // every PII field. Email is included because reconciling against
  // LS's dashboard always wants it. Card numbers / addresses stay
  // out.
  console.log(`[ls-webhook] event=${eventName}`, {
    id: data.id,
    type: data.type,
    identifier: attrs.identifier,
    order_number: attrs.order_number,
    user_email: attrs.user_email,
    total: attrs.total,
    status: attrs.status,
    refunded: attrs.refunded,
  });

  // 200 immediately. Any future async work (DB write, license
  // re-issue) should run in the background here, NOT block the
  // response — LS retries aggressively if we take too long.
  return NextResponse.json({ received: true, event: eventName });
}
