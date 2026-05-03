// apps/web/app/checkout/page.tsx
//
// Embedded Stripe Checkout. The buyer arrives here from a
// homepage CTA, sees a Delfi-themed wrapper with their order
// summary on the left and Stripe's secure card form on the
// right (or stacked on mobile). The card field is rendered by
// Stripe inside a same-origin iframe so PCI scope stays at
// SAQ A.
//
// Flow:
//   1. Component mounts. Reads UTM tags from `?utm_*` query
//      params (the homepage's `withUtm()` helper appends them).
//   2. POST /api/checkout/create-session with the UTMs.
//   3. Server returns `clientSecret`. We hand it to Stripe.js
//      via <EmbeddedCheckoutProvider>.
//   4. Stripe renders the form. On successful payment Stripe
//      redirects the iframe to `/checkout/return?session_id=...`
//      where we confirm and explain what happens next.
//
// Failure modes handled inline:
//   - NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY missing      -> error card
//   - /api/checkout/create-session 503/500            -> error card
//   - Stripe.js fails to load (blocked by extension)  -> error card

"use client";

import { useEffect, useMemo, useState } from "react";
import { loadStripe, Stripe } from "@stripe/stripe-js";
import {
  EmbeddedCheckoutProvider,
  EmbeddedCheckout,
} from "@stripe/react-stripe-js";
import "./checkout.css";

// loadStripe is async + idempotent; cache the promise at the module
// scope so a re-mount of the page doesn't re-fetch Stripe.js.
let stripePromise: Promise<Stripe | null> | null = null;
function getStripe(): Promise<Stripe | null> {
  if (stripePromise) return stripePromise;
  const key = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY;
  if (!key) {
    return Promise.resolve(null);
  }
  stripePromise = loadStripe(key);
  return stripePromise;
}

interface CreateSessionResponse {
  clientSecret?: string;
  sessionId?: string;
  error?: string;
}

export default function CheckoutPage() {
  const [clientSecret, setClientSecret] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Pull UTM params from the URL the marketing site appended
    // (utm_source, utm_medium, utm_content). Forward to the server
    // so they land on `metadata` of the Stripe session for later
    // attribution.
    const params = new URLSearchParams(window.location.search);
    const utm = {
      source:  params.get("utm_source")  || undefined,
      medium:  params.get("utm_medium")  || undefined,
      content: params.get("utm_content") || undefined,
    };

    let cancelled = false;

    if (!process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY) {
      setError(
        "Checkout is not yet configured. Email info@delfibot.com to buy in the meantime.",
      );
      return;
    }

    fetch("/api/checkout/create-session", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ utm }),
    })
      .then(async (res) => {
        const body = (await res.json()) as CreateSessionResponse;
        if (!res.ok || !body.clientSecret) {
          throw new Error(body.error || `HTTP ${res.status}`);
        }
        if (!cancelled) setClientSecret(body.clientSecret);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(
            e instanceof Error ? e.message : "Could not start checkout.",
          );
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const options = useMemo(
    () => (clientSecret ? { clientSecret } : null),
    [clientSecret],
  );

  return (
    <main className="checkout-page">
      <header className="checkout-header">
        <a href="/" className="checkout-back" aria-label="Back to delfibot.com">
          <span className="checkout-back-arrow" aria-hidden="true">←</span>
          <span className="checkout-back-text">Delfi</span>
        </a>
      </header>

      <div className="checkout-grid">
        <aside className="checkout-summary">
          <div>
            <div className="checkout-summary-eyebrow">Order</div>
            <h1 className="checkout-summary-title">Delfi</h1>
            <p className="checkout-summary-desc">
              Autonomous Polymarket trader.<br />Runs on your machine, 24/7.
            </p>
          </div>

          <ul className="checkout-summary-list">
            <li>Lifetime access. Yours forever.</li>
            <li>Your keys never leave your machine.</li>
            <li>All future updates included.</li>
            <li>14-day refund. No questions.</li>
          </ul>

          <div className="checkout-summary-trust">
            <div className="checkout-summary-trust-row">
              <svg viewBox="0 0 16 16" aria-hidden="true" width="14" height="14">
                <path
                  d="M8 1 2.5 3.5v4c0 3.3 2.4 6.4 5.5 7 3.1-.6 5.5-3.7 5.5-7v-4L8 1Zm-1 9L4.5 7.5l1-1L7 8l3.5-3.5 1 1L7 10Z"
                  fill="currentColor"
                />
              </svg>
              <span>Card data goes straight to Stripe, never to Delfi.</span>
            </div>
            <div className="checkout-summary-trust-row">
              <svg viewBox="0 0 16 16" aria-hidden="true" width="14" height="14">
                <path
                  d="M8 1a4 4 0 0 0-4 4v2H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1h-1V5a4 4 0 0 0-4-4Zm-2 6V5a2 2 0 1 1 4 0v2H6Z"
                  fill="currentColor"
                />
              </svg>
              <span>Encrypted end-to-end by Stripe.</span>
            </div>
          </div>
        </aside>

        <section className="checkout-stripe-wrap">
          <div className="checkout-stripe-frame">
            {error ? (
              <div className="checkout-error" role="alert">
                <div className="checkout-error-title">
                  Checkout couldn&apos;t start.
                </div>
                <div className="checkout-error-detail">{error}</div>
                <div className="checkout-error-fallback">
                  Email{" "}
                  <a href="mailto:info@delfibot.com">info@delfibot.com</a> and
                  we&apos;ll send you a payment link directly.
                </div>
              </div>
            ) : !options ? (
              <div className="checkout-loading" role="status">
                <span className="checkout-spinner" aria-hidden="true" />
                <span className="checkout-loading-text">
                  Loading secure checkout...
                </span>
              </div>
            ) : (
              <EmbeddedCheckoutProvider stripe={getStripe()} options={options}>
                <EmbeddedCheckout />
              </EmbeddedCheckoutProvider>
            )}
          </div>

          <div className="checkout-next">
            <div className="checkout-next-eyebrow">After you pay</div>
            <ol className="checkout-next-steps">
              <li>
                <span className="checkout-next-num">1</span>
                Your license key lands in your inbox in seconds.
              </li>
              <li>
                <span className="checkout-next-num">2</span>
                Download Delfi for macOS or Windows from the email.
              </li>
              <li>
                <span className="checkout-next-num">3</span>
                Paste the license on first launch. You&apos;re live.
              </li>
            </ol>
          </div>
        </section>
      </div>
    </main>
  );
}
