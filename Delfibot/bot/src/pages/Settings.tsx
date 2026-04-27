import { useEffect, useMemo, useState } from "react";
import {
  api,
  ArchetypeCatalogue,
  ArchetypeEntry,
  Credentials,
  NotificationsConfig,
  TelegramConfig,
} from "../api";

/**
 * Settings — the desktop equivalent of the SaaS settings stack.
 *
 * Architecture
 * ============
 * One sub-nav (Account / Connections / Notifications / Risk) and four
 * sub-pages, each with its own form state and save flow. Each form
 * submits independently and refreshes the parent on success so the
 * sidebar status pill stays coherent.
 *
 * Why one file
 * ============
 * The four sub-pages share data fetched from the parent (`creds`,
 * `config`) plus a few derived endpoints (telegram, notifications,
 * archetypes). Keeping them co-located avoids prop-drilling 5+ shared
 * helpers. Each section is small enough that one file is still
 * navigable.
 */

const BOUNDS = {
  base_stake_pct:        [0.005, 0.05] as const,
  max_stake_pct:         [0.01,  0.10] as const,
  daily_loss_limit_pct:  [0.01,  1.00] as const,
  weekly_loss_limit_pct: [0.01,  1.00] as const,
  drawdown_halt_pct:     [0.01,  1.00] as const,
  streak_cooldown_losses:[2,     10]   as const,
  dry_powder_reserve_pct:[0.10,  0.40] as const,
  starting_cash:         [10,    100_000] as const,
};

type ConfigShape = {
  base_stake_pct?: number;
  max_stake_pct?: number;
  daily_loss_limit_pct?: number;
  weekly_loss_limit_pct?: number;
  drawdown_halt_pct?: number;
  streak_cooldown_losses?: number;
  dry_powder_reserve_pct?: number;
  starting_cash?: number | null;
  archetype_skip_list?: string[];
  archetype_stake_multipliers?: Record<string, number>;
  [k: string]: unknown;
};

interface Props {
  creds: Credentials | null;
  config: ConfigShape | null;
  onSaved: () => void;
}

type Tab = "account" | "connections" | "notifications" | "risk";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "account",       label: "Account" },
  { id: "connections",   label: "Connections" },
  { id: "notifications", label: "Notifications" },
  { id: "risk",          label: "Risk and sizing" },
];

export default function Settings({ creds, config, onSaved }: Props) {
  const [tab, setTab] = useState<Tab>("account");

  return (
    <div>
      <div className="page-header">
        <h1>Settings</h1>
      </div>

      <div className="settings-grid">
        <nav className="settings-sidenav">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={tab === t.id ? "active" : ""}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div>
          {tab === "account" && (
            <AccountPanel config={config} onSaved={onSaved} />
          )}
          {tab === "connections" && (
            <ConnectionsPanel creds={creds} onSaved={onSaved} />
          )}
          {tab === "notifications" && <NotificationsPanel />}
          {tab === "risk" && (
            <RiskPanel config={config} onSaved={onSaved} />
          )}
        </div>
      </div>
    </div>
  );
}

// ── Account: bankroll + simulation reset ─────────────────────────────────

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
      setMsg({ kind: "ok", text: `Bankroll set to $${n.toFixed(2)}.` });
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
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <form className="settings-card" onSubmit={save}>
        <h2>Bankroll</h2>
        <p className="hint">
          The starting cash Delfi treats as 100% of bankroll. Stake size and
          circuit breakers are computed against this number. In simulation
          mode it is the synthetic balance; in live mode it is your seeded
          capital.
        </p>
        <label>
          Starting cash (USD)
          <input
            type="number"
            min={BOUNDS.starting_cash[0]}
            max={BOUNDS.starting_cash[1]}
            step="1"
            value={startingCash}
            onChange={(e) => setStartingCash(e.target.value)}
          />
        </label>
        <div className="form-actions">
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save bankroll"}
          </button>
          {msg && <span className={msg.kind}>{msg.text}</span>}
        </div>
      </form>

      <div className="settings-card">
        <h2>Simulation reset</h2>
        <p className="hint">
          Clears all simulation positions and resets the synthetic bankroll
          to your starting cash. Live trading is untouched. Use this when
          you want to re-test from a clean slate.
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
    </div>
  );
}

