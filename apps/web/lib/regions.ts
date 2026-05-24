// apps/web/lib/regions.ts
//
// Region helpers for cookie / consent gating.
//
// We only need a binary signal here: "does this visitor require an
// explicit cookie-consent banner before we set non-essential
// (analytics, heatmap, etc.) cookies?"
//
// The set below is the union of jurisdictions where opt-in consent
// for analytics cookies is the default legal posture:
//   * EU 27 (GDPR + ePrivacy Directive)
//   * EEA add-ons: Iceland, Liechtenstein, Norway
//   * United Kingdom (UK GDPR + PECR; same posture, post-Brexit)
//   * Switzerland (FADP, similar regime, included to be safe)
//
// Visitors from anywhere else see no banner and have analytics fire
// immediately. This matches what Stripe, Vercel, Cloudflare, and
// most B2B SaaS do today.
//
// If the country code is missing (local dev with no Vercel edge,
// proxy that strips geo headers, etc.) we conservatively treat the
// visitor as in scope and show the banner. Better to over-show than
// to silently skip consent for an actual EU visitor.
//
// This is distinct from lib/geoblock/, which decides whether to
// block access entirely (e.g. US for offshore Polymarket compliance).
// Geoblock is access control; this is consent posture.

const CONSENT_REQUIRED_COUNTRIES: ReadonlySet<string> = new Set([
  // EU 27
  "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
  "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
  "PL", "PT", "RO", "SK", "SI", "ES", "SE",
  // EEA add-ons
  "IS", "LI", "NO",
  // UK
  "GB",
  // Switzerland (FADP)
  "CH",
]);

/**
 * Returns true if the visitor is in a jurisdiction that requires an
 * explicit cookie consent banner before non-essential cookies/scripts
 * can fire.
 *
 * @param country ISO 3166-1 alpha-2 country code, or null/undefined
 *                when geo could not be determined. Unknown defaults
 *                to "true" (require consent) for safety.
 */
export function consentRequiredForCountry(
  country: string | null | undefined,
): boolean {
  if (!country) return true;
  return CONSENT_REQUIRED_COUNTRIES.has(country.toUpperCase());
}
