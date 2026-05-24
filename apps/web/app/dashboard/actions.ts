"use server";

import { revalidatePath } from "next/cache";

import { botPost } from "@/lib/bot-proxy";
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

  // Side effects the bot owns: Telegram notify + kick an immediate scan so
  // the user doesn't have to wait for the next 5-min cycle. Both are
  // fail-open: the UI has already flipped bot_enabled in Supabase, so any
  // bot hiccup is logged but doesn't block the user.
  try {
    const toggleRes = await botPost<{ status: string }>(
      "/api/bot-toggle",
      { enabled },
      8_000,
    );
    if (!toggleRes.ok) {
      console.warn("[dashboard/setBotEnabled] bot-toggle failed", {
        userId: user.id,
        enabled,
        status: toggleRes.status,
        error: toggleRes.error,
      });
    }
    if (enabled) {
      const scanRes = await botPost<{ status: string }>(
        "/api/scan-now",
        {},
        8_000,
      );
      if (!scanRes.ok) {
        console.warn("[dashboard/setBotEnabled] scan-now failed", {
          userId: user.id,
          status: scanRes.status,
          error: scanRes.error,
        });
      }
    }
  } catch (e) {
    console.warn("[dashboard/setBotEnabled] bot side-effect threw", {
      userId: user.id,
      enabled,
      error: e instanceof Error ? e.message : String(e),
    });
  }

  revalidatePath("/dashboard");
  return { ok: true };
}

export async function completeTour(): Promise<ActionResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { ok: false, error: "Not signed in." };

  // Upsert so a missing user_config row self-heals - otherwise .update()
  // silently affects 0 rows and the tour replays on every refresh.
  const { error } = await supabase
    .from("user_config")
    .upsert(
      {
        user_id: user.id,
        tour_completed_at: new Date().toISOString(),
      },
      { onConflict: "user_id" },
    );

  if (error) {
    console.error("[dashboard/completeTour] failed", {
      userId: user.id,
      code: error.code,
      message: error.message,
      details: error.details,
      hint: error.hint,
    });
    return { ok: false, error: error.message };
  }

  revalidatePath("/dashboard");
  return { ok: true };
}