// ── Connections: keychain credentials ────────────────────────────────────

function ConnectionsPanel({
  creds,
  onSaved,
}: {
  creds: Credentials | null;
  onSaved: () => void;
}) {
  const [pmKey, setPmKey] = useState("");
  const [wallet, setWallet] = useState("");
  const [anthropic, setAnthropic] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (creds) setWallet(creds.wallet_address ?? "");
  }, [creds]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const payload: Record<string, string> = {};
      if (pmKey.trim())     payload.polymarket_private_key = pmKey.trim();
      if (wallet.trim())    payload.wallet_address = wallet.trim();
      if (anthropic.trim()) payload.anthropic_api_key = anthropic.trim();
      if (Object.keys(payload).length === 0) {
        setMsg({ kind: "err", text: "Nothing to save." });
        return;
      }
      const res = await api.saveCredentials(payload);
      setPmKey("");
      setAnthropic("");
      setMsg({
        kind: "ok",
        text: `Saved: ${res.wrote.join(", ") || "nothing"}.`,
      });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="settings-card" onSubmit={save}>
      <h2>Connections</h2>
      <p className="hint">
        Private keys are stored in your operating system keychain, never in
        plain text on disk. The wallet address is the public 0x address
        that pairs with your Polymarket private key. Leaving a key field
        blank keeps the existing value.
      </p>

      <label>
        Polymarket private key
        <input
          type="password"
          autoComplete="off"
          placeholder={creds?.has_polymarket_key ? "(stored)" : "0x..."}
          value={pmKey}
          onChange={(e) => setPmKey(e.target.value)}
        />
      </label>

      <label>
        Wallet address
        <input
          type="text"
          autoComplete="off"
          placeholder="0x..."
          value={wallet}
          onChange={(e) => setWallet(e.target.value)}
        />
      </label>

      <label>
        Anthropic API key
        <input
          type="password"
          autoComplete="off"
          placeholder={creds?.has_anthropic_key ? "(stored)" : "sk-ant-..."}
          value={anthropic}
          onChange={(e) => setAnthropic(e.target.value)}
        />
      </label>

      <div className="form-actions">
        <button type="submit" className="btn small" disabled={busy}>
          {busy ? "Saving..." : "Save credentials"}
        </button>
        {msg && <span className={msg.kind}>{msg.text}</span>}
      </div>
    </form>
  );
}

// ── Notifications: Telegram + per-category toggles ───────────────────────

const CATEGORY_LABELS: Record<string, { title: string; description: string }> = {
  position_opened: {
    title: "New positions",
    description:
      "Every time Delfi opens a position: market, side, stake, and forecast.",
  },
  position_settled: {
    title: "Position resolutions",
    description:
      "Every win or loss when a market resolves, with P&L and running bankroll.",
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
    description:
      "When Delfi proposes a strategy change, with evidence and inline controls.",
  },
  risk_event: {
    title: "Risk events",
    description:
      "Circuit breaker trips: daily loss cap, drawdown halt, or streak cooldown.",
  },
};

