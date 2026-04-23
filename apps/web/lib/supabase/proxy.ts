import { NextResponse, type NextRequest } from "next/server";
import { createServerClient } from "@supabase/ssr";

const PROTECTED_PREFIXES = ["/dashboard", "/onboarding", "/admin", "/subscribe"];
const SUBSCRIPTION_GATED_PREFIXES = ["/dashboard", "/onboarding"];
const AUTH_ROUTES = ["/auth", "/login"];

export async function updateSession(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) =>
            request.cookies.set(name, value),
          );
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
        },
      },
    },
  );

  const {
    data: { user },
  } = await supabase.auth.getUser();

  const pathname = request.nextUrl.pathname;
  const isProtected = PROTECTED_PREFIXES.some((p) => pathname.startsWith(p));
  const isAuthRoute = AUTH_ROUTES.some(
    (p) => pathname === p || pathname.startsWith(`${p}/`),
  );

  if (isProtected && !user) {
    const url = request.nextUrl.clone();
    url.pathname = "/auth";
    url.hash = "login";
    url.searchParams.set("redirect", pathname);
    return NextResponse.redirect(url);
  }

  if (isAuthRoute && user && !pathname.startsWith("/auth/callback")) {
    const url = request.nextUrl.clone();
    url.pathname = "/dashboard";
    url.search = "";
    url.hash = "";
    return NextResponse.redirect(url);
  }

  const needsSubscriptionCheck =
    user && SUBSCRIPTION_GATED_PREFIXES.some((p) => pathname.startsWith(p));

  if (needsSubscriptionCheck) {
    const { data: cfg } = await supabase
      .from("user_config")
      .select("subscription_status, is_admin")
      .eq("user_id", user.id)
      .maybeSingle();

    const hasAccess =
      cfg?.is_admin === true || cfg?.subscription_status === "active";

    if (!hasAccess) {
      const url = request.nextUrl.clone();
      url.pathname = "/subscribe";
      url.search = "";
      url.hash = "";
      return NextResponse.redirect(url);
    }
  }

  return response;
}
