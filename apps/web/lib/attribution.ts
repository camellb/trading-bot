const STORAGE_KEY = "delfi.attribution";
const MAX_VALUE_LENGTH = 500;

const QUERY_KEYS = [
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_content",
  "utm_term",
  "utm_id",
  "fbclid",
  "gclid",
  "msclkid",
] as const;

type QueryKey = (typeof QUERY_KEYS)[number];

export interface AttributionData extends Partial<Record<QueryKey, string>> {
  captured_at_ms?: string;
  landing_path?: string;
  referrer_origin?: string;
  delfi_cta?: string;
  fbc?: string;
  fbp?: string;
}

function clean(value: string | null | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed ? trimmed.slice(0, MAX_VALUE_LENGTH) : undefined;
}

function readStored(): AttributionData {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) ?? "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeStored(data: AttributionData): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch {
    // Attribution must never block navigation or checkout.
  }
}

function trackingAllowed(): boolean {
  try {
    const consent = window.localStorage.getItem("delfi.cookie-consent");
    const consentRequired = document.documentElement.dataset.consentRequired !== "false";
    return consent === "accepted" || (!consentRequired && consent !== "rejected");
  } catch {
    return false;
  }
}

function referrerOrigin(): string | undefined {
  if (!document.referrer) return undefined;
  try {
    return clean(new URL(document.referrer).origin);
  } catch {
    return undefined;
  }
}

function readCookie(name: string): string | undefined {
  const prefix = `${name}=`;
  const raw = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
    ?.slice(prefix.length);
  if (!raw) return undefined;
  try {
    return clean(decodeURIComponent(raw));
  } catch {
    return clean(raw);
  }
}

/**
 * INPUT axis: acquisition parameters on the current URL plus this tab's
 * previously captured values.
 * OUTPUT axis: one flat attribution object that can be forwarded to Stripe.
 * INVARIANT: an inbound Meta utm_campaign and fbclid survive every homepage
 * CTA and reach the checkout session unchanged. CTA location is stored in
 * delfi_cta and never overwrites utm_content from the ad.
 */
export function captureAttribution(): AttributionData {
  if (typeof window === "undefined") return {};

  const stored = readStored();
  const params = new URLSearchParams(window.location.search);
  const current: AttributionData = {};

  for (const key of QUERY_KEYS) {
    const value = clean(params.get(key));
    if (value) current[key] = value;
  }

  const hasNewAcquisition = QUERY_KEYS.some((key) => Boolean(current[key]));
  const forwardedCapturedAt = clean(params.get("captured_at_ms"));
  const captured: AttributionData = {
    ...stored,
    ...current,
    captured_at_ms: hasNewAcquisition
      ? forwardedCapturedAt ?? String(Date.now())
      : stored.captured_at_ms ?? String(Date.now()),
    landing_path: hasNewAcquisition
      ? clean(params.get("landing_path")) ?? window.location.pathname
      : stored.landing_path ?? window.location.pathname,
    referrer_origin: hasNewAcquisition
      ? clean(params.get("referrer_origin")) ?? referrerOrigin()
      : stored.referrer_origin ?? referrerOrigin(),
  };

  const cta = clean(params.get("delfi_cta"));
  if (cta) captured.delfi_cta = cta;

  if (!trackingAllowed()) {
    delete captured.fbclid;
    delete captured.gclid;
    delete captured.msclkid;
    delete captured.fbc;
    delete captured.fbp;
  }

  writeStored(captured);
  return captured;
}

export function checkoutUrl(
  baseUrl: string,
  ctaLocation: string,
): string {
  if (baseUrl.startsWith("mailto:")) return baseUrl;
  if (typeof window === "undefined") return baseUrl;

  const attribution = captureAttribution();
  const target = new URL(baseUrl, window.location.origin);
  for (const [key, value] of Object.entries(attribution)) {
    if (value) target.searchParams.set(key, value);
  }
  target.searchParams.set("delfi_cta", ctaLocation);

  return /^https?:\/\//i.test(baseUrl)
    ? target.toString()
    : `${target.pathname}${target.search}${target.hash}`;
}

export function checkoutAttribution(): AttributionData {
  const attribution = captureAttribution();
  if (!trackingAllowed()) return attribution;

  const fbp = readCookie("_fbp");
  const existingFbc = readCookie("_fbc");
  const fbc = existingFbc || (
    attribution.fbclid
      ? `fb.1.${attribution.captured_at_ms ?? Date.now()}.${attribution.fbclid}`
      : undefined
  );

  return {
    ...attribution,
    ...(fbp ? { fbp } : {}),
    ...(fbc ? { fbc } : {}),
  };
}
