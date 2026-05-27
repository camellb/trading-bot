// apps/web/lib/email/license-issued.ts
//
// Builds the post-purchase email we send buyers as soon as the Stripe
// `checkout.session.completed` webhook lands. The email contains:
//
//   * Their signed license blob (the only thing that unlocks the app).
//   * Two platform download links pointing at the delfibot.com proxy
//     (/api/download/mac, /api/download/win). The proxy hides the
//     GitHub release backend from the buyer entirely - user rule
//     (2026-05-27): "NO ONE SHOULD EVER SEE THE GITHUB. NO USER. EVER."
//     DOWNLOAD_URL_MAC / DOWNLOAD_URL_WIN env vars can still override
//     the default (e.g. when migrating to a dedicated CDN later).
//   * A two-step "paste this in" instruction.
//
// Both an HTML and a text body are produced. Resend prefers HTML; the
// text body is the spam-filter fallback and what gets shown if the
// recipient's client renders text-only.
//
// No "edge", no "shadow", no model-vendor names in any user-visible
// copy (per project doctrine). The buyer email is the only person
// reading this; tone is "thank you + here's what to do".

import { resend, RESEND_FROM, SUPPORT_INBOX } from "@/lib/resend";

/** Server-side env var (no NEXT_PUBLIC_*). Override per-environment in
 *  Vercel when migrating off the default proxy; otherwise the user
 *  hits the delfibot.com proxy at /api/download/{mac,win}, which
 *  streams the latest installer without ever exposing the GitHub
 *  Release URL to the buyer's browser. */
