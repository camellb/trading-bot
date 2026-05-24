// Edge-safe geoblock helpers.
//
// Callers:
//   1. apps/web/proxy.ts (edge runtime). Calls decideGeoblock() at the top
//      of updateSession before any auth work. Redirects to /geoblocked on
//      a blocked match.
//   2. apps/web/app/admin/geoblock/page.tsx (node runtime, server component).
//      Calls listRules() to render the admin table.
//   3. apps/web/app/api/admin/geoblock/route.ts. Calls invalidateCache()
//      after POST/DELETE so the next request sees the new rule set.
//
// The rule set is fetched through Supabase PostgREST with the anon key.
// Reading geoblock_rules does not require a session because the proxy runs
// before any auth. RLS on the table is "SELECT USING (true)".
//
// Results are cached in-module for CACHE_TTL_MS so we do not hit Supabase on
// every request. On the edge this cache lives for the lifetime of a single
// isolate; good enough for our traffic levels, and stale rules disappear
// within seconds.

const CACHE_TTL_MS = 30_000;

export type GeoblockRule = {
  id: number;
  country_code: string;                 // ISO 3166-1 alpha-2, uppercase, e.g. "US"
  subdivision_code: string | null;      // ISO 3166-2 suffix only, e.g. "ON"
  reason: string | null;
  created_at: string;                   // ISO 8601 timestamptz from PostgREST
};

export type GeoblockDecision =
  | { blocked: false }
  | {
      blocked: true;
      rule: GeoblockRule;
      country_code: string;
      subdivision_code: string | null;
    };

let cache: { at: number; rules: GeoblockRule[] } | null = null;

function supabaseUrl(): string {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  if (!url) throw new Error("NEXT_PUBLIC_SUPABASE_URL not set");
  return url.replace(/\/+$/, "");
}

function anonKey(): string {
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!key) throw new Error("NEXT_PUBLIC_SUPABASE_ANON_KEY not set");
  return key;
}

/**
 * Fetch the full rule set from Supabase PostgREST.
 * Cached in-module for CACHE_TTL_MS.
 */
export async function listRules(options?: { force?: boolean }): Promise<GeoblockRule[]> {
  const now = Date.now();
  if (!options?.force && cache && now - cache.at < CACHE_TTL_MS) {
    return cache.rules;
  }

  try {
    const res = await fetch(
      `${supabaseUrl()}/rest/v1/geoblock_rules?select=id,country_code,subdivision_code,reason,created_at&order=country_code.asc,subdivision_code.asc`,
      {
        headers: {
          apikey: anonKey(),
          Authorization: `Bearer ${anonKey()}`,
          Accept: "application/json",
        },
        // Revalidate on the server side every CACHE_TTL_MS seconds for
        // node runtime callers (admin page). Edge proxy ignores `next` and
        // uses the in-module cache above.
        next: { revalidate: Math.floor(CACHE_TTL_MS / 1000) },
      },
    );

    if (!res.ok) {
      // Fail open: if the rules table is unreachable, do not block users.
      // A partial or empty fetch is worse than no enforcement, because it
      // could lock us out during a Supabase outage.
      console.warn("[geoblock] listRules failed", res.status, await res.text().catch(() => ""));
      return cache?.rules ?? [];
    }

    const rules = (await res.json()) as GeoblockRule[];
    cache = { at: now, rules };
    return rules;
  } catch (err) {
    console.warn("[geoblock] listRules error", err);
    return cache?.rules ?? [];
  }
}

/**
 * Normalize a subdivision value. Accepts both full ISO 3166-2 strings
 * (e.g. "CA-ON") and bare suffixes (e.g. "ON"), returns the suffix only
 * in uppercase. Returns null if empty.
 */
export function normalizeSubdivision(
  raw: string | null | undefined,
  countryCode: string | null,
): string | null {
  if (!raw) return null;
  const upper = raw.trim().toUpperCase();
  if (!upper) return null;
  if (countryCode && upper.startsWith(`${countryCode}-`)) {
    return upper.slice(countryCode.length + 1) || null;
  }
  return upper;
}

/**
 * Match a country + subdivision against the rule set.
 *
 * Rule types (both stored as rows in geoblock_rules):
 *   - Country-wide: (country_code, subdivision_code=NULL). Blocks everyone
 *     with that country code regardless of region.
 *   - Subdivision-only: (country_code, subdivision_code='ON'). Blocks only
 *     that specific region. The rest of the country is unaffected.
 *
 * Country-wide rules take precedence.
 */
export function matchRule(
  rules: ReadonlyArray<GeoblockRule>,
  countryCode: string | null,
  subdivisionCode: string | null,
): GeoblockRule | null {
  if (!countryCode) return null;
  const cc = countryCode.toUpperCase();
  const sub = subdivisionCode ? subdivisionCode.toUpperCase() : null;

  const countryWide = rules.find(
    (r) => r.country_code === cc && !r.subdivision_code,
  );
  if (countryWide) return countryWide;

  if (sub) {
    const subMatch = rules.find(
      (r) => r.country_code === cc && r.subdivision_code === sub,
    );
    if (subMatch) return subMatch;
  }

  return null;
}

/** One-shot convenience used by the edge proxy. */
export async function decideGeoblock(
  countryCode: string | null,
  subdivisionRaw: string | null,
): Promise<GeoblockDecision> {
  if (!countryCode) return { blocked: false };
  const cc = countryCode.toUpperCase();
  const sub = normalizeSubdivision(subdivisionRaw, cc);
  const rules = await listRules();
  const rule = matchRule(rules, cc, sub);
  if (!rule) return { blocked: false };
  return {
    blocked: true,
    rule,
    country_code: cc,
    subdivision_code: sub,
  };
}

/** Invalidate the in-module cache. Called after admin writes. */
export function invalidateCache(): void {
  cache = null;
}
