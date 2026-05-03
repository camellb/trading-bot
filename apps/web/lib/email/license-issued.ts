// apps/web/lib/email/license-issued.ts
//
// Builds the post-purchase email we send buyers as soon as the Stripe
// `checkout.session.completed` webhook lands. The email contains:
//
//   * Their signed license blob (the only thing that unlocks the app).
//   * Two platform download links (read from DOWNLOAD_URL_MAC /
//     DOWNLOAD_URL_WIN env vars, with a GitHub Releases fallback so
//     the function still works before the env vars are set).
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
 *  Vercel; otherwise the user lands on the GitHub Releases page and
 *  picks the right binary. */
function downloadUrl(platform: "mac" | "win"): string {
  if (platform === "mac") {
    return (
      process.env.DOWNLOAD_URL_MAC ||
      "https://github.com/camellb/trading-bot/releases/latest"
    );
  }
  return (
    process.env.DOWNLOAD_URL_WIN ||
    "https://github.com/camellb/trading-bot/releases/latest"
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

  const subject = "Your Delfi license";

  const text = [
    `Welcome to Delfi.`,
    ``,
    `Your license key is below. Keep it somewhere safe; this is the`,
    `only thing you need to unlock the app, on as many of your own`,
    `machines as you like.`,
    ``,
    `--- Delfi license (begin) ---`,
    blob,
    `--- Delfi license (end) ---`,
    ``,
    `Download Delfi:`,
    `  macOS:    ${macUrl}`,
    `  Windows:  ${winUrl}`,
    ``,
    `Install Delfi, open it, and on first launch paste the license`,
    `key from above into the field labelled "License key". You only`,
    `need to do this once per machine.`,
    ``,
    `Lost this email? Reply and we'll resend.`,
    ``,
    `Questions about how Delfi works? Read the docs at`,
    `https://delfibot.com or just reply to this email.`,
    ``,
    `Delfi never sees your funds, your wallet, or your trades. It`,
    `runs entirely on your computer.`,
    ``,
    `Delfi`,
    `${SUPPORT_INBOX}`,
    `https://delfibot.com`,
  ].join("\n");

  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${escapeHtml(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#0a0a0c;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,Cantarell,sans-serif;color:#e9e6dd;">
  <div style="max-width:600px;margin:0 auto;padding:48px 24px;">
    <h1 style="font-size:28px;font-weight:400;letter-spacing:-0.01em;color:#daaa4c;margin:0 0 24px 0;">
      Welcome to Delfi.
    </h1>

    <p style="font-size:15px;line-height:1.6;color:#e9e6dd;margin:0 0 16px 0;">
      Hello ${safeEmail},
    </p>
    <p style="font-size:15px;line-height:1.6;color:#cfcabd;margin:0 0 24px 0;">
      Your license key is below. Keep it somewhere safe; this is the
      only thing you need to unlock the app, on as many of your own
      machines as you like.
    </p>

    <div style="background:#13141a;border:1px solid #2a2c34;border-radius:6px;padding:20px;margin:0 0 32px 0;">
      <div style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;letter-spacing:0.16em;text-transform:uppercase;color:#8c8675;margin:0 0 10px 0;">
        Your Delfi license
      </div>
      <pre style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.55;color:#daaa4c;white-space:pre-wrap;word-break:break-all;margin:0;">${safeBlob}</pre>
    </div>

    <h2 style="font-size:18px;font-weight:400;color:#e9e6dd;margin:0 0 16px 0;">
      Download Delfi
    </h2>
    <table cellpadding="0" cellspacing="0" border="0" style="margin:0 0 32px 0;">
      <tr>
        <td style="padding-right:12px;">
          <a href="${escapeHtml(macUrl)}" style="display:inline-block;padding:12px 22px;border:1px solid #daaa4c;border-radius:4px;color:#daaa4c;text-decoration:none;font-size:13px;letter-spacing:0.12em;text-transform:uppercase;">
            macOS &rarr;
          </a>
        </td>
        <td>
          <a href="${escapeHtml(winUrl)}" style="display:inline-block;padding:12px 22px;border:1px solid #daaa4c;border-radius:4px;color:#daaa4c;text-decoration:none;font-size:13px;letter-spacing:0.12em;text-transform:uppercase;">
            Windows &rarr;
          </a>
        </td>
      </tr>
    </table>

    <h2 style="font-size:18px;font-weight:400;color:#e9e6dd;margin:0 0 12px 0;">
      What to do next
    </h2>
    <ol style="font-size:15px;line-height:1.6;color:#cfcabd;margin:0 0 32px 0;padding-left:22px;">
      <li>Install Delfi from the link for your platform.</li>
      <li>Open the app. On first launch, paste the license key from
          above into the field labelled "License key". You only need
          to do this once per machine.</li>
    </ol>

    <p style="font-size:13px;line-height:1.6;color:#8c8675;margin:0 0 12px 0;">
      Lost this email? Reply and we'll resend.
    </p>
    <p style="font-size:13px;line-height:1.6;color:#8c8675;margin:0 0 32px 0;">
      Delfi never sees your funds, your wallet, or your trades. It runs
      entirely on your computer.
    </p>

    <hr style="border:none;border-top:1px solid #2a2c34;margin:32px 0;">
    <p style="font-size:12px;line-height:1.6;color:#8c8675;margin:0;">
      Delfi &middot;
      <a href="mailto:${escapeHtml(SUPPORT_INBOX)}" style="color:#8c8675;">${escapeHtml(SUPPORT_INBOX)}</a> &middot;
      <a href="https://delfibot.com" style="color:#8c8675;">delfibot.com</a>
    </p>
  </div>
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
