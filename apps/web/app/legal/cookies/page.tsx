"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { clearConsent, readConsent, type CookieConsent } from "../../components/CookieBanner";
import "../../styles/content.css";

// Cookies policy page. Mounted at /legal/cookies.
//
// Static legal copy on the left, plus a small interactive panel that
// shows the visitor's current consent state and lets them clear it
// to bring the banner back on the next page render. The panel is the
// reason this page is "use client" rather than a plain server
// component like terms / privacy / risk.

export default function CookiesPage() {
  return (
    <main className="content-main">
      <div className="content-eyebrow">Legal</div>
      <h1 className="content-h1">Cookies Policy</h1>
      <p className="content-lede">
        We try to set the smallest number of cookies we can while still
        running a working site. This page explains what we use and how
        you can change your choice.
      </p>
      <div className="content-meta">Effective 2026-04-01 &middot; Last updated 2026-05-04</div>

      <div className="content-body">
        <h2>1. What is a cookie</h2>
        <p>
          A cookie is a small file a website asks your browser to store. The next time you visit, your browser
          sends the file back so the site can remember things like that you&apos;re signed in, your preference
          for dark mode, or whether you&apos;ve seen a banner before. Some cookies are strictly necessary;
          others are used for analytics or marketing.
        </p>

        <h2>2. Cookies we use</h2>
        <p>We split cookies into two buckets, mirroring how the cookie banner works:</p>

        <h3>Necessary (always on)</h3>
        <ul>
          <li>
            <strong>Cookie-consent flag</strong> in your browser&apos;s local storage under the key{" "}
            <code>delfi.cookie-consent</code>. Records whether you accepted or rejected the analytics
            bucket. Without this we would re-prompt you on every visit. This value never leaves your
            browser.
          </li>
        </ul>
        <p>
          We have no signed-in marketing-site account, so there are no authentication cookies on
          delfibot.com. Your Polymarket credentials live exclusively in your operating system&apos;s
          keychain on the machine where you installed Delfi; they are not cookies, they are not stored on
          our servers, and they never travel through the website.
        </p>

        <h3>Analytics (only if you accept)</h3>
        <p>
          The following load only after you click <strong>Accept</strong> on the banner. They help us see
          which pages people read and which links they click, so we can fix what doesn&apos;t work.
        </p>
        <ul>
          <li>
            <strong>Google Analytics 4</strong>. Aggregate page views and outbound link clicks. Provides us
            with traffic data only.
          </li>
          <li>
            <strong>Meta Pixel</strong>. Page-view tracking that lets us measure ads on Meta platforms if and
            when we run them.
          </li>
          <li>
            <strong>Microsoft Clarity</strong>. Anonymised heatmaps and session replays so we can see where
            visitors get stuck. Clarity masks form inputs by default.
          </li>
          <li>
            <strong>Vercel Speed Insights</strong>. Web-vital measurements (LCP, CLS, INP) attributed by
            page. Sampled and aggregated; no individual user identifiers.
          </li>
        </ul>

        <h2>3. We do NOT use</h2>
        <ul>
          <li>Advertising cookies that follow you across the web.</li>
          <li>Affiliate-tracking cookies.</li>
          <li>Any cookie tied to your trading data. Your trading happens on your computer; we do not see it.</li>
        </ul>

        <h2>4. Change your mind</h2>
        <ConsentControls />

        <h2>5. Browser-level controls</h2>
        <p>
          Every modern browser lets you clear stored cookies and local storage from its settings. Doing so
          will reset your consent choice and the banner will reappear on the next visit. Most browsers also
          offer a setting that blocks all third-party cookies; the analytics tools listed above respect that
          setting and will not load.
        </p>

        <h2>6. Changes to this policy</h2>
        <p>
          If we add a tool that sets new cookies we will list it here and the next visit will prompt you for
          consent again. Material changes will also be reflected on the homepage footer.
        </p>

        <h2>7. Contact</h2>
        <p>
          Questions can be sent to{" "}
          <a href="mailto:info@delfibot.com">info@delfibot.com</a>.
        </p>
      </div>
    </main>
  );
}

// ── Consent state + reset control ───────────────────────────────────

function ConsentControls() {
  const [state, setState] = useState<CookieConsent | null | "loading">("loading");

  useEffect(() => {
    setState(readConsent());
    const onChange = () => setState(readConsent());
    window.addEventListener("delfi:consent-changed", onChange);
    return () => window.removeEventListener("delfi:consent-changed", onChange);
  }, []);

  let label = "Loading...";
  if (state === "accepted") label = "You have accepted analytics cookies.";
  else if (state === "rejected") label = "You have rejected analytics cookies.";
  else if (state === null) label = "You have not made a choice yet.";

  return (
    <div className="consent-control">
      <p className="consent-state">{label}</p>
      <button
        type="button"
        className="consent-reset"
        onClick={() => {
          clearConsent();
          setState(null);
        }}
        disabled={state === "loading" || state === null}
      >
        Reset my choice
      </button>
      <p className="consent-hint">
        Resetting clears your stored choice. The banner will appear again on your next page load so you can
        choose differently.{" "}
        <Link href="/">Back to home.</Link>
      </p>
    </div>
  );
}
