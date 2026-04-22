"use client";

import { useEffect, useState } from "react";

type Prefs = {
  dailyDigest: boolean;
  weeklyReview: boolean;
  bigWins: boolean;
  bigLosses: boolean;
  riskEvents: boolean;
  calibrationSuggestions: boolean;
  productUpdates: boolean;
  pushTrades: boolean;
  pushRisk: boolean;
};

const DEFAULTS: Prefs = {
  dailyDigest: true,
  weeklyReview: true,
  bigWins: false,
  bigLosses: true,
  riskEvents: true,
  calibrationSuggestions: true,
  productUpdates: false,
  pushTrades: false,
  pushRisk: true,
};

export default function NotificationsPage() {
  const [p, setP] = useState<Prefs>(DEFAULTS);
  const t = (k: keyof Prefs) => setP((prev) => ({ ...prev, [k]: !prev[k] }));

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Email</h2>
          <span className="panel-meta">What lands in your inbox</span>
        </div>

        <Toggle label="Daily digest" desc="A short morning summary of yesterday's trades and today's focus." on={p.dailyDigest} onChange={() => t("dailyDigest")} />
        <Toggle label="Weekly review" desc="Every Sunday evening. Performance, calibration, suggestions." on={p.weeklyReview} onChange={() => t("weeklyReview")} />
        <Toggle label="Meaningful wins" desc="Trades resolving above +$100 P&L get a one-line email with reasoning." on={p.bigWins} onChange={() => t("bigWins")} />
        <Toggle label="Meaningful losses" desc="Trades resolving below -$100 P&L get an honest post-mortem email." on={p.bigLosses} onChange={() => t("bigLosses")} />
        <Toggle label="Risk events" desc="Daily cap, drawdown halt, or streak cooldown triggered." on={p.riskEvents} onChange={() => t("riskEvents")} />
        <Toggle label="Calibration suggestions" desc="Delfi found a pattern worth a config change, with backtest evidence." on={p.calibrationSuggestions} onChange={() => t("calibrationSuggestions")} />
        <Toggle label="Product updates" desc="Occasional emails about new features and strategies. Not marketing." on={p.productUpdates} onChange={() => t("productUpdates")} />
      </div>

      <TelegramSection />

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Push notifications</h2>
          <span className="panel-meta">Mobile and browser</span>
        </div>
        <Toggle label="Each trade" desc="Push a notification when Delfi opens or closes a position." on={p.pushTrades} onChange={() => t("pushTrades")} />
        <Toggle label="Risk events" desc="Push when a risk guardrail engages." on={p.pushRisk} onChange={() => t("pushRisk")} />

        <div style={{ marginTop: 16 }}>
          <button className="btn-sm">Enable browser push</button>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Quiet hours</h2>
          <span className="panel-meta">No notifications during these hours</span>
        </div>
        <p className="panel-body">
          Daily digest and weekly review still arrive on schedule, but push notifications and loss post-mortems
          will hold until your quiet hours end.
        </p>
        <div className="form-row" style={{ flexDirection: "row", gap: 16, maxWidth: "100%" }}>
          <div className="form-field" style={{ flex: 1 }}>
            <label>From</label>
            <input type="time" defaultValue="22:00" />
          </div>
          <div className="form-field" style={{ flex: 1 }}>
            <label>To</label>
            <input type="time" defaultValue="07:00" />
          </div>
        </div>
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 12 }}>
        <button className="btn-sm">Cancel</button>
        <button className="btn-sm gold">Save preferences</button>
      </div>
    </>
  );
}

function TelegramSection() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/config/telegram", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => setConfigured(Boolean(d?.configured)))
      .catch(() => setConfigured(false));
  }, []);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setStatus(null);
    try {
      const res = await fetch("/api/config/telegram", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_token: botToken.trim() || null,
          chat_id:   chatId.trim()   || null,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
      setConfigured(Boolean(data?.configured));
      setBotToken("");
      setChatId("");
      setStatus(data?.configured ? "Saved — Telegram alerts enabled." : "Cleared — Telegram alerts disabled.");
    } catch (err) {
      setStatus(`Failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }

  async function clearCreds() {
    setSaving(true);
    setStatus(null);
    try {
      const res = await fetch("/api/config/telegram", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bot_token: null, chat_id: null }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
      setConfigured(false);
      setStatus("Cleared — Telegram alerts disabled.");
    } catch (err) {
      setStatus(`Failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Telegram</h2>
        <span className="panel-meta">
          {configured === null ? "Checking…" : configured ? "Connected" : "Not connected"}
        </span>
      </div>
      <p className="panel-body">
        Paste your own bot token and chat ID to receive trade, risk, and
        summary alerts. Create a bot with <code>@BotFather</code>, then message
        it once and grab your chat ID from{" "}
        <code>https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code>.
        Leave blank to disable.
      </p>
      <form onSubmit={save} className="form-row" style={{ gap: 12 }}>
        <div className="form-field">
          <label>Bot token</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={configured ? "•••••••• (saved — leave blank to keep)" : "123456:ABC-DEF…"}
            value={botToken}
            onChange={(e) => setBotToken(e.target.value)}
          />
        </div>
        <div className="form-field">
          <label>Chat ID</label>
          <input
            type="text"
            autoComplete="off"
            placeholder={configured ? "•••••••• (saved — leave blank to keep)" : "e.g. 123456789"}
            value={chatId}
            onChange={(e) => setChatId(e.target.value)}
          />
        </div>
        <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 8 }}>
          <button type="submit" className="btn-sm gold" disabled={saving || (!botToken.trim() && !chatId.trim())}>
            {saving ? "Saving…" : "Save credentials"}
          </button>
          {configured && (
            <button type="button" className="btn-sm" onClick={clearCreds} disabled={saving}>
              Disconnect
            </button>
          )}
          {status && <span className="panel-meta">{status}</span>}
        </div>
      </form>
    </div>
  );
}

function Toggle({ label, desc, on, onChange }: { label: string; desc: string; on: boolean; onChange: () => void }) {
  return (
    <div className="split-row">
      <div className="split-body">
        <div className="split-title">{label}</div>
        <div className="split-desc">{desc}</div>
      </div>
      <div className="split-right">
        <label className="toggle-switch">
          <input type="checkbox" checked={on} onChange={onChange} />
          <span className="toggle-slider"></span>
        </label>
      </div>
    </div>
  );
}
