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
    .select("display_name, is_admin")
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

  return <DashboardShell user={shellUser} isAdmin={isAdmin}>{children}</DashboardShell>;
}
