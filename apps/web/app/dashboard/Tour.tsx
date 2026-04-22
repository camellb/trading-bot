"use client";

import React, { useState, useTransition } from "react";
import { completeTour } from "./actions";

type Step = {
  title: string;
  render: () => React.ReactNode;
};

export function Tour() {
  const [step, setStep] = useState(0);
  const [pending, startTransition] = useTransition();
  const [dismissed, setDismissed] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const steps: Step[] = [
    {
      title: "Welcome to Delfi",
      render: () => (
        <>
          <p className="tour-body">
            Delfi is an autonomous AI agent for Polymarket. It reads every active
            market, sizes the positions worth taking, and executes on your behalf
            around the clock.
          </p>
          <p className="tour-body">
            You're about to set it up in under three minutes.
          </p>
        </>
      ),
    },
    {
      title: "You start in Simulation",
      render: () => (
        <>
          <p className="tour-body">
            Delfi runs two ways: Simulation and Live. Either way you are playing
            on the real market with the same signals.
          </p>
          <p className="tour-body">
            Watch Delfi trade for a day or a week. Go Live when the numbers
            convince you.
          </p>
        </>
      ),
    },
    {
      title: "Set your risk limits",
      render: () => (
        <>
          <p className="tour-body">
            Delfi has circuit breakers that cap daily and weekly losses, halt
            trading on drawdown, cool off after a losing streak, and reserve dry
            powder — and more. You can tune every parameter now, or adjust as you
            go.
          </p>
          <p className="tour-body">
            These settings shape Delfi's behavior the moment you save them, but
            remember that meaningful conclusions need 50 to 100 closed trades
            before you adjust strategy based on results.
          </p>
        </>
      ),
    },
    {
      title: "Delfi is always learning",
      render: () => (
        <>
          <p className="tour-body">
            Speaking of which, every 50 closed trades, Delfi runs a full analysis
            pass on its own performance and proposes adjustments.
          </p>
          <p className="tour-body">
            You'll find every review under <strong>Intelligence</strong>,
            stamped with the date and its supporting data.
          </p>
        </>
      ),
    },
    {
      title: "Get alerts on Telegram",
      render: () => (
        <>
          <p className="tour-body">
            Delfi sends every new position, every resolution, and daily &
            weekly summaries straight to your Telegram.
          </p>
          <p className="tour-body">
            You can connect it under <strong>Settings → Account</strong>.
          </p>
        </>
      ),
    },
    {
      title: "Ready to go Live? Connect Polymarket",
      render: () => (
        <>
          <p className="tour-body">
            Live trading needs your Polymarket proxy wallet address and an API
            key. Delfi can send orders on your behalf. It cannot custody funds,
            withdraw, or read anything outside the markets you trade.
          </p>
          <p className="tour-body">
            Your wallet stays yours.
          </p>
        </>
      ),
    },
  ];

  if (dismissed) return null;

  const current = steps[step];
  const isLast = step === steps.length - 1;
  const isFirst = step === 0;

  const advance = () => setStep((s) => Math.min(s + 1, steps.length - 1));
  const back = () => setStep((s) => Math.max(s - 1, 0));

  const finish = () => {
    setErr(null);
    startTransition(async () => {
      const res = await completeTour();
      if (res.ok) {
        setDismissed(true);
      } else {
        setErr(res.error || "Could not save. Try again.");
      }
    });
  };

  const onPrimary = () => {
    if (isLast) return finish();
    advance();
  };

  const primaryLabel = isLast
    ? pending
      ? "Saving..."
      : "Start exploring →"
    : "Next →";

  return (
    <div className="tour-overlay" role="dialog" aria-modal="true" aria-label="Delfi product tour">
      <div className="tour-card">
        <div className="tour-step">
          Step {step + 1} of {steps.length}
        </div>
        <h2 className="tour-title">{current.title}</h2>
        {current.render()}
        {err && <div className="tour-err">{err}</div>}
        <div className="tour-foot">
          <div className="tour-dots" aria-hidden="true">
            {steps.map((_, i) => (
              <span key={i} className={`tour-dot ${i === step ? "active" : ""}`} />
            ))}
          </div>
          <div className="tour-actions">
            {!isFirst && (
              <button className="tour-btn ghost" onClick={back} disabled={pending}>
                Back
              </button>
            )}
            {!isLast && (
              <button className="tour-btn ghost" onClick={finish} disabled={pending}>
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
