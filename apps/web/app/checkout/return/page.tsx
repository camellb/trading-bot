// apps/web/app/checkout/return/page.tsx
//
// Landing page Stripe redirects to after the embedded checkout
// finishes (success or otherwise). The session_id is in the
// query string; we fetch its status from our server-side proxy
// and show one of three states:
//
//   complete + paid        -> "thanks, license is on the way"
//   open / processing      -> "still processing, refresh in 30s"
//   anything else          -> generic error + support link
//
// Crucially this page is NOT what triggers license issuance.
// The webhook at /api/webhooks/stripe issues the license the
// instant Stripe fires `checkout.session.completed`, regardless
// of whether the buyer ever lands here. This page is only the
// human-facing confirmation.

"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { trackPurchase } from "@/lib/track";
import "../checkout.css";

interface SessionStatus {
  status?:        "complete" | "open" | "expired";
  paymentStatus?: "paid" | "unpaid" | "no_payment_required";
  email?:         string | null;
  amountTotal?:   number | null;
  currency?:      string | null;
  error?:         string;
}

interface LicensePayload {
  licenseId?: string;
  email?:     string;
  blob?:      string;
  error?:     string;
}

function ReturnInner() {
  const params = useSearchParams();
  const sessionId = params.get("session_id");
  const [status, setStatus] = useState<SessionStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [license, setLicense] = useState<LicensePayload | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!sessionId) {
      setLoadError("Missing session id in the return URL.");
      return;
    }
    let cancelled = false;
    fetch(`/api/checkout/session-status?session_id=${encodeURIComponent(sessionId)}`)
      .then(async (res) => {
        const body = (await res.json()) as SessionStatus;
        if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
        if (!cancelled) setStatus(body);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setLoadError(
            e instanceof Error ? e.message : "Could not load order status.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Fetch the actual license blob once we've confirmed the session is
  // paid. The blob lives in Postgres (written by the Stripe webhook
  // or the /api/admin/issue-license backfill); the endpoint
  // re-verifies "paid" against Stripe before exposing it.
  //
  // If the webhook is still in flight on a fresh purchase the
  // endpoint returns 202 "license not issued yet" — poll with a
  // 3-second backoff up to ~30s before giving up. After that the
  // buyer still has the email (delivered by Resend in the same
  // webhook), so this is a convenience, not the only delivery path.
  useEffect(() => {
    if (!sessionId) return;
    if (status?.status !== "complete") return;
    if (status?.paymentStatus !== "paid") return;
    if (license?.blob) return;

    let cancelled = false;
    let attempts = 0;
    const maxAttempts = 10;

    const tick = async () => {
      if (cancelled) return;
      attempts += 1;
      try {
        const res = await fetch(
          `/api/checkout/license-for-session?session_id=${encodeURIComponent(sessionId)}`,
        );
        const body = (await res.json()) as LicensePayload;
        if (!cancelled && res.ok && body.blob) {
          setLicense(body);
          return;
        }
        // 202 = webhook still in flight; back off and retry.
        if (!cancelled && res.status === 202 && attempts < maxAttempts) {
          setTimeout(tick, 3000);
          return;
        }
        if (!cancelled && attempts >= maxAttempts) {
          // Don't surface as an error: the email path still works,
          // and we don't want a transient failure to scare the buyer.
          setLicense({ error: "license-not-ready" });
        }
      } catch {
        if (!cancelled && attempts < maxAttempts) {
          setTimeout(tick, 3000);
        }
      }
    };

    tick();
    return () => {
      cancelled = true;
    };
  }, [sessionId, status, license]);

  const copyLicense = async () => {
    if (!license?.blob) return;
    try {
      await navigator.clipboard.writeText(license.blob);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard API can fail in older browsers / non-HTTPS dev;
      // user can still select the text and copy manually.
    }
  };

  // Fire the Pixel + GA4 `Purchase` event the moment we confirm
  // the session completed and was paid. trackPurchase() has its
  // own once()-keyed-by-sessionId dedup, so a re-render or a
  // refresh of this page won't double-count the conversion.
  useEffect(() => {
    if (!sessionId) return;
    if (status?.status !== "complete") return;
    if (status?.paymentStatus !== "paid") return;
    if (status.amountTotal == null || !status.currency) return;
    trackPurchase({
      eventId:  sessionId,
      value:    status.amountTotal,
      currency: status.currency,
    });
  }, [sessionId, status]);

  if (loadError) {
    return (
      <div className="checkout-return-card">
        <h1 className="checkout-return-title">Something went wrong</h1>
        <p className="checkout-return-body">{loadError}</p>
        <p className="checkout-return-body muted">
          If you were charged, your license is still on the way (the email
          fires from the webhook, not this page). If nothing arrives in 5
          minutes, email{" "}
          <a className="checkout-return-link" href="mailto:info@delfibot.com">
            info@delfibot.com
          </a>{" "}
          and we&apos;ll resend it.
        </p>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="checkout-return-card">
        <div className="checkout-loading" role="status">
          <span className="checkout-spinner" aria-hidden="true" />
          <span className="checkout-loading-text">Confirming payment...</span>
        </div>
      </div>
    );
  }

  if (status.status === "complete" && status.paymentStatus === "paid") {
    return (
      <div className="checkout-return-card">
        <div className="checkout-return-eyebrow">Payment confirmed</div>
        <h1 className="checkout-return-title">Welcome to Delfi.</h1>
        <p className="checkout-return-body">
          Your license key is below and on its way to{" "}
          <span className="checkout-return-email">
            {status.email ?? "the email you entered"}
          </span>
          .
        </p>

        {license?.blob ? (
          <div className="checkout-return-license">
            <div className="checkout-return-license-head">
              <span className="checkout-return-license-label">
                Your license
              </span>
              <button
                type="button"
                className="checkout-return-license-copy"
                onClick={copyLicense}
                aria-label="Copy license key"
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <pre className="checkout-return-license-blob">{license.blob}</pre>
            <p className="checkout-return-body muted">
              Paste this into Delfi on first launch. Same key is in your
              email — keep both safe.
            </p>
          </div>
        ) : (
          <div className="checkout-return-license-pending">
            <span className="checkout-spinner" aria-hidden="true" />
            <span>Loading your license key…</span>
          </div>
        )}

        <p className="checkout-return-body muted">
          macOS &middot; open Terminal and paste:
        </p>
        <code className="checkout-return-cmd">
          curl -fsSL https://delfibot.com/install/mac | bash
        </code>

        <p className="checkout-return-body muted">
          Windows &middot; open PowerShell and paste:
        </p>
        <code className="checkout-return-cmd">
          iwr https://delfibot.com/install/win -UseBasicParsing | iex
        </code>

        <p className="checkout-return-body muted">
          Each command downloads Delfi, installs it, and launches it
          for you.
        </p>

        <p className="checkout-return-body muted">
          Nothing in your inbox in five minutes?
          <br />
          Check spam, then reply to info@delfibot.com and we will resend
          your license.
        </p>
      </div>
    );
  }

  return (
    <div className="checkout-return-card">
      <div className="checkout-return-eyebrow">Still processing</div>
      <h1 className="checkout-return-title">Hang tight.</h1>
      <p className="checkout-return-body">
        Stripe is still confirming your payment. Refresh this page in 30
        seconds; if it still says this, email info@delfibot.com with your
        order email and we&apos;ll sort it.
      </p>
    </div>
  );
}

export default function CheckoutReturnPage() {
  return (
    <main className="checkout-return-page">
      <header className="checkout-header">
        <a href="/" className="checkout-back" aria-label="Back to homepage">
          <span className="checkout-back-arrow">←</span>
          <span className="checkout-back-text">Delfi</span>
        </a>
      </header>
      {/* useSearchParams() must live inside <Suspense> in Next.js 16. */}
      <Suspense
        fallback={
          <div className="checkout-return-card">
            <div className="checkout-loading" role="status">
              <span className="checkout-spinner" aria-hidden="true" />
              <span className="checkout-loading-text">Loading...</span>
            </div>
          </div>
        }
      >
        <ReturnInner />
      </Suspense>
    </main>
  );
}
