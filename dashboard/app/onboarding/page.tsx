"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import "../styles/content.css";

type Step = 1 | 2 | 3 | 4;
type Mode = "simulation" | "live";
type RiskProfile = "cautious" | "balanced" | "aggressive";

export default function OnboardingPage() {
  const router = useRouter();
  const [step, setStep] = useState<Step>(1);
  const [mode, setMode] = useState<Mode>("simulation");
  const [capital, setCapital] = useState<number>(1000);
  const [riskProfile, setRiskProfile] = useState<RiskProfile>("balanced");
  const [notify, setNotify] = useState<boolean>(true);

  const skipCapital = mode === "live";
  const totalSteps = skipCapital ? 3 : 4;
  const visibleIndex =
    step === 1 ? 1 : skipCapital ? step - 1 : step;
  const pad = (n: number) => String(n).padStart(2, "0");

  const next = () =>
    setStep((s) => {
      if (s === 1 && skipCapital) return 3;
      return s < 4 ? ((s + 1) as Step) : s;
    });
  const back = () =>
    setStep((s) => {
      if (s === 3 && skipCapital) return 1;
      return s > 1 ? ((s - 1) as Step) : s;
    });
  const finish = () => router.push("/dashboard");

  return (
    <div className="ob-page">
      <header className="content-nav">
        <Link href="/" className="wordmark">
          <img src="/brand/mark.svg" alt="" />
          <span>DELFI</span>
        </Link>
        <div className="ob-progress">
          {Array.from({ length: totalSteps }, (_, i) => i + 1).map((i) => (
            <span key={i} className={visibleIndex >= i ? "ob-step active" : "ob-step"}>
              {pad(i)}
            </span>
          ))}
        </div>
      </header>

      <main className="ob-main">
        {step === 1 && (
          <section>
            <div className="ob-eyebrow">Step {pad(visibleIndex)} of {pad(totalSteps)}</div>
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

        {step === 2 && (
          <section>
            <div className="ob-eyebrow">Step {pad(visibleIndex)} of {pad(totalSteps)}</div>
            <h1 className="ob-title">Paper bankroll</h1>
            <p className="ob-sub">
              This is the pretend balance Delfi will size positions against in Simulation. No real
              money is ever at stake — it's a sandbox for watching the system trade. You can adjust
              or reset it any time from Settings.
            </p>

            <div className="ob-form">
              <label className="ob-field">
                <span className="ob-field-label">Amount (USD)</span>
                <input
                  type="number"
                  className="ob-input"
                  value={capital}
                  onChange={(e) => setCapital(Number(e.target.value) || 0)}
                  min={100}
                  step={100}
                />
              </label>

              <div className="ob-quick">
                {[500, 1000, 5000, 10000].map((v) => (
                  <button
                    key={v}
                    className={`ob-quick-btn ${capital === v ? "selected" : ""}`}
                    onClick={() => setCapital(v)}
                  >
                    ${v.toLocaleString()}
                  </button>
                ))}
              </div>
            </div>
          </section>
        )}

        {step === 3 && (
          <section>
            <div className="ob-eyebrow">Step {pad(visibleIndex)} of {pad(totalSteps)}</div>
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
            <div className="ob-eyebrow">Step {pad(visibleIndex)} of {pad(totalSteps)}</div>
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
              <button className="ob-next" onClick={next}>
                Continue →
              </button>
            ) : (
              <button className="ob-next" onClick={finish}>
                {mode === "live" ? "Connect wallet →" : "Enter dashboard →"}
              </button>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
