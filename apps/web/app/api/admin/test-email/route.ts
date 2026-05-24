// apps/web/app/api/admin/test-email/route.ts
//
// Operator-only smoke-test endpoint. Builds a synthetic license,
// signs it with the real LICENSE_SIGNING_KEY, and sends it to a
// chosen email address via the same Resend path the Stripe
// webhook uses. Lets the operator verify the post-purchase email
// path works without doing a real $249 charge first.
//
// Usage (replace TOKEN and EMAIL):
//
//   curl -X POST https://delfibot.com/api/admin/test-email \
//     -H "Authorization: Bearer $ADMIN_TOKEN" \
//     -H "Content-Type: application/json" \
//     -d '{"to":"you@example.com"}'
//
// Auth: requires the request `Authorization: Bearer <token>` header
// to match the ADMIN_TOKEN env var. ADMIN_TOKEN must be a long
// random string (the operator generates it; we never know it).
// Without ADMIN_TOKEN set, the route returns 503 -- fail closed.
//
// Returns { ok: true, messageId, to } on a successful Resend send,
// { error } with status >= 400 otherwise. The email contains a
// real Ed25519-signed license blob -- though the email is clearly
// labelled as a test in the subject so the recipient knows.

import { NextResponse } from "next/server";
import {
  buildPayload,
  signLicense,
  loadSigningKey,
} from "@/lib/license";
import { renderLicenseEmail } from "@/lib/email/license-issued";
import { resend, RESEND_FROM, SUPPORT_INBOX } from "@/lib/resend";

export const runtime = "nodejs";

interface Body {
  to?: string;
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
    console.error("[test-email] ADMIN_TOKEN is not set; refusing");
    return NextResponse.json(
      { error: "test-email route not configured (ADMIN_TOKEN unset)" },
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

  const to = (body.to ?? "").trim();
  if (!to || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(to)) {
    return NextResponse.json(
      { error: "body must be {\"to\": \"<email>\"}" },
      { status: 400 },
    );
  }

  // ── Sign + send ──────────────────────────────────────────────────
  let blob: string;
  try {
    const payload = buildPayload({ email: to });
    blob = signLicense(payload, loadSigningKey());
  } catch (e) {
    console.error("[test-email] license sign failed", {
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      {
        error:
          "could not sign a license. Likely cause: LICENSE_SIGNING_KEY is unset or malformed in Vercel env.",
      },
      { status: 500 },
    );
  }

  const { html, text } = renderLicenseEmail({ to, blob, email: to });

  try {
    const { data, error } = await resend().emails.send({
      from: RESEND_FROM,
      to,
      replyTo: SUPPORT_INBOX,
      // Subject deliberately marked as a test so the recipient can
      // ignore it. The blob inside is real and would activate the
      // desktop app if pasted in.
      subject: "[TEST] Your Delfi license",
      html,
      text,
    });
    if (error) {
      console.error("[test-email] resend send failed", {
        to,
        name: error.name,
        message: error.message,
      });
      return NextResponse.json(
        { error: `resend: ${error.name}: ${error.message}` },
        { status: 502 },
      );
    }
    console.log("[test-email] sent", { to, messageId: data?.id });
    return NextResponse.json({
      ok: true,
      to,
      messageId: data?.id ?? "",
    });
  } catch (e) {
    console.error("[test-email] resend threw", {
      to,
      err: e instanceof Error ? e.message : String(e),
    });
    return NextResponse.json(
      { error: "resend send threw" },
      { status: 502 },
    );
  }
}
