"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

export async function completeOnboarding() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/auth#login");

  await supabase
    .from("user_config")
    .upsert(
      { user_id: user.id, onboarded_at: new Date().toISOString() },
      { onConflict: "user_id" },
    );

  redirect("/dashboard");
}
