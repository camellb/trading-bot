import { useEffect, useState } from "react";
import {
  api,
  AutostartStatus,
  Credentials,
  LicenseStatus,
  NotificationsConfig,
  TelegramConfig,
} from "../api";
import type { SettingsTab } from "../App";

/**
 * Settings - SaaS-parity layout, with desktop additions:
 *   - Simulation reset (desktop-only)
 *   - Auto-start at login toggle (macOS LaunchAgent supervision)
 *
 * Risk and archetype controls used to live here as a sub-tab. They were
 * promoted to a top-level page on 2026-05-02 (see pages/Risk.tsx).
 *
 * The active tab is owned by App and surfaced via the sidebar sub-nav.
 * This page renders one panel at a time. Each form submits independently
 * and refreshes the parent on success.
 */

const BOUNDS = {
  starting_cash: [10, 100_000] as const,
};

type ConfigShape = {
  starting_cash?: number | null;
  [k: string]: unknown;
};

interface Props {
  tab: SettingsTab;
  setTab: (t: SettingsTab) => void;
  creds: Credentials | null;
  config: ConfigShape | null;
  onSaved: () => void;
}

const TITLES: Record<SettingsTab, { h1: string; sub: string }> = {
  account:       { h1: "Account",         sub: "" },
  connections:   { h1: "Connections",     sub: "" },
  notifications: { h1: "Notifications",   sub: "" },
};

export default function Settings({ tab, creds, config, onSaved }: Props) {
  // setTab is in Props for future use (eg deep-linking) but the sidebar owns
  // tab switching today; ignore it here without triggering noUnusedLocals.
  const t = TITLES[tab];
  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">{t.h1}</h1>
            {t.sub && <p className="page-sub">{t.sub}</p>}
          </div>
        </div>
      </div>

      {tab === "account"       && <AccountPanel       config={config} onSaved={onSaved} />}
      {tab === "connections"   && <ConnectionsPanel   creds={creds}   onSaved={onSaved} />}
      {tab === "notifications" && <NotificationsPanel />}
    </div>
  );
}

// ── Account ──────────────────────────────────────────────────────────────

function AccountPanel({
  config,
  onSaved,
}: {
  config: ConfigShape | null;
  onSaved: () => void;
}) {
  const [startingCash, setStartingCash] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);

  useEffect(() => {
    if (config?.starting_cash != null) setStartingCash(String(config.starting_cash));
  }, [config?.starting_cash]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const n = Number(startingCash);
      if (!Number.isFinite(n)) throw new Error("Starting cash must be a number.");
      const [lo, hi] = BOUNDS.starting_cash;
      if (n < lo || n > hi) throw new Error(`Starting cash must be between ${lo} and ${hi}.`);
      await api.updateConfig({ starting_cash: n });
      setMsg({ kind: "ok", text: `Capital set to $${n.toFixed(2)}.` });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setResetBusy(true);
    setMsg(null);
    try {
      const r = await api.resetSimulation();
      setMsg({ kind: "ok", text: r.detail || "Simulation reset." });
      setConfirm(false);
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setResetBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Capital</h2>
        </div>
        <p className="page-sub" style={{ marginBottom: 16 }}>
          The starting cash Delfi treats as 100% of capital. Stake size and
          circuit breakers are computed against this number.
        </p>
        <form className="form-row" onSubmit={save}>
          <div className="form-field">
            <label>Starting cash (USD)</label>
            <input
              type="number"
              min={BOUNDS.starting_cash[0]}
              max={BOUNDS.starting_cash[1]}
              step="1"
              value={startingCash}
              onChange={(e) => setStartingCash(e.target.value)}
            />
          </div>
          <div className="form-actions">
            <button type="submit" className="btn small" disabled={busy}>
              {busy ? "Saving..." : "Save capital"}
            </button>
            {msg && (
              <span className={msg.kind === "ok" ? "form-success" : "form-error"}>
                {msg.text}
              </span>
            )}
          </div>
        </form>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Simulation reset</h2>
        </div>
        <p className="page-sub" style={{ marginBottom: 16 }}>
          Clears all simulation positions and resets the synthetic capital
          to your starting cash. Live trading is untouched.
        </p>
        {!confirm ? (
          <div className="form-actions">
            <button
              type="button"
              className="btn ghost small"
              onClick={() => setConfirm(true)}
            >
              Reset simulation
            </button>
          </div>
        ) : (
          <div className="form-actions">
            <button
              type="button"
              className="btn danger small"
              onClick={reset}
              disabled={resetBusy}
            >
              {resetBusy ? "Resetting..." : "Yes, reset"}
            </button>
            <button
              type="button"
              className="btn ghost small"
              onClick={() => setConfirm(false)}
              disabled={resetBusy}
            >
              Cancel
            </button>
          </div>
        )}
      </div>

      <AutostartPanel />
      <LicensePanel />
    </>
  );
}

