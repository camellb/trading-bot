"use client";

import { useEffect, useState } from "react";
import { useCredentials, type Credentials } from "../../../../lib/credentials";

type Profile = { email: string; displayName: string };

export default function AccountPage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [nameSaving, setNameSaving] = useState(false);
  const [nameSavedAt, setNameSavedAt] = useState<number | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);

  const { creds, update, missing, canGoLive } = useCredentials();
  const [draft, setDraft] = useState<Credentials>(creds);
  const [reveal, setReveal] = useState(false);
  const [credsSaved, setCredsSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/profile", { cache: "no-store" });
        if (!r.ok) return;
        const j = (await r.json()) as Profile;
        if (cancelled) return;
        setProfile(j);
        setNameDraft(j.displayName ?? "");
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const setField = (k: keyof Credentials, v: string) => {
    setDraft((d) => ({ ...d, [k]: v }));
    setCredsSaved(false);
  };
  const saveCreds = () => {
    update(draft);
    setCredsSaved(true);
  };

  const saveName = async () => {
    setNameError(null);
    setNameSaving(true);
    try {
      const trimmed = nameDraft.trim();
      if (trimmed.length < 2) {
        setNameError("Name must be at least 2 characters.");
        return;
      }
      const r = await fetch("/api/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ displayName: trimmed }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setNameError(body?.error ?? "Couldn't save — try again.");
        return;
      }
      setProfile((p) => (p ? { ...p, displayName: trimmed } : p));
      setNameSavedAt(Date.now());
    } finally {
      setNameSaving(false);
    }
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
            <input
              value={nameDraft}
              onChange={(e) => {
                setNameDraft(e.target.value);
                setNameSavedAt(null);
                setNameError(null);
              }}
              placeholder={profile ? "" : "Loading…"}
              disabled={!profile}
            />
            <div className="form-hint">Shown in the sidebar and on weekly review emails.</div>
          </div>

          <div className="form-field">
            <label>Email address</label>
            <input
              type="email"
              value={profile?.email ?? ""}
              readOnly
              disabled
              placeholder={profile ? "" : "Loading…"}
            />
            <div className="form-hint">
              Your sign-in email. To change it, contact support.
            </div>
          </div>

          <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
            <button
              className="btn-sm gold"
              onClick={saveName}
              disabled={!profile || nameSaving || nameDraft.trim() === (profile?.displayName ?? "")}
            >
              {nameSaving ? "Saving…" : "Save profile"}
            </button>
            {nameSavedAt && !nameError && (
              <span style={{ color: "var(--vellum-60)", fontSize: 13 }}>Saved.</span>
            )}
            {nameError && (
              <span style={{ color: "var(--red)", fontSize: 13 }}>{nameError}</span>
            )}
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
            {credsSaved && (
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