function NotificationsPanel() {
  const [tg, setTg] = useState<TelegramConfig | null>(null);
  const [notif, setNotif] = useState<NotificationsConfig | null>(null);
  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [prefSavingKey, setPrefSavingKey] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [t, n] = await Promise.all([api.telegram(), api.notifications()]);
        if (!cancelled) {
          setTg(t);
          setNotif(n);
          if (t.telegram_chat_id) setChatId(t.telegram_chat_id);
        }
      } catch (err) {
        if (!cancelled) {
          setMsg({
            kind: "err",
            text: err instanceof Error ? err.message : String(err),
          });
        }
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const saveTelegram = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const payload: { telegram_bot_token?: string | null; telegram_chat_id?: string | null } = {};
      if (botToken.trim()) payload.telegram_bot_token = botToken.trim();
      if (chatId.trim() !== (tg?.telegram_chat_id ?? "")) {
        payload.telegram_chat_id = chatId.trim() || null;
      }
      if (Object.keys(payload).length === 0) {
        setMsg({ kind: "err", text: "Nothing to save." });
        return;
      }
      const res = await api.saveTelegram(payload);
      setTg(res);
      setBotToken("");
      setMsg({ kind: "ok", text: "Saved." });
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
      const res = await api.saveTelegram({
        telegram_bot_token: null,
        telegram_chat_id: null,
      });
      setTg(res);
      setBotToken("");
      setChatId("");
      setMsg({ kind: "ok", text: "Disconnected." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    setMsg(null);
    try {
      const r = await api.testTelegram();
      setMsg({
        kind: r.ok ? "ok" : "err",
        text: r.detail || (r.ok ? "Test message sent." : "Test failed."),
      });
    } catch (err) {
      setMsg({
        kind: "err",
        text: humanizeTelegramError(err instanceof Error ? err.message : String(err)),
      });
    } finally {
      setTesting(false);
    }
  };

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
      setMsg({
        kind: "err",
        text: err instanceof Error ? err.message : String(err),
      });
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
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <form className="settings-card" onSubmit={saveTelegram}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
          }}
        >
          <h2>Telegram</h2>
          <span className="t-caption">
            {tg == null
              ? "Loading..."
              : tg.is_configured
              ? "Connected"
              : "Not connected"}
          </span>
        </div>
        <p className="hint">
          Delfi sends every new position, every resolution, and daily and
          weekly summaries straight to your Telegram. Create a bot with{" "}
          <code>@BotFather</code>, paste its token below, message the bot
          once, then grab your chat ID from{" "}
          <code>https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code>.
        </p>

        <label>
          Bot token
          <input
            type="password"
            autoComplete="off"
            placeholder={tg?.has_telegram_token ? "(stored)" : "123456:ABC-..."}
            value={botToken}
            onChange={(e) => setBotToken(e.target.value)}
          />
        </label>

        <label>
          Chat ID
          <input
            type="text"
            autoComplete="off"
            placeholder="e.g. 123456789"
            value={chatId}
            onChange={(e) => setChatId(e.target.value)}
          />
        </label>

        <div className="form-actions">
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save"}
          </button>
          {tg?.is_configured && (
            <>
              <button
                type="button"
                className="btn ghost small"
                onClick={sendTest}
                disabled={testing}
              >
                {testing ? "Sending..." : "Send test message"}
              </button>
              <button
                type="button"
                className="btn ghost small"
                onClick={disconnect}
                disabled={busy}
              >
                Disconnect
              </button>
            </>
          )}
          {msg && <span className={msg.kind}>{msg.text}</span>}
        </div>
      </form>

      <div className="settings-card">
        <h2>What Delfi will send</h2>
        <p className="hint">
          Toggle individual categories on or off. Changes apply immediately.
        </p>
        <div>
          {categories.map((key) => {
            const label = CATEGORY_LABELS[key] ?? {
              title: key,
              description: "",
            };
            return (
              <div key={key} className="notif-row">
                <div>
                  <div className="notif-name">{label.title}</div>
                  {label.description && (
                    <div className="notif-desc">{label.description}</div>
                  )}
                </div>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={isOn(key)}
                    disabled={prefSavingKey === key}
                    onChange={() => togglePref(key)}
                  />
                  <span className="slider" />
                </label>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function humanizeTelegramError(raw: string): string {
  const s = raw.toLowerCase();
  if (s.includes("chat not found")) {
    return "Telegram cannot find that chat. Open your bot in Telegram, tap Start (or send any message), then copy the chat ID again from https://api.telegram.org/bot<TOKEN>/getUpdates. Group chats use a negative ID.";
  }
  if (s.includes("unauthorized")) {
    return "Telegram rejected the bot token. Double-check you copied the full token from @BotFather (including the colon).";
  }
  if (s.includes("bot was blocked") || s.includes("blocked by the user")) {
    return "You have blocked this bot in Telegram. Unblock it and send /start, then retry.";
  }
  if (s.includes("forbidden")) {
    return "Telegram refused delivery. For groups, make sure the bot is a member. For personal chats, message the bot first.";
  }
  return raw;
}

// ── Risk and sizing: numeric form + per-archetype grid ───────────────────

function RiskPanel({
  config,
  onSaved,
}: {
  config: ConfigShape | null;
  onSaved: () => void;
}) {
  const [risk, setRisk] = useState({
    base_stake_pct: "",
    max_stake_pct: "",
    daily_loss_limit_pct: "",
    weekly_loss_limit_pct: "",
    drawdown_halt_pct: "",
    streak_cooldown_losses: "",
    dry_powder_reserve_pct: "",
  });
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!config) return;
    setRisk({
      base_stake_pct:         config.base_stake_pct         != null ? String(config.base_stake_pct)         : "",
      max_stake_pct:          config.max_stake_pct          != null ? String(config.max_stake_pct)          : "",
      daily_loss_limit_pct:   config.daily_loss_limit_pct   != null ? String(config.daily_loss_limit_pct)   : "",
      weekly_loss_limit_pct:  config.weekly_loss_limit_pct  != null ? String(config.weekly_loss_limit_pct)  : "",
      drawdown_halt_pct:      config.drawdown_halt_pct      != null ? String(config.drawdown_halt_pct)      : "",
      streak_cooldown_losses: config.streak_cooldown_losses != null ? String(config.streak_cooldown_losses) : "",
      dry_powder_reserve_pct: config.dry_powder_reserve_pct != null ? String(config.dry_powder_reserve_pct) : "",
    });
  }, [config]);

  const saveRisk = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const changes: Record<string, unknown> = {};
      const numericKeys = [
        "base_stake_pct",
        "max_stake_pct",
        "daily_loss_limit_pct",
        "weekly_loss_limit_pct",
        "drawdown_halt_pct",
        "dry_powder_reserve_pct",
      ] as const;
      for (const k of numericKeys) {
        const raw = risk[k].trim();
        if (raw === "") continue;
        const n = Number(raw);
        if (!Number.isFinite(n)) throw new Error(`${k} must be a number.`);
        const [lo, hi] = BOUNDS[k];
        if (n < lo || n > hi) throw new Error(`${k} must be between ${lo} and ${hi}.`);
        changes[k] = n;
      }
      const streakRaw = risk.streak_cooldown_losses.trim();
      if (streakRaw !== "") {
        const n = Number(streakRaw);
        if (!Number.isInteger(n)) throw new Error("Streak cooldown must be an integer.");
        const [lo, hi] = BOUNDS.streak_cooldown_losses;
        if (n < lo || n > hi) throw new Error(`Streak cooldown must be between ${lo} and ${hi}.`);
        changes.streak_cooldown_losses = n;
      }
      if (Object.keys(changes).length === 0) {
        setMsg({ kind: "err", text: "Nothing to save." });
        return;
      }
      await api.updateConfig(changes);
      setMsg({ kind: "ok", text: `Saved ${Object.keys(changes).length} field(s).` });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <form className="settings-card" onSubmit={saveRisk}>
        <h2>Risk and sizing</h2>
        <p className="hint">
          Sizing is flat: stake = bankroll * base stake * archetype
          multiplier, capped at max stake. Loss limits halt new trades when
          realized loss crosses the threshold. All values are fractions of
          bankroll (0.05 = 5%).
        </p>
        <div className="grid-2">
          <NumField
            label="Base stake (fraction)"
            step="0.001"
            range={BOUNDS.base_stake_pct}
            value={risk.base_stake_pct}
            onChange={(v) => setRisk({ ...risk, base_stake_pct: v })}
          />
          <NumField
            label="Max stake (fraction)"
            step="0.001"
            range={BOUNDS.max_stake_pct}
            value={risk.max_stake_pct}
            onChange={(v) => setRisk({ ...risk, max_stake_pct: v })}
          />
          <NumField
            label="Daily loss limit (fraction)"
            step="0.01"
            range={BOUNDS.daily_loss_limit_pct}
            value={risk.daily_loss_limit_pct}
            onChange={(v) => setRisk({ ...risk, daily_loss_limit_pct: v })}
          />
          <NumField
            label="Weekly loss limit (fraction)"
            step="0.01"
            range={BOUNDS.weekly_loss_limit_pct}
            value={risk.weekly_loss_limit_pct}
            onChange={(v) => setRisk({ ...risk, weekly_loss_limit_pct: v })}
          />
          <NumField
            label="Drawdown halt (fraction)"
            step="0.01"
            range={BOUNDS.drawdown_halt_pct}
            value={risk.drawdown_halt_pct}
            onChange={(v) => setRisk({ ...risk, drawdown_halt_pct: v })}
          />
          <NumField
            label="Streak cooldown (losses)"
            step="1"
            range={BOUNDS.streak_cooldown_losses}
            value={risk.streak_cooldown_losses}
            onChange={(v) => setRisk({ ...risk, streak_cooldown_losses: v })}
          />
          <NumField
            label="Dry powder reserve (fraction)"
            step="0.01"
            range={BOUNDS.dry_powder_reserve_pct}
            value={risk.dry_powder_reserve_pct}
            onChange={(v) => setRisk({ ...risk, dry_powder_reserve_pct: v })}
          />
        </div>
        <div className="form-actions">
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save risk and sizing"}
          </button>
          {msg && <span className={msg.kind}>{msg.text}</span>}
        </div>
      </form>

      <ArchetypePanel onSaved={onSaved} />
    </div>
  );
}