// ── Auto-start at login ──────────────────────────────────────────────────

/**
 * Auto-start panel inside Account.
 *
 * Toggles the macOS LaunchAgent at ~/Library/LaunchAgents/
 * com.delfi.bot.plist. ON means the daemon launches at every user
 * login and auto-restarts on crash (RunAtLoad=true + KeepAlive=true).
 * OFF means the daemon doesn't start at login; toggling OFF also
 * stops the currently-running daemon (launchctl bootout signals
 * SIGTERM).
 *
 * Currently macOS-only. On other platforms the panel renders a
 * disabled state with a "macOS-only" hint.
 */
function AutostartPanel() {
  const [status, setStatus] = useState<AutostartStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.autostart()
      .then((s) => alive && setStatus(s))
      .catch(() => alive && setStatus({
        supported: false,
        enabled:   false,
        reason:    "Could not read auto-start status.",
      }));
    return () => { alive = false; };
  }, []);

  const toggle = async () => {
    if (!status?.supported || busy) return;
    const next = !status.enabled;
    const previous = status;
    // Optimistic flip: the launchctl call takes 1-2s and the user
    // wants to see the switch move immediately.
    setStatus({ ...status, enabled: next });
    setBusy(true);
    setMsg(null);
    try {
      const updated = await api.setAutostart(next);
      setStatus(updated);
      setMsg({
        kind: "ok",
        text: next
          ? "Auto-start enabled. Delfi will launch at every login."
          : "Auto-start disabled. Delfi stopped and won't start at login.",
      });
    } catch (err) {
      setStatus(previous);
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Auto-start at login</h2>
        <span className="panel-meta">
          {status?.supported === false ? "macOS only" : "macOS"}
        </span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        When on, Delfi runs as a background daemon: it starts at every
        login, survives the GUI window closing, and auto-restarts within
        ~10s if it crashes. Trading continues 24/7. Turning it off stops
        the daemon and requires you to launch Delfi manually next time.
      </p>
      <div className="notif-row">
        <div>
          <div className="notif-name">
            Run Delfi automatically at login
          </div>
          <div className="notif-desc">
            {status === null
              ? "Loading..."
              : status.supported === false
                ? (status.reason ?? "Not available on this platform.")
                : status.enabled
                  ? "Currently enabled. Delfi is running in the background."
                  : (status.reason ?? "Currently disabled. Delfi is not running.")}
          </div>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            checked={!!status?.enabled}
            disabled={!status?.supported || busy}
            onChange={toggle}
          />
          <span className="toggle-slider" />
        </label>
      </div>
      {msg && (
        <p className={msg.kind === "ok" ? "form-success" : "form-error"}
           style={{ marginTop: 12 }}>
          {msg.text}
        </p>
      )}
    </div>
  );
}

// ── License ──────────────────────────────────────────────────────────────

