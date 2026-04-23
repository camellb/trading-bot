"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

export async function completeOnboarding(formData: FormData) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/auth#login");

  const displayName = String(formData.get("display_name") ?? "").trim();
  if (!displayName) redirect("/onboarding");

  const rawMode = String(formData.get("mode") ?? "").trim();
  const mode: "simulation" | "live" =
    rawMode === "live" ? "live" : "simulation";

  // Simulation bankroll is fixed at $1,000 for every new user. Live mode
  // uses the real wallet balance once CLOB is wired, so we persist 0 as a
  // placeholder the executor can read.
  const startingCash = mode === "live" ? 0 : 1000;

  const { error } = await supabase
    .from("user_config")
    .upsert(
      {
        user_id: user.id,
        display_name: displayName,
        mode,
        starting_cash: startingCash,
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
    const params = new URLSearchParams({
      error: "save_failed",
      code: error.code ?? "",
      message: error.message ?? "",
    });
    redirect(`/onboarding?${params.toString()}`);
  }

  // Live users still need to connect Polymarket credentials before the bot
  // will trade for them. Route them to the credentials page; simulation
  // users go straight to the dashboard.
  if (mode === "live") {
    redirect("/dashboard/settings/account?setup=live");
  }
  redirect("/dashboard");
}
