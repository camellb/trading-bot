import { useEffect, useMemo, useState } from "react";
import {
  api,
  ArchetypeCatalogue,
  ArchetypeEntry,
  Credentials,
  NotificationsConfig,
} from "../api";
import type { SettingsTab } from "../App";

/**
 * Settings - SaaS-parity layout, with desktop additions:
 *   - Per-archetype grid (vs the SaaS JSON editor)
 *   - Simulation reset (desktop-only)
 *
 * The active tab is owned by App and surfaced via the sidebar sub-nav.
 * This page renders one panel at a time. Each form submits independently
 * and refreshes the parent on success.
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
  tab: SettingsTab;
  setTab: (t: SettingsTab) => void;
  creds: Credentials | null;
  config: ConfigShape | null;
  onSaved: () => void;
}

const TITLES: Record<SettingsTab, { h1: string; sub: string }> = {
  account: {
    h1: "Account",
    sub: "Bankroll and starting capital. The number Delfi treats as 100% of your trading capital.",
  },
  connections: {
    h1: "Connections",
    sub: "Polymarket private key, wallet address, and Anthropic API key. Stored in your OS keychain, never on disk.",
  },
  risk: {
    h1: "Risk and sizing",
    sub: "Stake size, loss limits, and per-archetype multipliers. Applied immediately.",
  },
  notifications: {
    h1: "Notifications",
    sub: "Per-category toggles for what Delfi surfaces in the dashboard activity feed.",
  },
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
            <p className="page-sub">{t.sub}</p>
          </div>
        </div>
      </div>

      {tab === "account"       && <AccountPanel       config={config} onSaved={onSaved} />}
      {tab === "connections"   && <ConnectionsPanel   creds={creds}   onSaved={onSaved} />}
      {tab === "risk"          && <RiskPanel          config={config} onSaved={onSaved} />}
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
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Bankroll</h2>
        </div>
        <p className="page-sub" style={{ marginBottom: 16 }}>
          The starting cash Delfi treats as 100% of bankroll. Stake size and
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
              {busy ? "Saving..." : "Save bankroll"}
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
          Clears all simulation positions and resets the synthetic bankroll
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
    </>
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
            placeholder={hasLlm ? "(stored)" : "sk-ant-..."}
            value={llmKey}
            onChange={(e) => setLlmKey(e.target.value)}
          />
          <p className="form-hint">
            The model that reads each Polymarket market and produces Delfi&apos;s
            forecast. Without this, Delfi can&apos;t decide whether to trade.
            Recommended: Claude (Anthropic). OpenAI / ChatGPT support is on
            the roadmap; the field accepts that key today and stores it for
            the multi-provider rollout. Get a Claude key at console.anthropic.com.
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
    description: "Every win or loss when a market resolves, with P&L and running bankroll.",
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

// ── Risk + sizing ────────────────────────────────────────────────────────

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
        "base_stake_pct", "max_stake_pct", "daily_loss_limit_pct",
        "weekly_loss_limit_pct", "drawdown_halt_pct", "dry_powder_reserve_pct",
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
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Sizing and limits</h2>
          <span className="panel-meta">Fractions of bankroll</span>
        </div>
        <p className="page-sub" style={{ marginBottom: 16 }}>
          Stake = bankroll × base stake × archetype multiplier, capped at
          max stake. Loss limits halt new trades when realized loss crosses
          the threshold.
        </p>
        <form onSubmit={saveRisk}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, maxWidth: 720 }}>
            <NumField
              label="Base stake (fraction)" step="0.001"
              range={BOUNDS.base_stake_pct}
              value={risk.base_stake_pct}
              onChange={(v) => setRisk({ ...risk, base_stake_pct: v })}
            />
            <NumField
              label="Max stake (fraction)" step="0.001"
              range={BOUNDS.max_stake_pct}
              value={risk.max_stake_pct}
              onChange={(v) => setRisk({ ...risk, max_stake_pct: v })}
            />
            <NumField
              label="Daily loss limit" step="0.01"
              range={BOUNDS.daily_loss_limit_pct}
              value={risk.daily_loss_limit_pct}
              onChange={(v) => setRisk({ ...risk, daily_loss_limit_pct: v })}
            />
            <NumField
              label="Weekly loss limit" step="0.01"
              range={BOUNDS.weekly_loss_limit_pct}
              value={risk.weekly_loss_limit_pct}
              onChange={(v) => setRisk({ ...risk, weekly_loss_limit_pct: v })}
            />
            <NumField
              label="Drawdown halt" step="0.01"
              range={BOUNDS.drawdown_halt_pct}
              value={risk.drawdown_halt_pct}
              onChange={(v) => setRisk({ ...risk, drawdown_halt_pct: v })}
            />
            <NumField
              label="Streak cooldown (losses)" step="1"
              range={BOUNDS.streak_cooldown_losses}
              value={risk.streak_cooldown_losses}
              onChange={(v) => setRisk({ ...risk, streak_cooldown_losses: v })}
            />
            <NumField
              label="Dry powder reserve" step="0.01"
              range={BOUNDS.dry_powder_reserve_pct}
              value={risk.dry_powder_reserve_pct}
              onChange={(v) => setRisk({ ...risk, dry_powder_reserve_pct: v })}
            />
          </div>
          <div className="form-actions" style={{ marginTop: 18 }}>
            <button type="submit" className="btn small" disabled={busy}>
              {busy ? "Saving..." : "Save risk and sizing"}
            </button>
            {msg && (
              <span className={msg.kind === "ok" ? "form-success" : "form-error"}>
                {msg.text}
              </span>
            )}
          </div>
        </form>
      </div>

      <ArchetypePanel onSaved={onSaved} />
    </>
  );
}

function NumField({
  label, step, range, value, onChange,
}: {
  label: string;
  step: string;
  range: readonly [number, number];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="form-field">
      <label>{label}</label>
      <input
        type="number"
        step={step}
        min={range[0]}
        max={range[1]}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className="form-hint">Range: {range[0]} - {range[1]}</span>
    </div>
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

  useEffect(() => { load(); }, []);

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

    const nextSkip = new Set(
      data.archetypes
        .filter((x) => (x.id === a.id ? (patch.skip ?? x.skip) : x.skip))
        .map((x) => x.id),
    );
    const nextMults: Record<string, number> = {};
    for (const x of data.archetypes) {
      const m = x.id === a.id ? (patch.multiplier ?? x.multiplier) : x.multiplier;
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
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Archetypes</h2>
        </div>
        <div className="empty-state">Loading archetypes...</div>
        {error && <div className="error">{error}</div>}
      </div>
    );
  }

  const { multiplier_min, multiplier_max } = data.bounds;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Archetypes</h2>
        <span className="panel-meta">{data.archetypes.length} categories</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Each market Delfi looks at is classified into one archetype. Skip an
        archetype to ignore those markets entirely. Use the multiplier to
        size up or down without skipping. Default for unknown archetypes is 1.0×.
      </p>

      {error && <div className="error">{error}</div>}

      {groups.map((g) => (
        <div key={g.title} style={{ marginTop: 16 }}>
          <h3 className="t-caption" style={{ margin: "0 0 8px" }}>{g.title}</h3>
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
  a, busy, multMin, multMax, onToggleSkip, onMultChange, onReset,
}: {
  a: ArchetypeEntry;
  busy: boolean;
  multMin: number;
  multMax: number;
  onToggleSkip: () => void;
  onMultChange: (m: number) => void;
  onReset: () => void;
}) {
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
        <span style={{
          fontSize: 11, color: "var(--vellum-60)",
          letterSpacing: "0.1em", textTransform: "uppercase",
        }}>
          {a.skip ? "Skip" : "Trade"}
        </span>
        <label className="toggle-switch">
          <input
            type="checkbox"
            checked={!a.skip}
            disabled={busy}
            onChange={onToggleSkip}
          />
          <span className="toggle-slider" />
        </label>
      </div>

      <div className="archetype-mult">
        <span className="archetype-mult-label">Stake mult</span>
        <input
          type="range"
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
        />
        <span className="archetype-mult-value">{shown.toFixed(2)}×</span>
        {!isDefault && (
          <button
            type="button"
            className="archetype-mult-default"
            onClick={onReset}
            disabled={busy}
            title={`Default: ${a.default_skip ? "skip" : "trade"} at ${a.default_mult}×`}
          >
            Reset
          </button>
        )}
      </div>
    </div>
  );
}
