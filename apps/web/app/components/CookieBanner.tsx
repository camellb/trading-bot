"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

// First-visit cookie consent banner.
//
// Shows at bottom of viewport when no consent has been recorded.
// Two choices: "Accept" (full analytics) or "Reject" (no
// non-essential cookies). Choice persists in localStorage; banner
// stays hidden on subsequent visits unless the user clears storage.
//
// Pairs with <ConsentGate> which actually gates the Analytics +
// SpeedInsights components on the consent value. The banner only
// reads/writes the localStorage key; it doesn't render the analytics
// itself.
//
// To revisit / change choice, clear localStorage on the cookies
// policy page (the "review your choice" button there nukes the key
// and the banner reappears on next render).

const STORAGE_KEY = "delfi.cookie-consent";

export type CookieConsent = "accepted" | "rejected";

/** Read consent from localStorage. Returns null if not yet decided
 *  or if running on the server. */
export function readConsent(): CookieConsent | null {
  if (typeof window === "undefined") return null;
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (v === "accepted" || v === "rejected") return v;
  } catch {
    // localStorage can throw in private mode / sandboxed iframes;
    // treat as "not yet decided" so the banner shows.
  }
  return null;
}

/** Clear consent so the banner reappears. Used by the cookies policy
 *  page's "review choice" button. */
export function clearConsent(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
    window.dispatchEvent(new CustomEvent("delfi:consent-changed"));
  } catch {
    // best effort
  }
}

export function CookieBanner() {
  // `"loading"` until the first effect runs, so we don't flash the
  // banner during hydration on a returning visitor (the server can't
  // see localStorage).
  const [consent, setConsent] = useState<CookieConsent | null | "loading">(
    "loading",
  );

  useEffect(() => {
    setConsent(readConsent());
    // Cross-tab sync: banner reappears in this tab if consent is
    // cleared elsewhere (e.g. the cookies policy page in another
    // tab).
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setConsent(readConsent());
    };
    // Same-tab sync via custom event (StorageEvent only fires on
    // OTHER tabs, not the one that wrote the change).
    const onLocal = () => setConsent(readConsent());
    window.addEventListener("storage", onStorage);
    window.addEventListener("delfi:consent-changed", onLocal);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("delfi:consent-changed", onLocal);
    };
  }, []);

  // Hide while resolving + once a choice has been made.
  if (consent === "loading" || consent !== null) return null;

  const choose = (value: CookieConsent) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, value);
    } catch {
      // ignore, user will see the banner again next visit
    }
    setConsent(value);
    window.dispatchEvent(new CustomEvent("delfi:consent-changed"));
  };

  return (
    <div className="cookie-banner" role="dialog" aria-label="Cookie consent">
      <div className="cookie-banner-inner">
        <p className="cookie-banner-body">
          We use a small number of analytics cookies to understand how
          delfibot.com is used and to improve it. Necessary cookies for
          the site to function are always on.{" "}
          <Link href="/legal/cookies">What we use, in detail.</Link>
        </p>
        <div className="cookie-banner-buttons">
          <button
            type="button"
            className="cookie-banner-btn ghost"
            onClick={() => choose("rejected")}
          >
            Reject
          </button>
          <button
            type="button"
            className="cookie-banner-btn primary"
            onClick={() => choose("accepted")}
          >
            Accept
          </button>
        </div>
      </div>
    </div>
  );
}
