"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  readConsent,
  saveConsent,
  type CookieConsent,
} from "../../components/CookieBanner";
import "../../styles/content.css";

export default function CookiesPage() {
  return (
    <main className="content-main">
      <div className="content-eyebrow">Legal</div>
      <h1 className="content-h1">Cookies Policy</h1>
      <p className="content-lede">
        This page explains the cookies and browser storage used on delfibot.com and lets you enable or disable
        analytics and advertising measurement.
      </p>
      <div className="content-meta">Effective 2026-04-01 · Last updated 2026-09-11</div>

      <div className="content-body">
        <h2>1. Browser storage</h2>
        <ul>
          <li>
            <strong>Consent preference:</strong> <code>delfi.cookie-consent</code> in local storage remembers
            whether analytics and advertising measurement are enabled. It remains until you change the choice or
            clear browser storage.
          </li>
          <li>
            <strong>Checkout attribution:</strong> <code>delfi.attribution</code> in session storage retains
            campaign parameters, the landing path, referring origin, and the Delfi button used to reach
            checkout. Advertising click identifiers are included only when advertising measurement is enabled.
            The stored data expires when the browser tab session ends.
          </li>
          <li>
            <strong>Stripe Checkout:</strong> Stripe may use cookies or similar storage in the embedded payment
            form to process payments, prevent fraud, and protect the checkout.
          </li>
        </ul>
        <p>
          The marketing site has no signed-in customer account and does not store Polymarket credentials. The
          desktop app stores those credentials locally on the computer where Delfi is installed.
        </p>

        <h2>2. Analytics and advertising technologies</h2>
        <p>
          For visitors in the EU, EEA, United Kingdom, Switzerland, and locations where the visitor&apos;s country
          cannot be determined, these tools load only after the visitor selects <strong>Accept</strong>. In other
          locations they may load by default unless the visitor disables them below.
        </p>
        <ul>
          <li>
            <strong>Google Analytics 4:</strong> measures visits, sessions, page use, and checkout events. It
            commonly sets <code>_ga</code> and a property-specific <code>_ga_*</code> cookie.
          </li>
          <li>
            <strong>Meta Pixel and Conversions API:</strong> measure visits, checkout starts, and purchases from
            Meta advertising. Browser measurement may set <code>_fbp</code> and <code>_fbc</code>. When allowed,
            server measurement may send a purchase event containing campaign data and hashed customer data.
          </li>
          <li>
            <strong>Microsoft Clarity:</strong> provides heatmaps and session replays. It may set first-party and
            third-party cookies including <code>_clck</code> and <code>_clsk</code>. Form inputs are masked by
            default.
          </li>
          <li>
            <strong>Vercel Speed Insights:</strong> collects sampled web-performance measurements such as LCP,
            CLS, and INP.
          </li>
        </ul>

        <h2>3. Change your choice</h2>
        <ConsentControls />
        <p>
          Disabling analytics prevents these tools from loading on the next page load and removes the known
          first-party analytics cookies Delfi can access. You can also clear all site data in your browser. Your
          browser, extensions, or the providers&apos; own opt-out controls may offer additional choices.
        </p>

        <h2>4. Changes and contact</h2>
        <p>
          We update this page when we change the technologies used on delfibot.com. Questions can be sent to{" "}
          <a href="mailto:info@delfibot.com">info@delfibot.com</a>.
        </p>
      </div>
    </main>
  );
}

function ConsentControls() {
  const [state, setState] = useState<CookieConsent | null | "loading">("loading");

  useEffect(() => {
    setState(readConsent());
    const onChange = () => setState(readConsent());
    window.addEventListener("delfi:consent-changed", onChange);
    return () => window.removeEventListener("delfi:consent-changed", onChange);
  }, []);

  let label = "Loading...";
  if (state === "accepted") label = "Analytics and advertising measurement are enabled.";
  else if (state === "rejected") label = "Analytics and advertising measurement are disabled.";
  else if (state === null) label = "No preference has been saved.";

  const choose = (value: CookieConsent) => {
    saveConsent(value);
    setState(value);
    window.location.reload();
  };

  return (
    <div className="consent-control">
      <p className="consent-state">{label}</p>
      <div className="consent-actions">
        <button
          type="button"
          className="consent-reset"
          onClick={() => choose("accepted")}
          disabled={state === "loading" || state === "accepted"}
        >
          Enable analytics
        </button>
        <button
          type="button"
          className="consent-reset"
          onClick={() => choose("rejected")}
          disabled={state === "loading" || state === "rejected"}
        >
          Disable analytics
        </button>
      </div>
      <p className="consent-hint">
        Your choice applies on this browser. <Link href="/">Back to home.</Link>
      </p>
    </div>
  );
}
