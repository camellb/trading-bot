"use client";

import { useEffect, useState } from "react";
import { SpeedInsights } from "@vercel/speed-insights/next";
import { Analytics } from "@/lib/analytics";
import { readConsent, type CookieConsent } from "./CookieBanner";

// Renders the analytics scripts under one of two conditions:
//
//   * `consentRequired === false` (visitor is outside the EU/EEA/UK/CH
//     consent regime): mount immediately. No banner shows for these
//     visitors and we collect analytics by default, mirroring how
//     Stripe, Vercel, Cloudflare, and most B2B SaaS handle it.
//
//   * `consentRequired === true` AND the visitor has clicked "Accept"
//     on the cookie banner. Trackers start firing on the same render
//     pass that follows the click, via the "delfi:consent-changed"
//     custom event the CookieBanner dispatches.
//
// `consentRequired` is decided server-side in app/layout.tsx using
// the Vercel edge geo header `x-vercel-ip-country`. When that header
// is missing (local dev, custom proxies) we default to true so the
// banner still shows.
//
// Wrapping all providers behind one gate (rather than gating each
// individually) keeps layout.tsx clean and means a future provider
// added to the Analytics component automatically inherits the
// consent guard.

export function ConsentGate({
  consentRequired,
}: {
  /** Server-rendered geo decision. False = visitor is outside the
   *  consent regime; mount analytics unconditionally. */
  consentRequired: boolean;
}) {
  const [consent, setConsent] = useState<CookieConsent | null>(null);

  useEffect(() => {
    // No banner for this visitor; localStorage state is irrelevant
    // and we don't need to listen for changes.
    if (!consentRequired) return;
    setConsent(readConsent());
    const onChange = () => setConsent(readConsent());
    const onStorage = (e: StorageEvent) => {
      if (e.key === "delfi.cookie-consent") setConsent(readConsent());
    };
    window.addEventListener("delfi:consent-changed", onChange);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("delfi:consent-changed", onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, [consentRequired]);

  // Outside the consent regime: mount unconditionally.
  if (!consentRequired) {
    return (
      <>
        <SpeedInsights />
        <Analytics />
      </>
    );
  }

  // In the consent regime: only mount after explicit accept.
  if (consent !== "accepted") return null;

  return (
    <>
      <SpeedInsights />
      <Analytics />
    </>
  );
}