/**
 * License panel inside Account.
 *
 * Lets the user see the license currently activated on this machine
 * and sign out of it. "Sign out" calls /api/license/deactivate which
 * (a) tells Lemon Squeezy to free the activation slot for this
 * instance, then (b) wipes the local keychain. After that the
 * LicenseGate re-mounts and the user can paste a different key.
 *
 * Used for: moving Delfi to a new computer, handing the machine to
 * someone else, recovering from a billing error after a refund.
 */
function LicensePanel() {
  const [status, setStatus] = useState<LicenseStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err" | "warn"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.license()
      .then((s) => alive && setStatus(s))
      .catch(() => alive && setStatus(null));
    return () => { alive = false; };
  }, []);

  const signOut = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const next = await api.deactivateLicense();
      setStatus(next);
      if (next.warning) {
        setMsg({ kind: "warn", text: next.warning });
      } else {
        setMsg({ kind: "ok", text: "Signed out. Restarting will show the license screen." });
      }
      // Re-mounts the LicenseGate the next time it polls.
      window.dispatchEvent(new CustomEvent("delfi:license-changed"));
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };

  const lastValidated = status?.last_validated_at
    ? new Date(status.last_validated_at).toLocaleString()
    : null;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">License</h2>
      </div>
      {status?.has_key ? (
        <p className="page-sub" style={{ marginBottom: 16 }}>
          This machine is activated. Last validated {lastValidated || "never"}.
        </p>
      ) : (
        <p className="page-sub" style={{ marginBottom: 16 }}>
          No license activated on this machine.
        </p>
      )}
      {status?.has_key && (
        !confirm ? (
          <div className="form-actions">
            <button
              type="button"
              className="btn ghost small"
              onClick={() => setConfirm(true)}
              disabled={busy}
            >
              Sign out from this device
            </button>
          </div>
        ) : (
          <div className="form-actions">
            <button
              type="button"
              className="btn danger small"
              onClick={signOut}
              disabled={busy}
            >
              {busy ? "Signing out..." : "Yes, sign out"}
            </button>
            <button
              type="button"
              className="btn ghost small"
              onClick={() => setConfirm(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        )
      )}
      {msg && (
        <p
          className={
            msg.kind === "ok"   ? "form-success" :
            msg.kind === "warn" ? "form-error"   :
            "form-error"
          }
          style={{ marginTop: 12 }}
        >
          {msg.text}
        </p>
      )}
    </div>
  );
}

// ── Connections ──────────────────────────────────────────────────────────

