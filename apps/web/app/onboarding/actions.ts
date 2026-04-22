"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

export async function completeOnboarding(formData: FormData) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/auth#login");

  const displayName = String(formData.get("display_name") ?? "").trim();
  if (!displayName) redirect("/onboarding");

  const { error } = await supabase
    .from("user_config")
    .upsert(
      {
        user_id: user.id,
        display_name: displayName,
        onboarded_at: new Date().toISOString(),
      },
      { onConflict: "user_id" },
    );

  if (error) {
    console.error("[onboarding] upsert user_config failed", {
      userId: user.id,
      code: error.code,
      message: error.message,
      details: error.details,
      hint: error.hint,
    });
  }

  redirect("/dashboard");
}
