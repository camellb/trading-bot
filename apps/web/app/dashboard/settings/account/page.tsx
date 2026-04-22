"use client";

import { useState } from "react";
import { useCredentials, type Credentials } from "../../../../lib/credentials";

export default function AccountPage() {
  const [name, setName] = useState("Alex Morgan");
  const [email, setEmail] = useState("alex@morgan.co");
  const [tz, setTz] = useState("America/New_York");
  const { creds, update, missing, canGoLive } = useCredentials();
  const [draft, setDraft] = useState<Credentials>(creds);
  const [reveal, setReveal] = useState(false);
  const [saved, setSaved] = useState(false);

  const setField = (k: keyof Credentials, v: string) => {
    setDraft((d) => ({ ...d, [k]: v }));
    setSaved(false);
  };
  const saveCreds = () => {
    update(draft);
    setSaved(true);
  };

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Profile</h2>
          <span className="panel-meta">How we address you</span>
        </div>

        <div className="form-row">
          <div className="form-field">
            <label>Display name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} />
            <div className="form-hint">Shown in the sidebar and on weekly review emails.</div>
          </div>

          <div className="form-field">
            <label>Email address</label>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
            <div className="form-hint">We'll send a confirmation link before any change takes effect.</div>
          </div>

          <div className="form-field">
            <label>Timezone</label>
            <select value={tz} onChange={(e) => setTz(e.target.value)}>
              <option value="America/New_York">America / New York (EST)</option>
              <option value="America/Los_Angeles">America / Los Angeles (PST)</option>
              <option value="Europe/London">Europe / London (GMT)</option>
              <option value="Europe/Berlin">Europe / Berlin (CET)</option>
              <option value="Asia/Singapore">Asia / Singapore (SGT)</option>
              <option value="Asia/Tokyo">Asia / Tokyo (JST)</option>
            </select>
            <div className="form-hint">Used for digest send times and the activity log.</div>
          </div>

          <div style={{ marginTop: 12 }}>
            <button className="btn-sm gold">Save profile</button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">API credentials</h2>
          <span className="panel-meta">
            {canGoLive ? "Ready for live trading" : `Missing: ${missing.join(", ")}`}
          </span>
        </div>

        <p className="panel-body" style={{ marginTop: 0, marginBottom: 18 }}>
          Delfi needs a Polymarket API key and a connected wallet address before it can place real
          trades. Telegram is optional — add it if you want push alerts on the same channel your
          daily digest uses. Keys are stored locally in your browser and sent encrypted to the Delfi
          trading engine over TLS.
        </p>

        <div className="form-row">
          <div className="form-field">
            <label>Polymarket API key <span style={{ color: "var(--gold-60)" }}>·required</span></label>
            <input
              type={reveal ? "text" : "password"}
              value={draft.polymarketApiKey}
              onChange={(e) => setField("polymarketApiKey", e.target.value)}
              placeholder="pk_live_…"
            />
            <div className="form-hint">Create in your Polymarket account under Settings → API.</div>
          </div>

          <div className="form-field">
            <label>Polymarket API secret <span style={{ color: "var(--gold-60)" }}>·required</span></label>
            <input
              type={reveal ? "text" : "password"}
              value={draft.polymarketApiSecret}
              onChange={(e) => setField("polymarketApiSecret", e.target.value)}
              placeholder="Shown once at creation"
            />
            <div className="form-hint">Pair of the API key. Stored only on your device.</div>
          </div>

          <div className="form-field">
            <label>Polymarket passphrase</label>
            <input
              type={reveal ? "text" : "password"}
              value={draft.polymarketPassphrase}
              onChange={(e) => setField("polymarketPassphrase", e.target.value)}
              placeholder="Optional"
            />
            <div className="form-hint">Only required if you set one when generating the key.</div>
          </div>

          <div className="form-field">
            <label>Wallet address <span style={{ color: "var(--gold-60)" }}>·required</span></label>
            <input
              value={draft.walletAddress}
              onChange={(e) => setField("walletAddress", e.target.value)}
              placeholder="0x…"
            />
            <div className="form-hint">Polygon address that will hold positions and receive fills.</div>
          </div>

          <div className="form-field">
            <label>Telegram bot token</label>
            <input
              type={reveal ? "text" : "password"}
              value={draft.telegramBotToken}
              onChange={(e) => setField("telegramBotToken", e.target.value)}
              placeholder="123456:ABC-…"
            />
            <div className="form-hint">Optional. Create a bot via @BotFather and paste its token.</div>
          </div>

          <div className="form-field">
            <label>Telegram chat ID</label>
            <input
              value={draft.telegramChatId}
              onChange={(e) => setField("telegramChatId", e.target.value)}
              placeholder="Optional"
            />
            <div className="form-hint">The chat where Delfi should post trade alerts and digests.</div>
          </div>

          <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
            <button className="btn-sm gold" onClick={saveCreds}>Save credentials</button>
            <button className="btn-sm" onClick={() => setReveal((r) => !r)}>
              {reveal ? "Hide values" : "Reveal values"}
            </button>
            {saved && (
              <span style={{ color: "var(--vellum-60)", fontSize: 13 }}>
                Saved locally.
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Close account</h2>
          <span className="panel-meta">Permanent</span>
        </div>
        <p className="panel-body">
          Closing your account ends your subscription and revokes the trading delegation. Open positions are
          closed back to your wallet first. Historical data is retained for record-keeping obligations as
          described in our privacy policy.
        </p>
        <div style={{ marginTop: 16 }}>
          <button className="btn-sm danger">Close account</button>
        </div>
      </div>
    </>
  );
}
