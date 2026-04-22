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

  const email = user.email ?? "";
  const name = (user.user_metadata?.full_name as string | undefined) ?? null;
  const shellUser: DashboardUser = {
    email,
    name: name ?? email.split("@")[0] ?? "Trader",
    initials: initialsFor(email, name),
  };

  return <DashboardShell user={shellUser}>{children}</DashboardShell>;
}
