"use server";

import { revalidatePath } from "next/cache";

import { createClient } from "@/lib/supabase/server";

export type ActionResult = { ok: true } | { ok: false; error: string };

export async function setBotEnabled(enabled: boolean): Promise<ActionResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { ok: false, error: "Not signed in." };

  const { error } = await supabase
    .from("user_config")
    .update({ bot_enabled: enabled })
    .eq("user_id", user.id);

  if (error) {
    console.error("[dashboard/setBotEnabled] failed", {
      userId: user.id,
      enabled,
      code: error.code,
      message: error.message,
    });
    return { ok: false, error: error.message };
  }

  revalidatePath("/dashboard");
  return { ok: true };
}

export async function completeTour(): Promise<ActionResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { ok: false, error: "Not signed in." };

  const { error } = await supabase
    .from("user_config")
    .update({ tour_completed_at: new Date().toISOString() })
    .eq("user_id", user.id);

  if (error) {
    console.error("[dashboard/completeTour] failed", {
      userId: user.id,
      code: error.code,
      message: error.message,
    });
    return { ok: false, error: error.message };
  }

  revalidatePath("/dashboard");
  return { ok: true };
}
