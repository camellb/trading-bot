"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import "../styles/content.css";
import "./subscribe.css";

import { startCheckout, type SubscriptionPlan } from "./actions";

function SubscribeErrorBanner() {
  const params = useSearchParams();
  const err = params?.get("error");
  if (!err) return null;
  const message =
    err === "invalid_plan"
      ? "Please pick a plan before continuing."
      : err === "save_failed"
      ? params.get("message") || "Couldn't save. Try again."
      : "Something went wrong. Try again.";
  const code = params.get("code") || "";
  return (
    <div className="creds-banner" role="alert" style={{ marginBottom: 24 }}>
      <div className="creds-banner-body">
        <span className="creds-banner-dot" aria-hidden="true"></span>
        <div>
          <div className="creds-banner-title">Couldn&apos;t start checkout</div>
          <div className="creds-banner-text">
            {message}{code ? ` (${code})` : ""}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function SubscribePage() {
  const [plan, setPlan] = useState<SubscriptionPlan>("annual");

  return (
    <div className="ob-page">
      <header className="content-nav">
        <Link href="/" className="wordmark">
          <img src="/brand/mark.svg" alt="" />
          <span>DELFI</span>
        </Link>
      </header>

      <main className="ob-main sub-main">
        <Suspense fallback={null}>
          <SubscribeErrorBanner />
        </Suspense>

        <div className="ob-eyebrow">Subscribe</div>
        <h1 className="ob-title sub-title">Pick your plan</h1>
        <p className="ob-sub sub-sub">
          Delfi forecasts every tradeable Polymarket market, sizes positions with risk brakes, and
          narrates every decision on your dashboard. One plan, billed monthly or yearly. Cancel any
          time.
        </p>

        <div className="ob-choices sub-grid">
          <button
            type="button"
            className={`ob-choice sub-plan ${plan === "annual" ? "selected" : ""}`}
            onClick={() => setPlan("annual")}
            aria-pressed={plan === "annual"}
          >
            <div className="ob-choice-head">
              <div className="ob-choice-title">Annual</div>
              <div className="sub-badge">Save 25%</div>
            </div>
            <div className="sub-price">
              <span className="sub-price-num t-num">$52.50</span>
              <span className="sub-price-unit">/ month</span>
            </div>
            <div className="sub-price-meta">Billed $630 yearly.</div>
            <div className="ob-choice-body">
              Best value. Locks in the discount for a full year of Delfi.
            </div>
          </button>

          <button
            type="button"
            className={`ob-choice sub-plan ${plan === "monthly" ? "selected" : ""}`}
            onClick={() => setPlan("monthly")}
            aria-pressed={plan === "monthly"}
          >
            <div className="ob-choice-head">
              <div className="ob-choice-title">Monthly</div>
            </div>
            <div className="sub-price">
              <span className="sub-price-num t-num">$69.99</span>
              <span className="sub-price-unit">/ month</span>
            </div>
            <div className="sub-price-meta">Billed monthly. Cancel any time.</div>
            <div className="ob-choice-body">
              Month to month. No commitment beyond the current cycle.
            </div>
          </button>
        </div>

        <div className="sub-includes">
          <div className="sub-includes-label">Every plan includes</div>
          <ul className="sub-includes-list">
            <li>Autonomous forecasting on every tradeable Polymarket market.</li>
            <li>Full reasoning visible on every trade, live on the dashboard.</li>
            <li>Simulation and live modes, with identical risk brakes.</li>
            <li>Telegram summaries and weekly performance review.</li>
          </ul>
        </div>

        <div className="ob-actions">
          <div className="ob-actions-left">
            <Link href="/" className="ob-back">
              Cancel
            </Link>
          </div>
          <div className="ob-actions-right">
            <form action={startCheckout} style={{ display: "inline" }}>
              <input type="hidden" name="plan" value={plan} />
              <button type="submit" className="ob-next">
                Subscribe →
              </button>
            </form>
          </div>
        </div>

        <p className="sub-legal">
          By subscribing you agree to Delfi&apos;s{" "}
          <Link href="/legal/terms">Terms of Service</Link> and{" "}
          <Link href="/legal/privacy">Privacy Policy</Link>.
        </p>
      </main>
    </div>
  );
}
