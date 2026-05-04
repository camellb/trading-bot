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
              Autonomous and self-improving bot for Polymarket
            </p>
          </div>

          <ul className="checkout-trust">
            <li className="checkout-trust-row">
              <span className="checkout-trust-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18.178 8C16.6 8 15.6 9 14.5 10.6 13.4 12.2 12 13.6 9.822 13.6 7.978 13.6 6.4 12.045 6.4 9.978 6.4 7.911 7.978 6.356 9.822 6.356 12 6.356 13.4 7.756 14.5 9.378 15.6 10.978 16.6 12 18.178 12 19.844 12 21.6 10.667 21.6 9 21.6 7.333 19.844 6 18.178 6"/>
                  <path d="M5.822 12C7.4 12 8.4 13 9.5 14.6 10.6 16.2 12 17.6 14.178 17.6 16.022 17.6 17.6 16.045 17.6 13.978"/>
                </svg>
              </span>
              <span className="checkout-trust-label">Lifetime access</span>
            </li>
            <li className="checkout-trust-row">
              <span className="checkout-trust-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 12a9 9 0 1 0 3-6.7"/>
                  <path d="M3 4v5h5"/>
                </svg>
              </span>
              <span className="checkout-trust-label">14-day refund</span>
            </li>
            <li className="checkout-trust-row">
              <span className="checkout-trust-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round">
                  <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>
                </svg>
              </span>
              <span className="checkout-trust-label">Instant delivery</span>
            </li>
          </ul>

          <div className="checkout-next">
            <div className="checkout-next-eyebrow">After you pay</div>
            <ol className="checkout-next-steps">
              <li>
                <span className="checkout-next-num">1</span>
                You will receive a license key to your email
              </li>
              <li>
                <span className="checkout-next-num">2</span>
                Download Delfi on your computer
              </li>
              <li>
                <span className="checkout-next-num">3</span>
                Activate using your license
              </li>
              <li>
                <span className="checkout-next-num">4</span>
                Connect your credentials and start trading
              </li>
            </ol>
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
                <span className="checkout-loading-text">Loading...</span>
              </div>
            ) : (
              <EmbeddedCheckoutProvider stripe={getStripe()} options={options}>
                <EmbeddedCheckout />
              </EmbeddedCheckoutProvider>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
