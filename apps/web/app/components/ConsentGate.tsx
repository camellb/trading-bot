"use client";

import { useEffect, useState } from "react";
import { SpeedInsights } from "@vercel/speed-insights/next";
import { Analytics } from "@/lib/analytics";
import { readConsent, type CookieConsent } from "./CookieBanner";

// In consent-required regions, trackers mount only after acceptance.
// Elsewhere they mount by default unless the visitor has explicitly
// disabled them on the Cookies Policy page.

export function ConsentGate({
  consentRequired,
}: {
  /** Server-rendered geo decision. */
  consentRequired: boolean;
}) {
  const [consent, setConsent] = useState<CookieConsent | null | "loading">("loading");

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
  }, [consentRequired]);

  if (consent === "loading") return null;
  const trackingAllowed = consent === "accepted" || (
    !consentRequired && consent !== "rejected"
  );
  if (!trackingAllowed) return null;

  return (
    <>
      <SpeedInsights />
      <Analytics />
    </>
  );
}
