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
import "../checkout.css";

interface SessionStatus {
  status?:        "complete" | "open" | "expired";
  paymentStatus?: "paid" | "unpaid" | "no_payment_required";
  email?:         string | null;
  error?:         string;
}

function ReturnInner() {
  const params = useSearchParams();
  const sessionId = params.get("session_id");
  const [status, setStatus] = useState<SessionStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

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
          Your license is on its way to{" "}
          <span className="checkout-return-email">
            {status.email ?? "the email you entered"}
          </span>
          . The email contains the license key and the macOS / Windows
          download links. Paste the key on first launch and you&apos;re live.
        </p>
        <p className="checkout-return-body muted">
          Nothing in your inbox in five minutes? Check spam, then reply to
          info@delfibot.com and we&apos;ll resend.
        </p>
        <a className="btn-primary" href="/">Back to delfibot.com</a>
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
