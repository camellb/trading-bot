import { NextResponse, type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const url = request.nextUrl;
  const code = url.searchParams.get("code");
  const next = url.searchParams.get("next");

  const supabase = await createClient();

  if (code) {
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (error) {
      const errUrl = new URL("/auth", url.origin);
      errUrl.hash = "login";
      errUrl.searchParams.set("error", error.message);
      return NextResponse.redirect(errUrl);
    }
  }

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    const authUrl = new URL("/auth", url.origin);
    authUrl.hash = "login";
    return NextResponse.redirect(authUrl);
  }

  const { data: cfg, error: cfgError } = await supabase
    .from("user_config")
    .select("onboarded_at, subscription_status, is_admin")
    .eq("user_id", user.id)
    .maybeSingle();

  if (cfgError) {
    console.error("[auth/callback] user_config select failed", {
      userId: user.id,
      code: cfgError.code,
      message: cfgError.message,
      details: cfgError.details,
      hint: cfgError.hint,
    });
  } else {
    console.log("[auth/callback] user_config lookup", {
      userId: user.id,
      hasRow: !!cfg,
      onboardedAt: cfg?.onboarded_at ?? null,
      subscriptionStatus: cfg?.subscription_status ?? null,
    });
  }

  const hasAccess =
    cfg?.is_admin === true || cfg?.subscription_status === "active";

  let target: string;
  if (!hasAccess) {
    target = "/subscribe";
  } else if (!cfg?.onboarded_at) {
    target = "/onboarding";
  } else {
    target = next ?? "/dashboard";
  }

  return NextResponse.redirect(new URL(target, url.origin));
}
