"use client";

import { useEffect, useState } from "react";
import { SpeedInsights } from "@vercel/speed-insights/next";
import { Analytics } from "@/lib/analytics";
import { readConsent, type CookieConsent } from "./CookieBanner";

// Renders the analytics scripts only when the visitor has clicked
// "Accept" on the cookie banner. Default state: render nothing.
//
// This is the GDPR-compliant default: third-party trackers (Google
// Analytics, Meta Pixel, Microsoft Clarity, Vercel SpeedInsights)
// stay dormant on first visit. They start firing on the same render
// pass that follows the user's accept click, via the
// "delfi:consent-changed" custom event the CookieBanner dispatches.
//
// Wrapping both providers behind one gate (rather than gating each
// individually) keeps layout.tsx clean and means a future provider
// added to the Analytics component automatically inherits the
// consent guard.

export function ConsentGate() {
  const [consent, setConsent] = useState<CookieConsent | null>(null);

  useEffect(() => {
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
  }, []);

  if (consent !== "accepted") return null;

  return (
    <>
      <SpeedInsights />
      <Analytics />
    </>
  );
}