function downloadUrl(platform: "mac" | "win"): string {
  if (platform === "mac") {
    return (
      process.env.DOWNLOAD_URL_MAC ||
      "https://delfibot.com/api/download/mac"
    );
  }
  return (
    process.env.DOWNLOAD_URL_WIN ||
    "https://delfibot.com/api/download/win"
  );
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

interface LicenseEmailArgs {
  /** Where the email is going. */
  to: string;
  /** The full signed license blob, exactly as the desktop app expects. */
  blob: string;
  /** Convenience for the email subject and the body greeting. */
  email: string;
}

export function renderLicenseEmail({ blob, email }: LicenseEmailArgs): {
  subject: string;
  html: string;
  text: string;
} {
  const macUrl = downloadUrl("mac");
  const winUrl = downloadUrl("win");
  const safeBlob = escapeHtml(blob);
  const safeEmail = escapeHtml(email);

  const subject = "Welcome to Delfi";

  const text = [
    `DELFI`,
    ``,
    `You're in.`,
    ``,
    `Hi ${email},`,
    ``,
    `Thanks for choosing Delfi. You just bought yourself a forecaster`,
    `that runs while you sleep. Your license key is below, you'll`,
    `need it the first time you open the app.`,
    ``,
    `--- YOUR DELFI LICENSE (keep this safe) ---`,
    blob,
    `--- end license ---`,
    ``,
    `Download Delfi:`,
    `  macOS:    ${macUrl}`,
    `  Windows:  ${winUrl}`,
    ``,
    `Getting started:`,
    `  1. Open Delfi and paste your license key.`,
    `  2. Delfi starts in Simulation. Watch it trade for as long as`,
    `     you want.`,
    `  3. When the numbers convince you, switch to Live and connect`,
    `     your Polymarket account. Your private keys never leave`,
    `     your computer.`,
    ``,
    `On macOS you may see a warning that Apple cannot verify Delfi.`,
    `To avoid it, open Terminal once and paste:`,
    ``,
    `    xattr -cr /Applications/Delfi.app`,
    ``,
    `Then launch Delfi normally.`,
    ``,
    `  - Delfi runs entirely on your machine.`,
    `  - Your funds, wallet, and Polymarket account never leave your`,
    `    control.`,
    `  - Delfi learns from every settled trade and proposes`,
    `    adjustments, but it never changes its own rules without`,
    `    your approval.`,
    ``,
    `You picked the right side of the trade.`,
    ``,
    `Any questions, just reply to this email. It comes straight to us.`,
    ``,
    `- The Delfi team`,
    `${SUPPORT_INBOX}`,
    `https://delfibot.com`,
  ].join("\n");

  // Inline styles only; most clients strip <style> tags. Wordmark is a
  // typographic recreation of public/brand/wordmark.svg so it renders
  // even when image assets are blocked. Tables for outer layout
  // (Outlook compatibility); divs inside the card.
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark only">
<meta name="supported-color-schemes" content="dark only">
<title>${escapeHtml(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#0a0a0c;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,Cantarell,sans-serif;color:#e9e6dd;-webkit-font-smoothing:antialiased;">
  <span style="display:none!important;visibility:hidden;opacity:0;color:transparent;height:0;width:0;font-size:1px;line-height:1px;mso-hide:all;">You're in. Your Delfi license is inside. Watch it trade in Simulation, then go live when the numbers convince you.</span>

  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0a0a0c;">
    <tr>
      <td align="center" style="padding:40px 16px 56px 16px;">

        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%;">

          <tr>
            <td align="center" style="padding:0 0 28px 0;">
              <div style="font-family:'Newsreader',Georgia,'Times New Roman',serif;font-size:30px;font-weight:500;letter-spacing:0.5em;color:#daaa4c;text-indent:0.5em;">DELFI</div>
              <div style="height:1px;width:64px;background:#daaa4c;opacity:0.55;margin:14px auto 0 auto;font-size:0;line-height:0;">&nbsp;</div>
            </td>
          </tr>

          <tr>
            <td style="background:#0e0f14;border:1px solid #1f2026;border-radius:10px;padding:44px 36px 36px 36px;">

              <h1 style="font-family:'Newsreader',Georgia,'Times New Roman',serif;font-size:32px;font-weight:500;letter-spacing:-0.01em;color:#daaa4c;margin:0 0 22px 0;line-height:1.1;">
                You're in.
              </h1>

              <p style="font-size:15px;line-height:1.65;color:#e9e6dd;margin:0 0 14px 0;">
                Hi ${safeEmail},
              </p>
              <p style="font-size:15px;line-height:1.7;color:#cfcabd;margin:0 0 28px 0;">
                Thanks for choosing Delfi. You just bought yourself a forecaster that runs while you sleep. Your license key is below, you'll need it the first time you open the app.
              </p>

              <div style="background:#13141a;border:1px solid #2a2c34;border-left:2px solid #daaa4c;border-radius:6px;padding:22px 22px 20px 22px;margin:0 0 32px 0;">
                <div style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10px;letter-spacing:0.22em;text-transform:uppercase;color:#8c8675;margin:0 0 12px 0;">
                  Your Delfi license <span style="color:#5c574d;text-transform:none;letter-spacing:0;font-size:11px;">(keep this safe)</span>
                </div>
                <pre style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.6;color:#daaa4c;white-space:pre-wrap;word-break:break-all;margin:0;">${safeBlob}</pre>
              </div>

              <div style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10px;letter-spacing:0.22em;text-transform:uppercase;color:#8c8675;margin:0 0 12px 0;">
                Download
              </div>
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 36px 0;">
                <tr>
                  <td style="padding-right:10px;">
                    <a href="${escapeHtml(macUrl)}" style="display:inline-block;padding:13px 26px;background:#daaa4c;border:1px solid #daaa4c;border-radius:4px;color:#0a0a0c;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;">
                      macOS
                    </a>
                  </td>
                  <td>
                    <a href="${escapeHtml(winUrl)}" style="display:inline-block;padding:13px 26px;background:transparent;border:1px solid #daaa4c;border-radius:4px;color:#daaa4c;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;">
                      Windows
                    </a>
                  </td>
                </tr>
              </table>

              <div style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10px;letter-spacing:0.22em;text-transform:uppercase;color:#8c8675;margin:0 0 14px 0;">
                Getting started
              </div>
              <ol style="font-size:14px;line-height:1.7;color:#cfcabd;margin:0 0 24px 0;padding-left:20px;">
                <li style="margin:0 0 10px 0;">Open Delfi and paste your license key.</li>
                <li style="margin:0 0 10px 0;">Delfi starts in Simulation. Watch it trade for as long as you want.</li>
                <li style="margin:0;">When the numbers convince you, switch to Live and connect your Polymarket account. Your private keys never leave your computer.</li>
              </ol>

              <p style="font-size:13px;line-height:1.65;color:#8c8675;margin:0 0 8px 0;">
                On macOS you may see a warning that Apple cannot verify Delfi. To avoid it, open Terminal once and paste:
              </p>
              <pre style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.4;color:#daaa4c;background:#0a0a0c;border:1px solid #1f2026;border-radius:4px;padding:10px 12px;margin:0 0 8px 0;white-space:pre-wrap;word-break:break-all;">xattr -cr /Applications/Delfi.app</pre>
              <p style="font-size:13px;line-height:1.65;color:#8c8675;margin:0 0 28px 0;">
                Then launch Delfi normally.
              </p>

              <div style="height:1px;background:#1f2026;margin:0 0 24px 0;font-size:0;line-height:0;">&nbsp;</div>

              <ul style="font-size:13px;line-height:1.75;color:#8c8675;margin:0 0 28px 0;padding-left:18px;">
                <li style="margin:0 0 8px 0;">Delfi runs entirely on your machine.</li>
                <li style="margin:0 0 8px 0;">Your funds, wallet, and Polymarket account never leave your control.</li>
                <li style="margin:0;">Delfi learns from every settled trade and proposes adjustments, but it never changes its own rules without your approval.</li>
              </ul>

              <div style="height:1px;background:#1f2026;margin:0 0 24px 0;font-size:0;line-height:0;">&nbsp;</div>

              <p style="font-family:'Newsreader',Georgia,'Times New Roman',serif;font-size:17px;font-style:italic;line-height:1.5;color:#daaa4c;margin:0 0 22px 0;">
                You picked the right side of the trade.
              </p>

              <p style="font-size:13px;line-height:1.7;color:#8c8675;margin:0 0 22px 0;">
                Any questions, just reply to this email. It comes straight to us.
              </p>

              <p style="font-size:13px;line-height:1.7;color:#cfcabd;margin:0;">
                - The Delfi team
              </p>

            </td>
          </tr>

          <tr>
            <td align="center" style="padding:24px 12px 0 12px;">
              <p style="font-size:11px;line-height:1.7;color:#5c574d;margin:0;letter-spacing:0.04em;">
                Delfi &middot;
                <a href="mailto:${escapeHtml(SUPPORT_INBOX)}" style="color:#5c574d;text-decoration:none;">${escapeHtml(SUPPORT_INBOX)}</a> &middot;
                <a href="https://delfibot.com" style="color:#5c574d;text-decoration:none;">delfibot.com</a>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>`;

  return { subject, html, text };
}

/**
 * Send the license email. Returns the Resend message id on success or
 * throws on failure -- the webhook catches the throw, stores the row
 * with `email_sent_at = null`, and lets us retry from an admin tool.
 */
export async function sendLicenseEmail(args: LicenseEmailArgs): Promise<string> {
  const { subject, html, text } = renderLicenseEmail(args);
  const { data, error } = await resend().emails.send({
    from: RESEND_FROM,
    to: args.to,
    replyTo: SUPPORT_INBOX,
    subject,
    html,
    text,
  });
  if (error) {
    throw new Error(
      `[license-email] resend failed: ${error.name}: ${error.message}`,
    );
  }
  return data?.id ?? "";
}
