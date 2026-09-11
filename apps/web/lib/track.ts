// apps/web/lib/track.ts
//
// Client-side conversion-event helpers for Meta Pixel + GA4.
//
// Events wait briefly for each provider to load. This prevents a
// checkout or purchase event from being lost when React mounts before
// an afterInteractive analytics script has initialized.
//
// Why both: GA4 powers the funnel reports we read every day
// (Realtime + Reports → Engagement → Conversions). Meta Pixel
// powers ad campaign optimisation and Audience building. They
// don't replace each other; you want both events firing on the
// same step so each platform sees the conversion.
//
// Why a stable event id: the Stripe session_id deduplicates the
// browser Purchase against the matching server-side Conversions API
// event and ties GA4's transaction_id to the Stripe order.

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

const fired = new Set<string>();
const pending = new Map<string, { attempts: number; send: () => boolean }>();
let retryTimer: ReturnType<typeof setTimeout> | null = null;

function flushPending(): void {
  retryTimer = null;
  for (const [key, event] of pending) {
    try {
      if (event.send()) {
        fired.add(key);
        pending.delete(key);
        continue;
      }
    } catch {
      // Analytics must never break the page.
    }
    event.attempts += 1;
    if (event.attempts >= 240) pending.delete(key);
  }
  if (pending.size > 0) retryTimer = setTimeout(flushPending, 500);
}

function onceReady(key: string, send: () => boolean): void {
  if (fired.has(key) || pending.has(key)) return;
  pending.set(key, { attempts: 0, send });
  if (retryTimer === null) flushPending();
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
  onceReady(`meta:initiate:${sessionId}`, () => {
    if (!window.fbq) return false;
    window.fbq(
      "track",
      "InitiateCheckout",
      {},
      { eventID: sessionId },
    );
    return true;
  });
  onceReady(`ga:initiate:${sessionId}`, () => {
    if (!window.gtag) return false;
    window.gtag("event", "begin_checkout", {
      transaction_id: sessionId,
    });
    return true;
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
  onceReady(`meta:purchase:${eventId}`, () => {
    if (!window.fbq) return false;
    window.fbq(
      "track",
      "Purchase",
      { value, currency },
      { eventID: eventId },
    );
    return true;
  });
  onceReady(`ga:purchase:${eventId}`, () => {
    if (!window.gtag) return false;
    window.gtag("event", "purchase", {
      transaction_id: eventId,
      value,
      currency,
    });
    return true;
  });
}
