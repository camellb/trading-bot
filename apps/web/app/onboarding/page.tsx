"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import "../styles/content.css";

import { completeOnboarding } from "./actions";

type Step = 1 | 2 | 3 | 4;
type Mode = "simulation" | "live";
type RiskProfile = "cautious" | "balanced" | "aggressive";

function OnboardingErrorBanner() {
  const params = useSearchParams();
  if (!params || params.get("error") !== "save_failed") return null;
  const code = params.get("code") || "";
  const message = params.get("message") || "Couldn't save. Try again.";
  return (
    <div className="creds-banner" role="alert" style={{ marginBottom: 24 }}>
      <div className="creds-banner-body">
        <span className="creds-banner-dot" aria-hidden="true"></span>
        <div>
          <div className="creds-banner-title">Couldn't complete onboarding</div>
          <div className="creds-banner-text">
            {message}{code ? ` (${code})` : ""}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function OnboardingPage() {
  const [step, setStep] = useState<Step>(1);
  const [name, setName] = useState<string>("");
  const [mode, setMode] = useState<Mode>("simulation");
  const [riskProfile, setRiskProfile] = useState<RiskProfile>("balanced");
  const [notify, setNotify] = useState<boolean>(true);

  // Step 1 = name, 2 = mode, 3 = risk, 4 = notify. Bankroll defaults to
  // $1,000 for simulation; live mode uses the real wallet balance.
  const totalSteps = 4;
  const pad = (n: number) => String(n).padStart(2, "0");

  const canContinueFromName = name.trim().length >= 2;

  const next = () =>
    setStep((s) => (s < 4 ? ((s + 1) as Step) : s));
  const back = () =>
    setStep((s) => (s > 1 ? ((s - 1) as Step) : s));

  return (
    <div className="ob-page">
      <header className="content-nav">
        <Link href="/" className="wordmark">
          <img src="/brand/mark.svg" alt="" />
          <span>DELFI</span>
        </Link>
      </header>

      <main className="ob-main">
        <Suspense fallback={null}>
          <OnboardingErrorBanner />
        </Suspense>
        {step === 1 && (
          <section>
            <div className="ob-eyebrow">Step {pad(step)} of {pad(totalSteps)}</div>
            <h1 className="ob-title">What should Delfi call you?</h1>
            <p className="ob-sub">
              This is how we'll greet you on the dashboard and in weekly review emails. You can change
              it later from Settings.
            </p>

            <div className="ob-form">
              <label className="ob-field">
                <span className="ob-field-label">Your name</span>
                <input
                  type="text"
                  className="ob-input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="First and last name"
                  autoFocus
                />
              </label>
            </div>
          </section>
        )}

        {step === 2 && (
          <section>
            <div className="ob-eyebrow">Step {pad(step)} of {pad(totalSteps)}</div>
            <h1 className="ob-title">How would you like to start?</h1>
            <p className="ob-sub">
              You can always switch modes later from your dashboard. Most users begin in Simulation to build
              trust in the system before deploying real capital.
            </p>

            <div className="ob-choices">
              <button
                className={`ob-choice ${mode === "simulation" ? "selected" : ""}`}
                onClick={() => setMode("simulation")}
              >
                <div className="ob-choice-head">
                  <div className="ob-choice-title">Start in Simulation</div>
                  <div className="ob-choice-badge">Recommended</div>
                </div>
                <div className="ob-choice-body">
                  Delfi trades with paper capital against the real live market. Same decisions, same
                  reasoning, no real money at stake. Graduate to live trading whenever you're ready.
                </div>
              </button>

              <button
                className={`ob-choice ${mode === "live" ? "selected" : ""}`}
                onClick={() => setMode("live")}
              >
                <div className="ob-choice-head">
                  <div className="ob-choice-title">Go live immediately</div>
                </div>
                <div className="ob-choice-body">
                  Connect your wallet and deploy real capital. Delfi trades Polymarket on your behalf. Daily
                  and drawdown caps remain active from the first trade.
                </div>
              </button>
            </div>
          </section>
        )}

        {step === 3 && (
          <section>
            <div className="ob-eyebrow">Step {pad(step)} of {pad(totalSteps)}</div>
            <h1 className="ob-title">Risk profile</h1>
            <p className="ob-sub">
              Pick a starting profile. Every value is adjustable later from the Risk Controls page. These
              profiles set the daily loss cap, per-trade ceiling, and drawdown halt threshold.
            </p>

            <div className="ob-choices">
              <button
                className={`ob-choice ${riskProfile === "cautious" ? "selected" : ""}`}
                onClick={() => setRiskProfile("cautious")}
              >
                <div className="ob-choice-head">
                  <div className="ob-choice-title">Cautious</div>
                  <div className="ob-choice-meta">5% daily cap · 2% max per trade</div>
                </div>
                <div className="ob-choice-body">
                  Conservative. Daily losses halt trading at 5% of bankroll. Good for users who want slow,
                  steady exposure while Delfi proves itself.
                </div>
              </button>

              <button
                className={`ob-choice ${riskProfile === "balanced" ? "selected" : ""}`}
                onClick={() => setRiskProfile("balanced")}
              >
                <div className="ob-choice-head">
                  <div className="ob-choice-title">Balanced</div>
                  <div className="ob-choice-badge">Default</div>
                  <div className="ob-choice-meta">10% daily cap · 3% max per trade</div>
                </div>
                <div className="ob-choice-body">
                  The recommended starting point. Enough headroom for Delfi to express its forecasts while
                  protecting the bankroll from rough days.
                </div>
              </button>

              <button
                className={`ob-choice ${riskProfile === "aggressive" ? "selected" : ""}`}
                onClick={() => setRiskProfile("aggressive")}
              >
                <div className="ob-choice-head">
                  <div className="ob-choice-title">Aggressive</div>
                  <div className="ob-choice-meta">20% daily cap · 5% max per trade</div>
                </div>
                <div className="ob-choice-body">
                  Higher variance. Delfi will take larger positions and tolerate deeper daily swings. Only
                  choose this if you've traded prediction markets before and understand the volatility.
                </div>
              </button>
            </div>
          </section>
        )}

        {step === 4 && (
          <section>
            <div className="ob-eyebrow">Step {pad(step)} of {pad(totalSteps)}</div>
            <h1 className="ob-title">Notifications</h1>
            <p className="ob-sub">
              Delfi can email you when something meaningful happens. You'll still see everything on the
              dashboard regardless.
            </p>

            <div className="ob-choices">
              <button
                className={`ob-choice ${notify ? "selected" : ""}`}
                onClick={() => setNotify(true)}
              >
                <div className="ob-choice-head">
                  <div className="ob-choice-title">Daily digest and weekly review</div>
                  <div className="ob-choice-badge">Recommended</div>
                </div>
                <div className="ob-choice-body">
                  A short morning summary of yesterday's trades and a weekly performance review. You can
                  unsubscribe any time.
                </div>
              </button>

              <button
                className={`ob-choice ${!notify ? "selected" : ""}`}
                onClick={() => setNotify(false)}
              >
                <div className="ob-choice-head">
                  <div className="ob-choice-title">No email</div>
                </div>
                <div className="ob-choice-body">
                  Delfi won't send you anything. You'll only see activity when you open the dashboard.
                </div>
              </button>
            </div>

            <div className="ob-legal">
              By entering the dashboard you acknowledge that you have read our{" "}
              <Link href="/legal/risk">Risk Disclosure</Link> and agree to the{" "}
              <Link href="/legal/terms">Terms of Service</Link>.
            </div>
          </section>
        )}

        <div className="ob-actions">
          <div className="ob-actions-left">
            {step > 1 ? (
              <button className="ob-back" onClick={back}>
                ← Back
              </button>
            ) : (
              <Link href="/" className="ob-back">
                Cancel
              </Link>
            )}
          </div>
          <div className="ob-actions-right">
            {step < 4 ? (
              <button
                className="ob-next"
                onClick={next}
                disabled={step === 1 && !canContinueFromName}
                style={step === 1 && !canContinueFromName ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
              >
                Continue →
              </button>
            ) : (
              <form action={completeOnboarding} style={{ display: "inline" }}>
                <input type="hidden" name="display_name" value={name.trim()} />
                <input type="hidden" name="mode" value={mode} />
                <input type="hidden" name="risk_profile" value={riskProfile} />
                <button type="submit" className="ob-next" disabled={!canContinueFromName}>
                  {mode === "live" ? "Connect wallet →" : "Enter dashboard →"}
                </button>
              </form>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