function ConnectionsPanel({
  creds,
  onSaved,
}: {
  creds: Credentials | null;
  onSaved: () => void;
}) {
  const [pmKey, setPmKey] = useState("");
  const [wallet, setWallet] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [llmBackup, setLlmBackup] = useState("");
  const [newsapi, setNewsapi] = useState("");
  const [cryptopanic, setCryptopanic] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (creds) setWallet(creds.wallet_address ?? "");
  }, [creds]);

  // Older sidecars don't return `has_llm_key`; fall back to the legacy
  // `has_anthropic_key` so the "(stored)" placeholder is correct on
  // either version.
  const hasLlm = creds?.has_llm_key ?? creds?.has_anthropic_key ?? false;
  const hasLlmBackup = creds?.has_llm_backup_key ?? false;
  const hasNewsapi = creds?.has_newsapi_key ?? false;
  const hasCryptopanic = creds?.has_cryptopanic_key ?? false;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const payload: Parameters<typeof api.saveCredentials>[0] = {};
      if (pmKey.trim())       payload.polymarket_private_key = pmKey.trim();
      if (wallet.trim())      payload.wallet_address = wallet.trim();
      if (llmKey.trim())      payload.llm_api_key = llmKey.trim();
      if (llmBackup.trim())   payload.llm_backup_key = llmBackup.trim();
      if (newsapi.trim())     payload.newsapi_key = newsapi.trim();
      if (cryptopanic.trim()) payload.cryptopanic_key = cryptopanic.trim();
      if (Object.keys(payload).length === 0) {
        setMsg({ kind: "err", text: "Nothing to save." });
        return;
      }
      const res = await api.saveCredentials(payload);
      setPmKey("");
      setLlmKey("");
      setLlmBackup("");
      setNewsapi("");
      setCryptopanic("");
      setMsg({ kind: "ok", text: `Saved: ${res.wrote.join(", ") || "nothing"}.` });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Credentials</h2>
        <span className="panel-meta">Stored in OS keychain</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        All keys live in your operating system keychain — never on disk and
        never sent to Delfi servers. Leaving a field blank keeps the existing
        value.
      </p>
      <form className="form-row" onSubmit={save}>
        <div className="form-field">
          <label>Polymarket private key</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={creds?.has_polymarket_key ? "(stored)" : "0x..."}
            value={pmKey}
            onChange={(e) => setPmKey(e.target.value)}
          />
          <p className="form-hint">
            Signs Polymarket orders for live trading. Required only when you
            switch the bot to Live mode.
          </p>
        </div>
        <div className="form-field">
          <label>Wallet address</label>
          <input
            type="text"
            autoComplete="off"
            placeholder="0x..."
            value={wallet}
            onChange={(e) => setWallet(e.target.value)}
          />
          <p className="form-hint">
            The public 0x address paired with the private key above.
          </p>
        </div>

        <div className="form-field">
          <label>LLM API key</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasLlm ? "(stored)" : "Paste your LLM API key"}
            value={llmKey}
            onChange={(e) => setLlmKey(e.target.value)}
          />
          <p className="form-hint">
            The model that reads each Polymarket market and produces
            Delfi&apos;s forecast. Without this, Delfi can&apos;t decide
            whether to trade. Bring your own key from any major LLM
            provider.
          </p>
        </div>

        <div className="form-field">
          <label>Backup LLM API key (optional)</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasLlmBackup ? "(stored)" : "sk-..."}
            value={llmBackup}
            onChange={(e) => setLlmBackup(e.target.value)}
          />
          <p className="form-hint">
            A second LLM Delfi falls back to if the primary is rate-limited
            or returns an error. Useful at higher trading volume or as a
            hedge against provider outages. Stored now; failover wiring lands
            with multi-provider support.
          </p>
        </div>

        <div className="form-field">
          <label>NewsAPI key (optional)</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasNewsapi ? "(stored)" : "..."}
            value={newsapi}
            onChange={(e) => setNewsapi(e.target.value)}
          />
          <p className="form-hint">
            Pulls breaking news headlines around event-resolution windows.
            Adds context to forecasts on geopolitical, economic, and
            current-event markets. Free tier at newsapi.org. Without it
            Delfi falls back to RSS feeds and may miss late-breaking context.
          </p>
        </div>

        <div className="form-field">
          <label>CryptoPanic key (optional)</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasCryptopanic ? "(stored)" : "..."}
            value={cryptopanic}
            onChange={(e) => setCryptopanic(e.target.value)}
          />
          <p className="form-hint">
            Pulls crypto-specific news (tokens, regulators, exchange events)
            into Delfi&apos;s research feed. Useful for Polymarket&apos;s
            crypto-themed markets (BTC threshold, ETH ETF, exchange events).
            Free at cryptopanic.com.
          </p>
        </div>

        <div className="form-actions">
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save credentials"}
          </button>
          {msg && (
            <span className={msg.kind === "ok" ? "form-success" : "form-error"}>
              {msg.text}
            </span>
          )}
        </div>
      </form>
    </div>
  );
}

// ── Notifications ────────────────────────────────────────────────────────

