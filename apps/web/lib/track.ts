// apps/web/lib/track.ts
//
// Client-side conversion-event helpers for Meta Pixel + GA4.
//
// These are fire-and-forget: if either provider isn't loaded
// (consent not granted, ad-blocker, NEXT_PUBLIC_*_ID env var not
// set in this environment), the call no-ops via the optional
// chaining on `window.fbq?.` / `window.gtag?.`.
//
// Why both: GA4 powers the funnel reports we read every day
// (Realtime + Reports → Engagement → Conversions). Meta Pixel
// powers ad campaign optimisation and Audience building. They
// don't replace each other; you want both events firing on the
// same step so each platform sees the conversion.
//
// Why a stable event id: the Stripe session_id (a) gives us
// a stable dedup key for when we add server-side Conversions
// API later, and (b) lets GA4's `transaction_id` field tie a
// purchase row to the same Stripe session in our DB. Reusing
// the same id on a second fire dedupes server-side; locally
// we also guard against double-firing via a module-scope
// `Set` (resists React StrictMode and accidental re-mounts).

// Augment Window so callers can call these without each page
// re-declaring the same `declare global` block.
type GtagFn = (...args: unknown[]) => void;
type FbqFn  = (...args: unknown[]) => void;
declare global {
  interface Window {
    gtag?: GtagFn;
    fbq?:  FbqFn;
  }
}

// Module-scope dedup set. Cleared on every full page navigation
// because the JS module reloads. Within a single page session
// (React effects re-running, StrictMode double-invoke) it stops
// duplicate fires.
const fired = new Set<string>();

function once(key: string, fn: () => void): void {
  if (fired.has(key)) return;
  fired.add(key);
  try {
    fn();
  } catch {
    // Analytics never breaks the page.
  }
}

/**
 * Fire when the buyer reaches /checkout and a Stripe session has
 * been minted for them. This is the "started checkout, ready to
 * pay" signal Meta + GA4 use as the mid-funnel step.
 *
 * `sessionId` is the Stripe Checkout Session id; reused on
 * `Purchase` so server-side dedup (when we add CAPI) lines up.
 */
export function trackInitiateCheckout(sessionId: string): void {
  once(`initiate:${sessionId}`, () => {
    window.fbq?.(
      "track",
      "InitiateCheckout",
      {},
      { eventID: sessionId },
    );
    window.gtag?.("event", "begin_checkout", {
      transaction_id: sessionId,
    });
  });
}

interface PurchaseArgs {
  /** Stripe Checkout Session id. Stable, idempotent dedup key. */
  eventId: string;
  /** Order total in major units (e.g. 249 for USD 249.00). */
  value: number;
  /** ISO 4217 currency code, uppercase (e.g. "USD"). */
  currency: string;
}

/**
 * Fire when the return page confirms `status === "complete"` and
 * `payment_status === "paid"`. Sends Pixel `Purchase` (with
 * value + currency for ad ROAS) and GA4 `purchase` (with
 * `transaction_id` for ecommerce reports).
 */
export function trackPurchase({
  eventId,
  value,
  currency,
}: PurchaseArgs): void {
  once(`purchase:${eventId}`, () => {
    window.fbq?.(
      "track",
      "Purchase",
      { value, currency },
      { eventID: eventId },
    );
    window.gtag?.("event", "purchase", {
      transaction_id: eventId,
      value,
      currency,
    });
  });
}