function NumField({
  label,
  step,
  range,
  value,
  onChange,
}: {
  label: string;
  step: string;
  range: readonly [number, number];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label>
      {label}
      <input
        type="number"
        step={step}
        min={range[0]}
        max={range[1]}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <span style={{ color: "var(--vellum-40)", fontSize: 11 }}>
        Range: {range[0]} - {range[1]}
      </span>
    </label>
  );
}

// ── Archetype grid ───────────────────────────────────────────────────────

function ArchetypePanel({ onSaved }: { onSaved: () => void }) {
  const [data, setData] = useState<ArchetypeCatalogue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await api.archetypes();
      setData(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    load();
  }, []);

  const groups = useMemo(() => {
    if (!data) return [] as Array<{ title: string; items: ArchetypeEntry[] }>;
    const sportIds = new Set([
      "tennis", "basketball", "baseball", "football", "hockey",
      "cricket", "esports", "soccer", "sports_other",
    ]);
    const marketIds = new Set([
      "price_threshold", "activity_count", "geopolitical_event", "binary_event",
    ]);
    const sports: ArchetypeEntry[] = [];
    const markets: ArchetypeEntry[] = [];
    const other:   ArchetypeEntry[] = [];
    for (const a of data.archetypes) {
      if (sportIds.has(a.id)) sports.push(a);
      else if (marketIds.has(a.id)) markets.push(a);
      else other.push(a);
    }
    const out: Array<{ title: string; items: ArchetypeEntry[] }> = [];
    if (sports.length)  out.push({ title: "Sports",  items: sports  });
    if (markets.length) out.push({ title: "Markets", items: markets });
    if (other.length)   out.push({ title: "Other",   items: other   });
    return out;
  }, [data]);

  const update = async (
    a: ArchetypeEntry,
    patch: Partial<Pick<ArchetypeEntry, "skip" | "multiplier">>,
  ) => {
    if (!data || busyId) return;
    setBusyId(a.id);
    setError(null);

    // Build the next config delta in one shot. The bot's API accepts
    // archetype_skip_list (full replacement) and
    // archetype_stake_multipliers (full replacement). We send both so the
    // user's intent is unambiguous.
    const nextSkip = new Set(
      data.archetypes
        .filter((x) => (x.id === a.id ? (patch.skip ?? x.skip) : x.skip))
        .map((x) => x.id),
    );
    const nextMults: Record<string, number> = {};
    for (const x of data.archetypes) {
      const m = x.id === a.id ? (patch.multiplier ?? x.multiplier) : x.multiplier;
      // Only persist non-default values to keep the config compact.
      if (Math.abs(m - x.default_mult) > 1e-6) {
        nextMults[x.id] = m;
      }
    }

    try {
      await api.updateConfig({
        archetype_skip_list: Array.from(nextSkip),
        archetype_stake_multipliers: nextMults,
      });
      await load();
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  const reset = (a: ArchetypeEntry) => {
    void update(a, { skip: a.default_skip, multiplier: a.default_mult });
  };

  if (!data) {
    return (
      <div className="settings-card">
        <h2>Archetypes</h2>
        <p className="empty">Loading archetypes...</p>
        {error && <div className="error">{error}</div>}
      </div>
    );
  }

  const { multiplier_min, multiplier_max } = data.bounds;

  return (
    <div className="settings-card">
      <h2>Archetypes</h2>
      <p className="hint">
        Each market Delfi looks at is classified into one archetype. Skip an
        archetype to ignore those markets entirely. Use the multiplier to
        size up or down without skipping. Default for unknown archetypes is
        1.0 (full stake).
      </p>

      {error && <div className="error">{error}</div>}

      {groups.map((g) => (
        <div key={g.title} style={{ marginTop: 8 }}>
          <h3 className="t-caption" style={{ margin: "0 0 8px", color: "var(--vellum-60)" }}>
            {g.title}
          </h3>
          <div className="archetype-grid">
            {g.items.map((a) => (
              <ArchetypeCard
                key={a.id}
                a={a}
                busy={busyId === a.id}
                multMin={multiplier_min}
                multMax={multiplier_max}
                onToggleSkip={() => update(a, { skip: !a.skip })}
                onMultChange={(m) => update(a, { multiplier: m })}
                onReset={() => reset(a)}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ArchetypeCard({
  a,
  busy,
  multMin,
  multMax,
  onToggleSkip,
  onMultChange,
  onReset,
}: {
  a: ArchetypeEntry;
  busy: boolean;
  multMin: number;
  multMax: number;
  onToggleSkip: () => void;
  onMultChange: (m: number) => void;
  onReset: () => void;
}) {
  // Local slider state for instant feedback; commits on mouseup via onChange.
  const [pending, setPending] = useState<number | null>(null);
  const shown = pending ?? a.multiplier;
  const isDefault =
    a.skip === a.default_skip && Math.abs(a.multiplier - a.default_mult) < 1e-6;

  return (
    <div className={`archetype-card ${a.skip ? "skipped" : ""}`}>
      <div>
        <div className="archetype-name">{a.label}</div>
        <div className="archetype-desc">{a.description}</div>
      </div>

      <div className="archetype-controls">
        <span
          style={{
            fontSize: 11,
            color: "var(--vellum-60)",
            letterSpacing: "0.1em",
            textTransform: "uppercase",
          }}
        >
          {a.skip ? "Skip" : "Trade"}
        </span>
        <label className="toggle">
          <input
            type="checkbox"
            checked={!a.skip}
            disabled={busy}
            onChange={onToggleSkip}
          />
          <span className="slider" />
        </label>
      </div>

      <div className="archetype-mult">
        <span className="archetype-mult-label">Stake mult</span>
        <input
          type="range"
          className="range"
          min={multMin}
          max={multMax}
          step="0.05"
          value={shown}
          disabled={busy || a.skip}
          onChange={(e) => setPending(Number(e.target.value))}
          onMouseUp={(e) => {
            const v = Number((e.target as HTMLInputElement).value);
            setPending(null);
            if (Math.abs(v - a.multiplier) > 1e-6) onMultChange(v);
          }}
          onTouchEnd={(e) => {
            const v = Number((e.target as HTMLInputElement).value);
            setPending(null);
            if (Math.abs(v - a.multiplier) > 1e-6) onMultChange(v);
          }}
          style={{ flex: 1 }}
        />
        <span className="archetype-mult-value">{shown.toFixed(2)}x</span>
        {!isDefault && (
          <button
            type="button"
            className="archetype-mult-default"
            onClick={onReset}
            disabled={busy}
            title={`Default: ${a.default_skip ? "skip" : "trade"} at ${a.default_mult}x`}
          >
            Reset
          </button>
        )}
      </div>
    </div>
  );
}