const CATEGORY_LABELS: Record<string, { title: string; description: string }> = {
  position_opened: {
    title: "New positions",
    description: "Every time Delfi opens a position: market, side, stake, and forecast.",
  },
  position_settled: {
    title: "Position resolutions",
    description: "Every win or loss when a market resolves, with P&L and running capital.",
  },
  daily_summary: {
    title: "Daily summary",
    description: "End-of-day recap with trades, P&L, and record.",
  },
  weekly_summary: {
    title: "Weekly summary",
    description: "Weekly performance review with win rate and P&L.",
  },
  calibration: {
    title: "Calibration proposals",
    description: "When Delfi proposes a strategy change, with evidence and inline controls.",
  },
  risk_event: {
    title: "Risk events",
    description: "Circuit breaker trips: daily loss cap, drawdown halt, or streak cooldown.",
  },
};

function NotificationsPanel() {
  const [notif, setNotif] = useState<NotificationsConfig | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [prefSavingKey, setPrefSavingKey] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const n = await api.notifications();
        if (!cancelled) setNotif(n);
      } catch (err) {
        if (!cancelled) {
          setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
        }
      }
    };
    load();
    return () => { cancelled = true; };
  }, []);

  const togglePref = async (key: string) => {
    if (!notif || prefSavingKey) return;
    const current = notif.notification_prefs[key];
    const next = current === false ? true : false;
    const previous = notif;
    const optimistic: NotificationsConfig = {
      ...notif,
      notification_prefs: { ...notif.notification_prefs, [key]: next },
    };
    setNotif(optimistic);
    setPrefSavingKey(key);
    try {
      const res = await api.saveNotifications(optimistic.notification_prefs);
      setNotif(res);
    } catch (err) {
      setNotif(previous);
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setPrefSavingKey(null);
    }
  };

  const isOn = (key: string): boolean => {
    if (!notif) return true;
    const v = notif.notification_prefs[key];
    return v === undefined ? true : v;
  };

  const categories = notif?.categories?.length
    ? notif.categories
    : Object.keys(CATEGORY_LABELS);

  return (
    <>
      <TelegramConnectorPanel />
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">What Delfi will surface</h2>
          <span className="panel-meta">Changes apply immediately</span>
        </div>
        <div>
          {categories.map((key) => {
            const label = CATEGORY_LABELS[key] ?? { title: key, description: "" };
            return (
              <div key={key} className="notif-row">
                <div>
                  <div className="notif-name">{label.title}</div>
                  {label.description && (
                    <div className="notif-desc">{label.description}</div>
                  )}
                </div>
                <label className="toggle-switch">
                  <input
                    type="checkbox"
                    checked={isOn(key)}
                    disabled={prefSavingKey === key}
                    onChange={() => togglePref(key)}
                  />
                  <span className="toggle-slider" />
                </label>
              </div>
            );
          })}
        </div>
        {msg && (
          <p className={msg.kind === "ok" ? "form-success" : "form-error"}
             style={{ marginTop: 12 }}>
            {msg.text}
          </p>
        )}
      </div>
    </>
  );
}

// ── Telegram connector ──────────────────────────────────────────────────

/**
 * Telegram connector card.
 *
 * Push-only outbound connection to a user-supplied Telegram bot.
 * Setup is BYO:
 *   1. User creates a bot via @BotFather on Telegram, gets a token.
 *   2. User starts a chat with their new bot and sends /start.
 *   3. User finds their numeric chat id (e.g. via @userinfobot, or
 *      by visiting `https://api.telegram.org/bot<TOKEN>/getUpdates`).
 *   4. User pastes both into this card and clicks "Test + save". The
 *      sidecar sends a probe message; on success it persists the
 *      pair (token to keychain, chat id to user_config). On failure
 *      nothing is persisted and the user sees Telegram's error.
 *
 * The token is treated as a secret: the GET endpoint returns only
 * `bot_token_configured: boolean`, never the token itself. Disconnect
 * wipes both.
 */
