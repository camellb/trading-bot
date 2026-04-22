"use client";

import React, { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { completeTour } from "./actions";

type Step = {
  title: string;
  render: () => React.ReactNode;
  primaryLabel?: string;
  onPrimary?: (ctx: { router: ReturnType<typeof useRouter> }) => void | Promise<void>;
};

export function Tour() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [pending, startTransition] = useTransition();
  const [dismissed, setDismissed] = useState(false);

  const steps: Step[] = [
    {
      title: "Welcome to Delfi",
      render: () => (
        <p className="tour-body">
          Prediction markets turn the world's unanswered questions into tradable contracts.
          Delfi reads them like puzzles — forecasting every outcome, sizing a calm stake,
          and learning from every resolution. You're about to watch an oracle at work.
        </p>
      ),
    },
    {
      title: "Simulation or Live — your call",
      render: () => (
        <>
          <p className="tour-body">
            Delfi always runs one of two ways. Start where you're comfortable; switch when you're ready.
          </p>
          <div className="tour-vs">
            <div className="tour-vs-col sim">
              <div className="tour-vs-head">Simulation</div>
              <div className="tour-vs-body">
                Paper capital. Same signals, same decisions, no real money at risk.
                Perfect for watching Delfi think before you commit a dollar.
              </div>
            </div>
            <div className="tour-vs-col live">
              <div className="tour-vs-head">Live</div>
              <div className="tour-vs-body">
                Real capital from your connected Polymarket wallet.
                Every position opens for real.
              </div>
            </div>
          </div>
        </>
      ),
    },
    {
      title: "Risk controls keep you safe",
      render: () => (
        <p className="tour-body">
          Delfi runs with circuit breakers that cap daily losses, halt on drawdown,
          cool down after a losing streak, and reserve dry powder. You can tune
          every parameter inside the system's safe bounds.
        </p>
      ),
      primaryLabel: "Go to Risk Controls →",
      onPrimary: ({ router }) => {
        router.push("/dashboard/risk");
      },
    },
    {
      title: "Tune the limits, respect the sample size",
      render: () => (
        <p className="tour-body">
          These settings shape Delfi's behavior the moment you save them. But remember:
          Delfi is an algorithmic forecaster. Meaningful conclusions need 50–100 closed
          trades before you adjust strategy based on results. Patience is part of the edge.
        </p>
      ),
    },
    {
      title: "Delfi reviews itself",
      render: () => (
        <p className="tour-body">
          Every 50 closed trades Delfi runs a full analysis pass on its own performance.
          Category ROI, calibration, skip-list candidates — all proposed as deliberate
          changes with evidence. You'll find every review under{" "}
          <strong>Intelligence</strong>, stamped with the date and its supporting data.
        </p>
      ),
    },
    {
      title: "Telegram keeps you in the loop",
      render: () => (
        <p className="tour-body">
          Connect a Telegram bot and you'll receive every trade, every resolution, and
          every strategy review right where you are. Setup lives in{" "}
          <strong>Settings → Notifications</strong> whenever you're ready.
        </p>
      ),
    },
    {
      title: "When you go Live: connect Polymarket",
      render: () => (
        <p className="tour-body">
          Live mode needs your Polymarket proxy wallet address and an API key.
          We only ever send orders on your behalf — we never custody funds, and we
          can never read anything outside of what you trade through Delfi. Your wallet
          remains yours, end to end.
        </p>
      ),
    },
  ];

  if (dismissed) return null;

  const current = steps[step];
  const isLast = step === steps.length - 1;

  const advance = () => setStep((s) => Math.min(s + 1, steps.length - 1));
  const back = () => setStep((s) => Math.max(s - 1, 0));

  const finish = () => {
    setDismissed(true);
    startTransition(async () => {
      await completeTour();
    });
  };

  const onPrimary = () => {
    if (isLast) return finish();
    if (current.onPrimary) {
      void current.onPrimary({ router });
      advance();
      return;
    }
    advance();
  };

  const skip = () => finish();

  const primaryLabel = isLast
    ? pending
      ? "Finishing..."
      : "Start exploring →"
    : current.primaryLabel ?? "Next →";

  return (
    <div className="tour-overlay" role="dialog" aria-modal="true" aria-label="Delfi product tour">
      <div className="tour-card">
        <div className="tour-step">
          Step {step + 1} of {steps.length}
        </div>
        <h2 className="tour-title">{current.title}</h2>
        {current.render()}
        <div className="tour-foot">
          <div className="tour-dots" aria-hidden="true">
            {steps.map((_, i) => (
              <span key={i} className={`tour-dot ${i === step ? "active" : ""}`} />
            ))}
          </div>
          <div className="tour-actions">
            {step > 0 && !isLast && (
              <button className="tour-btn ghost" onClick={back}>
                Back
              </button>
            )}
            {!isLast && (
              <button className="tour-btn ghost" onClick={skip} disabled={pending}>
                Skip tour
              </button>
            )}
            <button
              className="tour-btn primary"
              onClick={onPrimary}
              disabled={pending}
            >
              {primaryLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
