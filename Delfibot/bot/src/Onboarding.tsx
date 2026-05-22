import { useEffect, useState } from "react";
import { api, BotState, Credentials } from "./api";

/**
 * First-launch onboarding wizard.
 *
 * Shown when /api/state reports is_onboarded === false. Two screens
 * plus a confirmation:
 *   1. Welcome / mode overview (intro, no step number)
 *   2. Connect Polymarket (optional, only step)
 *   3. All set (confirmation + open dashboard)
 *
 * Starting capital and the LLM key are NOT collected here. The DB
 * defaults the bankroll to $1,000 (editable later in Settings >
 * Risk) and the forecaster runs against whichever LLM key the user
 * sets in Settings > Connections. Keeping the wizard short improves
 * first-launch conversion: simulation is fully usable straight out
 * of the wizard, no inputs required.
 */

type Step = "welcome" | "polymarket" | "done";

const ORDER: Step[] = ["welcome", "polymarket", "done"];

interface Props {
  state: BotState | null;
  creds: Credentials | null;
  onComplete: () => void;
}

export default function Onboarding({ creds, onComplete }: Props) {
  const [step, setStep] = useState<Step>("welcome");
  const [polymarketKey, setPolymarketKey] = useState("");
  const [wallet, setWallet] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const idx = ORDER.indexOf(step);
  const next = () => setStep(ORDER[Math.min(ORDER.length - 1, idx + 1)]);
  const back = () => setStep(ORDER[Math.max(0, idx - 1)]);

  const savePolymarket = async () => {
    if (!polymarketKey.trim() && !wallet.trim()) { next(); return; }
    setBusy(true); setError(null);
    try {
      const payload: Record<string, string> = {};
      if (polymarketKey.trim()) payload.polymarket_private_key = polymarketKey.trim();
      if (wallet.trim()) payload.wallet_address = wallet.trim();
      await api.saveCredentials(payload);
      setPolymarketKey("");
      next();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setBusy(false); }
  };

  return (
    <div className="ob-shell">
      <div className="ob-progress">
        {/* Single step (Polymarket) is the only progress dot. The
            Welcome screen is an intro, not a step; Done is the
            completion screen. Keeping the dot here so the user
            still sees a sense of "where am I" on Polymarket
            without overcounting. */}
        {ORDER.slice(1, -1).map((s) => {
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
            keyValue={polymarketKey} walletValue={wallet}
            onKeyChange={setPolymarketKey} onWalletChange={setWallet}
            hasStored={creds?.has_polymarket_key ?? false}
            existingWallet={creds?.wallet_address ?? null}
            busy={busy} error={error}
            onBack={back} onSave={savePolymarket} onSkip={next}
          />
        )}
        {step === "done" && (
          <DoneStep
            hasPolymarket={creds?.has_polymarket_key ?? false}
            onFinish={async () => {
              // Mark the wizard finished server-side. is_onboarded
              // gates on this timestamp, not on mode/starting_cash
              // (which have DB server defaults: mode=simulation,
              // starting_cash=1000.0). Let errors propagate to the
              // DoneStep so the user sees a real message instead of
              // a button that silently does nothing.
              await api.updateConfig({ tour_completed_at: new Date().toISOString() });
              onComplete();
            }} />
        )}
      </div>
    </div>
  );
}

// ── Steps ──────────────────────────────────────────────────────────────

function WelcomeStep({ onNext }: { onNext: () => void }) {
  return (
    <>
      <div className="ob-eyebrow">Welcome</div>
      <h1 className="ob-title">An autonomous forecaster for Polymarket.</h1>
      <p className="ob-sub">
        Delfi reads every tradable market, runs its own forecast, and
        only places a trade when the numbers line up. It runs entirely
        on your machine. Your keys and funds never leave it.
      </p>
      <div className="ob-callouts">
        <div className="ob-callout">
          <div className="ob-callout-eyebrow">SIMULATION MODE</div>
          <div className="ob-callout-title">Default. Risk-free.</div>
          <div className="ob-callout-body">
            Delfi trades with paper money. Same forecasts, same sizing,
            same risk limits as live. Run it until you trust the numbers.
          </div>
        </div>
        <div className="ob-callout gold">
          <div className="ob-callout-eyebrow">LIVE MODE</div>
          <div className="ob-callout-title">Real money on Polymarket.</div>
          <div className="ob-callout-body">
            Connect your Polymarket wallet when you&apos;re ready. You can
            switch over any time.
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
  keyValue, walletValue, onKeyChange, onWalletChange,
  hasStored, existingWallet, busy, error, onBack, onSave, onSkip,
}: {
  keyValue: string;
  walletValue: string;
  onKeyChange: (v: string) => void;
  onWalletChange: (v: string) => void;
  hasStored: boolean;
  existingWallet: string | null;
  busy: boolean;
  error: string | null;
  onBack: () => void;
  onSave: () => void;
  onSkip: () => void;
}) {
  return (
    <>
      <div className="ob-eyebrow">FINAL STEP · OPTIONAL</div>
      <h1 className="ob-title">Connect Polymarket</h1>
      <p className="ob-sub">
        Only needed for live trading. Skip it and run in Simulation as
        long as you like, or come back when you&apos;re ready. Your
        credentials are stored in your system keychain, never written
        to disk.
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
        <div className="ob-field">
          <label>Wallet address</label>
          <input
            type="text"
            autoComplete="off"
            placeholder={existingWallet ?? "0x..."}
            value={walletValue}
            onChange={(e) => onWalletChange(e.target.value)}
          />
        </div>
        <div className="ob-hint">
          Your private key never leaves this machine. Delfi signs every
          Polymarket trade locally and sends only the signed transaction.
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

function DoneStep({
  hasPolymarket,
  onFinish,
}: {
  hasPolymarket: boolean;
  onFinish: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const handleClick = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onFinish();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="ob-eyebrow">All set</div>
      <h1 className="ob-title">Delfi is ready.</h1>
      <p className="ob-sub">
        Simulation is live with a $1,000 paper bankroll. Delfi scans
        Polymarket every five minutes, forecasts each new market, and
        opens a position whenever the numbers line up. Watch the
        dashboard. Your first trades usually land within ten to twenty
        minutes.
      </p>
      <div className="ob-checklist">
        <ChecklistItem ok>Forecasting connected</ChecklistItem>
        <ChecklistItem ok={hasPolymarket}>
          Polymarket wallet (for live mode)
        </ChecklistItem>
      </div>
      {error && <div className="form-error">{error}</div>}
      <div className="ob-actions">
        <span />
        <button className="ob-next" onClick={handleClick} disabled={busy}>
          {busy ? "Saving..." : "Open the dashboard"} <span aria-hidden>→</span>
        </button>
      </div>
    </>
  );
}

function ChecklistItem({ ok, children }: { ok?: boolean; children: React.ReactNode }) {
  return (
    <div className={`ob-checklist-row ${ok ? "ok" : ""}`}>
      <span className="ob-checklist-mark">{ok ? "✓" : "○"}</span>
      <span>{children}</span>
    </div>
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
