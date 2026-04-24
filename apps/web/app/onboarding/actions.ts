"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

// Onboarding risk presets. These get written straight into user_config on
// completion so the picker actually does something. All values live inside
// USER_CONFIG_BOUNDS (apps/bot/engine/user_config.py) - the bot would reject
// anything outside the envelope. Users can still edit individual fields
// later from the Risk controls page.
const RISK_PRESETS = {
  cautious: {
    daily_loss_limit_pct: 0.05,
    weekly_loss_limit_pct: 0.10,
    drawdown_halt_pct: 0.25,
    streak_cooldown_losses: 2,
    base_stake_pct: 0.01,
    max_stake_pct: 0.02,
    dry_powder_reserve_pct: 0.30,
  },
  balanced: {
    daily_loss_limit_pct: 0.10,
    weekly_loss_limit_pct: 0.20,
    drawdown_halt_pct: 0.40,
    streak_cooldown_losses: 3,
    base_stake_pct: 0.02,
    max_stake_pct: 0.03,
    dry_powder_reserve_pct: 0.20,
  },
  aggressive: {
    daily_loss_limit_pct: 0.20,
    weekly_loss_limit_pct: 0.40,
    drawdown_halt_pct: 0.50,
    streak_cooldown_losses: 5,
    base_stake_pct: 0.03,
    max_stake_pct: 0.05,
    dry_powder_reserve_pct: 0.10,
  },
} as const;

type RiskProfile = keyof typeof RISK_PRESETS;

export async function completeOnboarding(formData: FormData) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/auth#login");

  const displayName = String(formData.get("display_name") ?? "").trim();
  if (!displayName) redirect("/onboarding");

  const rawMode = String(formData.get("mode") ?? "").trim();
  const mode: "simulation" | "live" =
    rawMode === "live" ? "live" : "simulation";

  const rawRiskProfile = String(formData.get("risk_profile") ?? "").trim();
  const riskProfile: RiskProfile =
    rawRiskProfile === "cautious" || rawRiskProfile === "aggressive"
      ? rawRiskProfile
      : "balanced";
  const riskValues = RISK_PRESETS[riskProfile];

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
        ...riskValues,
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
