import { Resend } from "resend";

// Shared Resend client + sender constants. Every app-side send goes through
// here so the From address stays consistent and the API key is loaded once.

export const RESEND_FROM = "Delfi <noreply@delfibot.com>";
export const SUPPORT_INBOX = "info@delfibot.com";

let client: Resend | null = null;

export function resend(): Resend {
  if (client) return client;
  const key = process.env.RESEND_API_KEY;
  if (!key) {
    throw new Error(
      "RESEND_API_KEY is not set. Add it in Vercel env vars (Production + Preview).",
    );
  }
  client = new Resend(key);
  return client;
}
