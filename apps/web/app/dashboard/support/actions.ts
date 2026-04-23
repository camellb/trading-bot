"use server";

import { createClient } from "@/lib/supabase/server";
import { resend, RESEND_FROM, SUPPORT_INBOX } from "@/lib/resend";

export type SupportState = { error?: string; ok?: boolean };

export async function sendSupportMessage(
  _: SupportState,
  formData: FormData,
): Promise<SupportState> {
  const subject = String(formData.get("subject") ?? "").trim();
  const message = String(formData.get("message") ?? "").trim();

  if (!subject) return { error: "Add a subject so we know what it's about." };
  if (!message) return { error: "Add a message so we know how to help." };
  if (message.length > 8000) return { error: "Message is too long. Keep it under 8000 characters." };

  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user?.email) return { error: "You must be signed in to send a message." };

  const body = [
    `From: ${user.email}`,
    `User ID: ${user.id}`,
    "",
    message,
  ].join("\n");

  try {
    const { error } = await resend().emails.send({
      from: RESEND_FROM,
      to: SUPPORT_INBOX,
      replyTo: user.email,
      subject: `[Support] ${subject}`,
      text: body,
    });
    if (error) {
      console.error("[support] resend send failed", {
        userId: user.id,
        name: error.name,
        message: error.message,
      });
      return { error: "Couldn't send right now. Try again in a moment." };
    }
  } catch (e) {
    console.error("[support] resend threw", { userId: user.id, err: String(e) });
    return { error: "Couldn't send right now. Try again in a moment." };
  }

  return { ok: true };
}
