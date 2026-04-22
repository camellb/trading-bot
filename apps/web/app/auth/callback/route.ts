import { NextResponse, type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const url = request.nextUrl;
  const code = url.searchParams.get("code");
  const next = url.searchParams.get("next") ?? "/dashboard";
  const redirectUrl = new URL(next, url.origin);

  if (code) {
    const supabase = await createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (error) {
      const errUrl = new URL("/auth", url.origin);
      errUrl.hash = "login";
      errUrl.searchParams.set("error", error.message);
      return NextResponse.redirect(errUrl);
    }
  }

  return NextResponse.redirect(redirectUrl);
}
