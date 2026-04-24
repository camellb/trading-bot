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

  // Venue is enforced by the CHECK constraint on user_config.venue (migration
  // 024). Anything other than the two known values falls back to the offshore
  // Polymarket default so a bad form post cannot trip the CHECK and abort
  // the upsert. The onboarding UI only sends these two values.
  const rawVenue = String(formData.get("venue") ?? "").trim();
  const venue: "polymarket" | "polymarket_us" =
    rawVenue === "polymarket_us" ? "polymarket_us" : "polymarket";

  const rawRiskProfile = String(formData.get("risk_profile") ?? "").trim();
  const riskProfile: RiskProfile =
    rawRiskProfile === "cautious" || rawRiskProfile === "aggressive"
      ? rawRiskProfile
      : "balanced";
  const riskValues = RISK_PRESETS[riskProfile];

  // Simulation bankroll is fixed at $1,000 for every new user. Live mode
  // uses the real wallet balance once CLOB is wired; until then we still
  // seed simulation bankroll = $1000 so the user has a working sim view
  // regardless of which mode they picked at onboarding.
  //
  // IMPORTANT: never overwrite starting_cash on re-onboarding. The upsert
  // below conflicts on user_id; if a row already exists (the user is
  // re-submitting, or a bot process seeded the row), we must preserve the
  // bankroll they have been trading against. Otherwise we reset their sim
  // history to a 0 baseline and their P&L math goes negative.
  const { data: existingRow } = await supabase
    .from("user_config")
    .select("starting_cash")
    .eq("user_id", user.id)
    .maybeSingle();

  const startingCash =
    existingRow?.starting_cash != null
      ? Number(existingRow.starting_cash)
      : 1000;

  const { error } = await supabase
    .from("user_config")
    .upsert(
      {
        user_id: user.id,
        display_name: displayName,
        mode,
        venue,
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

  // Live users still need to connect venue credentials before the bot will
  // trade for them. The Connections page is venue-aware (offshore vs US)
  // so it shows the right credential form based on what they picked above.
  // Simulation users go straight to the dashboard.
  if (mode === "live") {
    redirect("/dashboard/settings/connections?setup=live");
  }
  redirect("/dashboard");
}
