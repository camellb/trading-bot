import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

import "../styles/dash.css";
import { DashboardShell, type DashboardUser } from "./shell";

function initialsFor(email: string, name: string | null): string {
  if (name) {
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    if (parts[0]) return parts[0].slice(0, 2).toUpperCase();
  }
  return (email[0] ?? "?").toUpperCase();
}

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/auth#login");

  const { data: cfg } = await supabase
    .from("user_config")
    .select("display_name, is_admin, bot_enabled, tour_completed_at, mode")
    .eq("user_id", user.id)
    .maybeSingle();

  const email = user.email ?? "";
  const metaName = (user.user_metadata?.full_name as string | undefined) ?? null;
  const name = (cfg?.display_name as string | null | undefined)?.trim() || metaName || null;
  const shellUser: DashboardUser = {
    email,
    name: name ?? email.split("@")[0] ?? "Trader",
    initials: initialsFor(email, name),
  };
  const isAdmin = Boolean(cfg?.is_admin);
  const botEnabled = Boolean(cfg?.bot_enabled);
  const tourCompleted = Boolean(cfg?.tour_completed_at);
  // The TRADING mode - what the bot actually does (vs. the read-only
  // view-mode toggle in the sidebar). The status pill renders this so
  // the user can see at a glance which market their running bot is
  // hitting without having to click into settings.
  const rawMode = (cfg?.mode as string | null | undefined) ?? null;
  const tradingMode: "simulation" | "live" | null =
    rawMode === "live" ? "live" : rawMode === "simulation" ? "simulation" : null;

  return (
    <DashboardShell
      user={shellUser}
      isAdmin={isAdmin}
      botEnabled={botEnabled}
      tradingMode={tradingMode}
      tourCompleted={tourCompleted}
    >
      {children}
    </DashboardShell>
  );
}
