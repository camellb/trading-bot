import { NextResponse, type NextRequest } from "next/server";
import { createServerClient } from "@supabase/ssr";

import { decideGeoblock, type GeoblockDecision } from "@/lib/geoblock/check";

const PROTECTED_PREFIXES = ["/dashboard", "/onboarding", "/admin", "/subscribe"];
const SUBSCRIPTION_GATED_PREFIXES = ["/dashboard", "/onboarding"];
const AUTH_ROUTES = ["/auth", "/login"];

// Paths that must always be reachable even from a blocked jurisdiction.
// /geoblocked itself (else infinite redirect), auth callback so a user
// who started logging in from outside a blocked region can still finish,
// and /api/auth for internal Supabase session flows.
const GEOBLOCK_BYPASS_PREFIXES = [
  "/geoblocked",
  "/auth/callback",
  "/api/auth",
];

export async function updateSession(request: NextRequest) {
  const pathname = request.nextUrl.pathname;

  // --- Step 1. Geoblock preflight.
  // Runs before the Supabase client is constructed so users in a blocked
  // region do not consume auth quota. Admin bypass is applied below once
  // we know who the user is.
  const bypassGeoblock = GEOBLOCK_BYPASS_PREFIXES.some(
    (p) => pathname === p || pathname.startsWith(`${p}/`),
  );

  let geoblockDecision: GeoblockDecision = { blocked: false };
  if (!bypassGeoblock) {
    const countryHeader = request.headers.get("x-vercel-ip-country");
    const regionHeader = request.headers.get("x-vercel-ip-country-region");
    geoblockDecision = await decideGeoblock(countryHeader, regionHeader);
  }

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

  // --- Step 2. Admin bypass for geoblock.
  // Operators need to hit the product from anywhere (including blocked
  // regions) to test. We check is_admin only if the request was flagged
  // as blocked, so the common path pays zero extra queries.
  if (geoblockDecision.blocked) {
    let isAdmin = false;
    if (user) {
      const { data: cfg } = await supabase
        .from("user_config")
        .select("is_admin")
        .eq("user_id", user.id)
        .maybeSingle();
      isAdmin = cfg?.is_admin === true;
    }
    if (!isAdmin) {
      const url = request.nextUrl.clone();
      url.pathname = "/geoblocked";
      url.search = "";
      url.hash = "";
      url.searchParams.set("cc", geoblockDecision.country_code);
      if (geoblockDecision.subdivision_code) {
        url.searchParams.set("sub", geoblockDecision.subdivision_code);
      }
      return NextResponse.redirect(url);
    }
  }

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
