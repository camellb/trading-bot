import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

import AdminNav from "./nav";

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/auth#login");

  const { data: cfg } = await supabase
    .from("user_config")
    .select("is_admin")
    .eq("user_id", user.id)
    .maybeSingle();

  if (!cfg?.is_admin) redirect("/dashboard");

  return <AdminNav>{children}</AdminNav>;
}