function TelegramConnectorPanel() {
  const [tg, setTg] = useState<TelegramConfig | null>(null);
  const [token, setToken] = useState("");
  const [chat, setChat] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.telegram()
      .then((s) => {
        if (!alive) return;
        setTg(s);
        if (s.chat_id) setChat(s.chat_id);
      })
      .catch(() => alive && setTg(null));
    return () => { alive = false; };
  }, []);

  const isConnected = !!tg?.bot_token_configured && !!tg?.chat_id;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setMsg(null);
    // Save permits a partial update: empty token leaves the saved one
    // alone (the placeholder shows "saved"), empty chat id is the
    // same. Both empty + nothing already saved is a no-op error.
    if (!token.trim() && !chat.trim() && !tg?.bot_token_configured && !tg?.chat_id) {
      setMsg({ kind: "err", text: "Paste your bot token and chat id first." });
      return;
    }
    setBusy(true);
    try {
      const next = await api.saveTelegram(token.trim(), chat.trim());
      setTg(next);
      setToken("");
      setMsg({ kind: "ok", text: "Saved. Click Test to send a probe message." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    if (busy) return;
    setMsg(null);
    // Form values take priority; if the user is testing creds before
    // saving, those flow to the sidecar. If both are blank, the
    // sidecar falls back to whatever's saved.
    const formToken = token.trim();
    const formChat  = chat.trim();
    if (!formToken && !tg?.bot_token_configured) {
      setMsg({ kind: "err", text: "No bot token saved yet. Paste one and click Save first." });
      return;
    }
    if (!formChat && !tg?.chat_id) {
      setMsg({ kind: "err", text: "No chat id saved yet. Paste one and click Save first." });
      return;
    }
    setBusy(true);
    try {
      await api.testTelegram(formToken, formChat);
      setMsg({ kind: "ok", text: "Test sent. Check Telegram." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const next = await api.disconnectTelegram();
      setTg(next);
      setToken("");
      setChat("");
      setMsg({ kind: "ok", text: "Telegram disconnected." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
      setConfirmDisconnect(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Telegram</h2>
        <span className="panel-meta">
          {isConnected ? "Connected" : "Not connected"}
        </span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Push trades, settlements, and risk events to your phone.
        Create a bot via{" "}
        <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">
          @BotFather
        </a>{" "}
        to get a token, then send any message to your bot so it has a
        chat id. Find your chat id via{" "}
        <a href="https://t.me/userinfobot" target="_blank" rel="noreferrer">
          @userinfobot
        </a>.
      </p>

      <form className="form-row" onSubmit={save}>
        <div className="form-field">
          <label>Bot token</label>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={
              tg?.bot_token_configured
                ? "•••••• (saved)"
                : "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
            }
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        </div>
        <div className="form-field">
          <label>Chat id</label>
          <input
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="123456789"
            value={chat}
            onChange={(e) => setChat(e.target.value)}
          />
        </div>
        <div className="form-actions">
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save"}
          </button>
          <button
            type="button"
            className="btn ghost small"
            onClick={test}
            disabled={busy}
          >
            {busy ? "..." : "Test"}
          </button>
          {isConnected && (
            !confirmDisconnect ? (
              <button
                type="button"
                className="btn ghost small"
                onClick={() => setConfirmDisconnect(true)}
                disabled={busy}
              >
                Disconnect
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className="btn danger small"
                  onClick={disconnect}
                  disabled={busy}
                >
                  Yes, disconnect
                </button>
                <button
                  type="button"
                  className="btn ghost small"
                  onClick={() => setConfirmDisconnect(false)}
                  disabled={busy}
                >
                  Cancel
                </button>
              </>
            )
          )}
        </div>
      </form>

      {msg && (
        <p className={msg.kind === "ok" ? "form-success" : "form-error"}
           style={{ marginTop: 12 }}>
          {msg.text}
        </p>
      )}
    </div>
  );
}

