import { useEffect, useState } from "react";
import { api, BotState, Credentials } from "./api";

/**
 * First-launch onboarding wizard.
 *
 * Shown when /api/state reports is_onboarded === false. Walks the user
 * through the things Delfi needs:
 *   1. Welcome
 *   2. Anthropic API key (required)
 *   3. Bankroll + simulation default (required)
 *   4. Polymarket key + wallet (optional, only for live mode)
 *   5. Telegram (optional)
 *   6. Done
 */

type Step = "welcome" | "anthropic" | "bankroll" | "polymarket" | "telegram" | "done";

const ORDER: Step[] = ["welcome", "anthropic", "bankroll", "polymarket", "telegram", "done"];

interface Props {
  state: BotState | null;
  creds: Credentials | null;
  onComplete: () => void;
}

export default function Onboarding({ creds, onComplete }: Props) {
  const [step, setStep] = useState<Step>("welcome");
  const [anthropic, setAnthropic] = useState("");
  const [bankroll, setBankroll] = useState("1000");
  const [polymarketKey, setPolymarketKey] = useState("");
  const [wallet, setWallet] = useState("");
  const [telegramToken, setTelegramToken] = useState("");
  const [telegramChatId, setTelegramChatId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const idx = ORDER.indexOf(step);
  const next = () => setStep(ORDER[Math.min(ORDER.length - 1, idx + 1)]);
  const back = () => setStep(ORDER[Math.max(0, idx - 1)]);

  const saveAnthropic = async () => {
    if (!anthropic.trim()) { next(); return; }
    setBusy(true); setError(null);
    try {
      await api.saveCredentials({ anthropic_api_key: anthropic.trim() });
      setAnthropic("");
      next();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setBusy(false); }
  };

  const saveBankroll = async () => {
    setBusy(true); setError(null);
    try {
      const n = Number(bankroll);
      if (!Number.isFinite(n) || n < 10 || n > 100_000) {
        throw new Error("Bankroll must be between $10 and $100,000.");
      }
      await api.updateConfig({ starting_cash: n, mode: "simulation" });
      next();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setBusy(false); }
  };

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

  const saveTelegram = async () => {
    if (!telegramToken.trim() && !telegramChatId.trim()) { next(); return; }
    setBusy(true); setError(null);
    try {
      const payload: { telegram_bot_token?: string; telegram_chat_id?: string } = {};
      if (telegramToken.trim()) payload.telegram_bot_token = telegramToken.trim();
      if (telegramChatId.trim()) payload.telegram_chat_id = telegramChatId.trim();
      await api.saveTelegram(payload);
      setTelegramToken("");
      next();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setBusy(false); }
  };

  return (
    <div className="ob-shell">
      <div className="ob-progress">
        {ORDER.slice(0, -1).map((s, i) => (
          <div key={s} className={`ob-step ${i < idx ? "done" : i === idx ? "current" : ""}`} />
        ))}
      </div>

      <div className="ob-main">
        {step === "welcome" && (
          <WelcomeStep onNext={next} />
        )}
        {step === "anthropic" && (
          <AnthropicStep
            value={anthropic} onChange={setAnthropic}
            hasStored={creds?.has_anthropic_key ?? false}
            busy={busy} error={error}
            onBack={back} onSave={saveAnthropic} onSkip={next}
          />
        )}
        {step === "bankroll" && (
          <BankrollStep
            value={bankroll} onChange={setBankroll}
            busy={busy} error={error}
            onBack={back} onSave={saveBankroll}
          />
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
        {step === "telegram" && (
          <TelegramStep
            token={telegramToken} chatId={telegramChatId}
            onTokenChange={setTelegramToken} onChatIdChange={setTelegramChatId}
            busy={busy} error={error}
            onBack={back} onSave={saveTelegram} onSkip={next}
          />
        )}
        {step === "done" && (
          <DoneStep onFinish={onComplete} />
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
      <h1 className="ob-title">Delfi watches Polymarket so you don&apos;t have to.</h1>
      <p className="ob-sub">
        Delfi backs the side the market itself favours on every tradeable
        contract, and steps aside whenever its own forecast disagrees with
        the price. It runs entirely on your machine.
      </p>
      <div className="ob-callouts">
        <div className="ob-callout">
          <div className="ob-callout-eyebrow">SIMULATION MODE</div>
          <div className="ob-callout-title">Default. Risk-free.</div>
          <div className="ob-callout-body">
            Delfi trades against a synthetic bankroll. Same forecasts, same
            sizing, same risk caps. Use this until you trust the numbers.
          </div>
        </div>
        <div className="ob-callout gold">
          <div className="ob-callout-eyebrow">LIVE MODE</div>
          <div className="ob-callout-title">Real money on Polymarket.</div>
          <div className="ob-callout-body">
            Requires your Polymarket private key and a wallet address. You
            can switch to live whenever you&apos;re ready.
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

function AnthropicStep({
  value, onChange, hasStored, busy, error, onBack, onSave, onSkip,
}: {
  value: string;
  onChange: (v: string) => void;
  hasStored: boolean;
  busy: boolean;
  error: string | null;
  onBack: () => void;
  onSave: () => void;
  onSkip: () => void;
}) {
  return (
    <>
      <div className="ob-eyebrow">STEP 1 of 4 · Required</div>
      <h1 className="ob-title">Anthropic API key</h1>
      <p className="ob-sub">
        Delfi&apos;s forecaster runs on Anthropic. Bring your own key so
        you control the spend and the rate limits. Stored in your OS
        keychain, never on disk.
      </p>
      <div className="ob-form">
        <div className="ob-field">
          <label>API key</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasStored ? "(stored, paste a new one to replace)" : "sk-ant-..."}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            autoFocus
          />
          <div className="ob-hint">
            Get one at <code>console.anthropic.com</code>. Cost is roughly
            $0.01–$0.05 per market evaluation.
          </div>
        </div>
        {error && <div className="form-error">{error}</div>}
      </div>
      <div className="ob-actions">
        <button className="ob-back" onClick={onBack} disabled={busy}>← Back</button>
        <div className="ob-actions-right">
          {hasStored && (
            <button className="ob-skip" onClick={onSkip} disabled={busy}>
              Keep stored key
            </button>
          )}
          <button
            className="ob-next"
            onClick={onSave}
            disabled={busy || (!value.trim() && !hasStored)}
          >
            {busy ? "Saving..." : "Continue"} <span aria-hidden>→</span>
          </button>
        </div>
      </div>
    </>
  );
}

function BankrollStep({
  value, onChange, busy, error, onBack, onSave,
}: {
  value: string;
  onChange: (v: string) => void;
  busy: boolean;
  error: string | null;
  onBack: () => void;
  onSave: () => void;
}) {
  const presets = [500, 1000, 5000, 10000];
  const n = Number(value);
  const valid = Number.isFinite(n) && n >= 10 && n <= 100_000;
  return (
    <>
      <div className="ob-eyebrow">STEP 2 of 4 · Required</div>
      <h1 className="ob-title">Starting bankroll</h1>
      <p className="ob-sub">
        How much capital should Delfi treat as 100%? Stake size and
        circuit breakers are computed against this number. In simulation
        mode this is the synthetic balance; in live mode it is your seeded
        capital.
      </p>
      <div className="ob-form">
        <div className="ob-field">
          <label>Bankroll (USD)</label>
          <input
            type="number"
            min={10}
            max={100_000}
            step="1"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            autoFocus
          />
          <div className="ob-presets">
            {presets.map((p) => (
              <button
                key={p}
                type="button"
                className={`ob-preset ${Number(value) === p ? "on" : ""}`}
                onClick={() => onChange(String(p))}
              >
                ${p.toLocaleString()}
              </button>
            ))}
          </div>
          <div className="ob-hint">
            Minimum $10. You can change this any time from Settings.
          </div>
        </div>
        {error && <div className="form-error">{error}</div>}
      </div>
      <div className="ob-actions">
        <button className="ob-back" onClick={onBack} disabled={busy}>← Back</button>
        <button className="ob-next" onClick={onSave} disabled={busy || !valid}>
          {busy ? "Saving..." : "Continue"} <span aria-hidden>→</span>
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
      <div className="ob-eyebrow">STEP 3 of 4 · Optional</div>
      <h1 className="ob-title">Polymarket credentials</h1>
      <p className="ob-sub">
        Only needed for live trading. You can skip this and run in
        simulation mode forever, or come back later when you&apos;re ready.
        Stored in your OS keychain, never on disk.
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
          Your private key never leaves this machine. Delfi signs
          Polymarket trades locally and sends only the signed transaction.
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

function TelegramStep({
  token, chatId, onTokenChange, onChatIdChange,
  busy, error, onBack, onSave, onSkip,
}: {
  token: string;
  chatId: string;
  onTokenChange: (v: string) => void;
  onChatIdChange: (v: string) => void;
  busy: boolean;
  error: string | null;
  onBack: () => void;
  onSave: () => void;
  onSkip: () => void;
}) {
  return (
    <>
      <div className="ob-eyebrow">STEP 4 of 4 · Optional</div>
      <h1 className="ob-title">Telegram notifications</h1>
      <p className="ob-sub">
        Delfi can DM you every position open, every resolution, and a
        daily summary. The desktop app shows everything in-app, so this
        is purely for phone notifications.
      </p>
      <div className="ob-form">
        <div className="ob-field">
          <label>Bot token</label>
          <input
            type="password"
            autoComplete="off"
            placeholder="123456:ABC-..."
            value={token}
            onChange={(e) => onTokenChange(e.target.value)}
          />
          <div className="ob-hint">
            Create a bot with <code>@BotFather</code> in Telegram, copy the token here.
          </div>
        </div>
        <div className="ob-field">
          <label>Chat ID</label>
          <input
            type="text"
            autoComplete="off"
            placeholder="e.g. 123456789"
            value={chatId}
            onChange={(e) => onChatIdChange(e.target.value)}
          />
          <div className="ob-hint">
            Message your new bot once, then visit{" "}
            <code>api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code> and
            copy the chat ID.
          </div>
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

function DoneStep({ onFinish }: { onFinish: () => void }) {
  return (
    <>
      <div className="ob-eyebrow">All set</div>
      <h1 className="ob-title">Delfi is ready.</h1>
      <p className="ob-sub">
        Simulation mode is live. Delfi will scan Polymarket every five
        minutes, evaluate fresh markets, and open simulated positions when
        its forecast agrees with the market favourite. Watch the
        Dashboard for the first activity, expect the first trades within
        ten to twenty minutes.
      </p>
      <div className="ob-checklist">
        <ChecklistItem ok>Anthropic key stored</ChecklistItem>
        <ChecklistItem ok>Bankroll set</ChecklistItem>
        <ChecklistItem>Polymarket key + wallet (for live mode)</ChecklistItem>
        <ChecklistItem>Telegram (optional)</ChecklistItem>
      </div>
      <div className="ob-actions">
        <span />
        <button className="ob-next" onClick={onFinish}>
          Open the dashboard <span aria-hidden>→</span>
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
