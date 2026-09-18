import { useEffect, useState } from "react";
import { api, BotState, Credentials } from "./api";

/**
 * First-launch onboarding wizard.
 *
 * Shown when /api/state reports is_onboarded === false. Two screens:
 *   1. Welcome / mode overview (intro)
 *   2. Connect Polymarket (optional)
 *
 * The wizard finishes from the Polymarket step itself - either "Save
 * and continue" (creds entered) or "Skip for now" (going to
 * simulation). Both paths stamp `tour_completed_at` server-side and
 * call onComplete to refresh App state, which un-mounts the wizard.
 *
 * The previous "Delfi is ready / Open the dashboard" screen was
 * removed 2026-05-28: it was the only place tour_completed_at got
 * written, so any user who closed the GUI before clicking that
 * button re-saw the wizard on next launch. By moving the write into
 * the Polymarket step's exit buttons there's no escape path that
 * leaves the wizard half-finished.
 *
 * Starting capital and the LLM key are NOT collected here. The DB
 * defaults the bankroll to $1,000 (editable later in Settings >
 * Risk) and the forecaster runs against whichever LLM key the user
 * sets in Settings > Connections. Keeping the wizard short improves
 * first-launch conversion.
 */

type Step = "welcome" | "polymarket";

const ORDER: Step[] = ["welcome", "polymarket"];

interface Props {
  state: BotState | null;
  creds: Credentials | null;
  onComplete: () => void;
}

export default function Onboarding({ creds, onComplete }: Props) {
  const [step, setStep] = useState<Step>("welcome");
  const [polymarketKey, setPolymarketKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const idx = ORDER.indexOf(step);
  const next = () => setStep(ORDER[Math.min(ORDER.length - 1, idx + 1)]);
  const back = () => setStep(ORDER[Math.max(0, idx - 1)]);

  // The ONE place tour_completed_at gets written, fired from both
  // "Save and continue" (after the creds save) and "Skip for now".
  // Errors surface in the local `error` state so the button doesn't
  // silently do nothing - same UX as the old Done screen's button.
  const finish = async () => {
    try {
      await api.updateConfig({
        tour_completed_at: new Date().toISOString(),
      });
      onComplete();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      throw err;
    }
  };

  const savePolymarketAndFinish = async () => {
    setBusy(true);
    setError(null);
    try {
      if (polymarketKey.trim()) {
        // The wallet address is derived from the key on the backend. A
        // separate wallet field used to sit here; buyers pasted the
        // deposit address polymarket.com shows them, which is not the
        // address the key signs as, and the save was rejected.
        await api.saveCredentials({ polymarket_private_key: polymarketKey.trim() });
        setPolymarketKey("");
      }
      await finish();
    } catch (err) {
      // saveCredentials may have already populated `error`; if not,
      // surface the exception text directly.
      if (!error) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  const skipAndFinish = async () => {
    setBusy(true);
    setError(null);
    try {
      await finish();
    } catch (err) {
      if (!error) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ob-shell">
      <div className="ob-progress">
        {/* Single step (Polymarket) is the only progress dot. The
            Welcome screen is an intro, not a step. */}
        {ORDER.slice(1).map((s) => {
          const sIdx = ORDER.indexOf(s);
          return (
            <div
              key={s}
              className={`ob-step ${sIdx < idx ? "done" : sIdx === idx ? "current" : ""}`}
            />
          );
        })}
      </div>

      <div className="ob-main">
        {step === "welcome" && (
          <WelcomeStep onNext={next} />
        )}
        {step === "polymarket" && (
          <PolymarketStep
            keyValue={polymarketKey}
            onKeyChange={setPolymarketKey}
            hasStored={creds?.has_polymarket_key ?? false}
            busy={busy} error={error}
            onBack={back}
            onSave={savePolymarketAndFinish}
            onSkip={skipAndFinish}
          />
        )}
      </div>
    </div>
  );
}

// ── Steps ──────────────────────────────────────────────────────────────

function WelcomeStep({ onNext }: { onNext: () => void }) {
  return (
    <>
      <div className="ob-eyebrow">Welcome to Delfi</div>
      <h1 className="ob-title">Your autonomous forecaster bot for Polymarket.</h1>
      <p className="ob-sub">
        Delfi runs entirely on your computer. We never see your keys,
        APIs or funds.
      </p>
      <div className="ob-callouts">
        <div className="ob-callout">
          <div className="ob-callout-eyebrow">SIMULATION MODE</div>
          <div className="ob-callout-body">
            Play on the real market with paper money. Great for testing
            strategies before you risk your own cash.
          </div>
        </div>
        <div className="ob-callout gold">
          <div className="ob-callout-eyebrow">LIVE MODE</div>
          <div className="ob-callout-body">
            Connect your Polymarket account and start trading.
          </div>
        </div>
      </div>
      <div className="ob-actions">
        <span />
        <button className="ob-next" onClick={onNext}>
          Get started <span aria-hidden>→</span>
        </button>
      </div>
    </>
  );
}

function PolymarketStep({
  keyValue, onKeyChange,
  hasStored, busy, error, onBack, onSave, onSkip,
}: {
  keyValue: string;
  onKeyChange: (v: string) => void;
  hasStored: boolean;
  busy: boolean;
  error: string | null;
  onBack: () => void;
  onSave: () => void;
  onSkip: () => void;
}) {
  return (
    <>
      <h1 className="ob-title">Connect Polymarket</h1>
      <p className="ob-sub">
        Only needed for live trading. Skip it and run in Simulation as
        long as you like, or come back when you&apos;re ready.
      </p>
      <div className="ob-form">
        <div className="ob-field">
          <label>Polymarket private key</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasStored ? "(stored)" : "0x..."}
            value={keyValue}
            onChange={(e) => onKeyChange(e.target.value)}
          />
        </div>
        <div className="ob-hint">
          Your private key never leaves this machine. Delfi signs every
          Polymarket trade locally and sends only the signed transaction.
          The wallet address is read from the key.
        </div>
        <div className="ob-hint">
          Before you fund the account, check that polymarket.com lets you
          place an order from your location.
        </div>
        {error && <div className="form-error">{error}</div>}
      </div>
      <div className="ob-actions">
        <button className="ob-back" onClick={onBack} disabled={busy}>← Back</button>
        <div className="ob-actions-right">
          <button className="ob-skip" onClick={onSkip} disabled={busy}>
            Skip for now
          </button>
          <button className="ob-next" onClick={onSave} disabled={busy}>
            {busy ? "Saving..." : "Save and continue"} <span aria-hidden>→</span>
          </button>
        </div>
      </div>
    </>
  );
}

// Optional helper if a future caller wants a tighter signal than the
// 5-second poll on the parent App. Currently unused.
export function useOnboardingComplete(state: BotState | null): boolean {
  const [complete, setComplete] = useState(false);
  useEffect(() => {
    if (state?.is_onboarded) setComplete(true);
  }, [state?.is_onboarded]);
  return complete;
}
