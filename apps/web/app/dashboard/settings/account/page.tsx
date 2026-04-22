"use client";

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  usePolymarketCredentials,
  useTelegramCredentials,
  EMPTY_POLYMARKET,
  EMPTY_TELEGRAM,
  type PolymarketCreds,
  type TelegramCreds,
} from "../../../../lib/credentials";

type Profile = { email: string; displayName: string };

export default function AccountPage() {
  const params = useSearchParams();
  const setupFlag = params?.get("setup");

  const [profile, setProfile] = useState<Profile | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [nameSaving, setNameSaving] = useState(false);
  const [nameSavedAt, setNameSavedAt] = useState<number | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);

  const poly = usePolymarketCredentials();
  const [polyDraft, setPolyDraft] = useState<PolymarketCreds>({
    ...EMPTY_POLYMARKET,
  });
  const [polyReveal, setPolyReveal] = useState(false);
  const [polySavedAt, setPolySavedAt] = useState<number | null>(null);

  const telegram = useTelegramCredentials();
  const [tgDraft, setTgDraft] = useState<TelegramCreds>({ ...EMPTY_TELEGRAM });
  const [tgReveal, setTgReveal] = useState(false);
  const [tgSavedAt, setTgSavedAt] = useState<number | null>(null);

  // Once server state hydrates, pre-fill non-sensitive fields only.
  useEffect(() => {
    if (poly.hydrated) {
      setPolyDraft((d) => ({ ...d, walletAddress: poly.status.walletAddress }));
    }
  }, [poly.hydrated, poly.status.walletAddress]);

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

  const savePoly = async () => {
    const ok = await poly.save(polyDraft);
    if (ok) {
      // Secret fields are never echoed back from the server — clear the
      // local draft after a successful save so the form doesn't keep them
      // in component state. Wallet stays (it's non-sensitive).
      setPolyDraft((d) => ({
        ...d,
        apiKey: "",
        apiSecret: "",
        passphrase: "",
      }));
      setPolySavedAt(Date.now());
    }
  };

  const saveTelegram = async () => {
    const ok = await telegram.save(tgDraft);
    if (ok) {
      setTgDraft({ botToken: "", chatId: "" });
      setTgSavedAt(Date.now());
    }
  };

  const setupBanner = useMemo(() => {
    if (setupFlag !== "live") return null;
    if (poly.canGoLive) return null;
    return (
      <div className="panel" style={{ borderColor: "var(--gold-60)" }}>
        <div className="panel-head">
          <h2 className="panel-title">One more step — add your Polymarket keys</h2>
          <span className="panel-meta">Live mode blocked until set</span>
        </div>
        <p className="panel-body">
          You picked live trading during onboarding. Delfi needs a Polymarket API
          key, API secret, and wallet address before it can place real trades.
          Add them below — they are stored encrypted in your account and never
          shared.
        </p>
      </div>
    );
  }, [setupFlag, poly.canGoLive]);

  return (
    <>
      {setupBanner}

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
            <div className="form-hint">Your sign-in email. To change it, contact support.</div>
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
          <h2 className="panel-title">Polymarket credentials</h2>
          <span className="panel-meta">
            {poly.canGoLive ? "Ready for live trading" : `Missing: ${poly.missing.join(", ")}`}
          </span>
        </div>

        <p className="panel-body" style={{ marginTop: 0, marginBottom: 18 }}>
          Delfi needs a Polymarket API key, secret, and wallet address to place real trades.
          Keys are stored encrypted in your Delfi account and are only decrypted by the trading
          engine when sizing or settling a trade. We never show secrets back — leave a field
          blank to keep the value we already have on file; type a new value to replace it; type
          a single space to clear it.
        </p>

        <div className="form-row">
          <div className="form-field">
            <label>
              Polymarket API key <span style={{ color: "var(--gold-60)" }}>·required</span>
              {poly.status.apiKeySet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={polyReveal ? "text" : "password"}
              value={polyDraft.apiKey}
              onChange={(e) => {
                setPolyDraft((d) => ({ ...d, apiKey: e.target.value }));
                setPolySavedAt(null);
              }}
              placeholder={poly.status.apiKeySet ? "••••••••" : "pk_live_…"}
            />
            <div className="form-hint">Create in your Polymarket account under Settings → API.</div>
          </div>

          <div className="form-field">
            <label>
              Polymarket API secret <span style={{ color: "var(--gold-60)" }}>·required</span>
              {poly.status.apiSecretSet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={polyReveal ? "text" : "password"}
              value={polyDraft.apiSecret}
              onChange={(e) => {
                setPolyDraft((d) => ({ ...d, apiSecret: e.target.value }));
                setPolySavedAt(null);
              }}
              placeholder={poly.status.apiSecretSet ? "••••••••" : "Shown once at creation"}
            />
            <div className="form-hint">Pair of the API key.</div>
          </div>

          <div className="form-field">
            <label>
              Polymarket passphrase
              {poly.status.passphraseSet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={polyReveal ? "text" : "password"}
              value={polyDraft.passphrase}
              onChange={(e) => {
                setPolyDraft((d) => ({ ...d, passphrase: e.target.value }));
                setPolySavedAt(null);
              }}
              placeholder="Optional"
            />
            <div className="form-hint">Only required if you set one when generating the key.</div>
          </div>

          <div className="form-field">
            <label>Wallet address <span style={{ color: "var(--gold-60)" }}>·required</span></label>
            <input
              value={polyDraft.walletAddress}
              onChange={(e) => {
                setPolyDraft((d) => ({ ...d, walletAddress: e.target.value }));
                setPolySavedAt(null);
              }}
              placeholder="0x…"
            />
            <div className="form-hint">Polygon address that will hold positions and receive fills.</div>
          </div>

          <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
            <button
              className="btn-sm gold"
              onClick={savePoly}
              disabled={poly.saving}
            >
              {poly.saving ? "Saving…" : "Save credentials"}
            </button>
            <button className="btn-sm" onClick={() => setPolyReveal((r) => !r)}>
              {polyReveal ? "Hide values" : "Reveal values"}
            </button>
            {polySavedAt && !poly.error && (
              <span style={{ color: "var(--vellum-60)", fontSize: 13 }}>Saved.</span>
            )}
            {poly.error && (
              <span style={{ color: "var(--red)", fontSize: 13 }}>{poly.error}</span>
            )}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Telegram alerts</h2>
          <span className="panel-meta">
            {telegram.configured ? "Configured" : "Optional — not set"}
          </span>
        </div>

        <p className="panel-body" style={{ marginTop: 0, marginBottom: 18 }}>
          Push trade alerts, settlements, and daily/weekly summaries to your own Telegram bot.
          Optional. Create a bot via @BotFather, grab its token, and paste the chat ID of the
          channel you want Delfi to post into.
        </p>

        <div className="form-row">
          <div className="form-field">
            <label>
              Telegram bot token
              {telegram.configured && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={tgReveal ? "text" : "password"}
              value={tgDraft.botToken}
              onChange={(e) => {
                setTgDraft((d) => ({ ...d, botToken: e.target.value }));
                setTgSavedAt(null);
              }}
              placeholder={telegram.configured ? "••••••••" : "123456:ABC-…"}
            />
          </div>

          <div className="form-field">
            <label>Telegram chat ID</label>
            <input
              value={tgDraft.chatId}
              onChange={(e) => {
                setTgDraft((d) => ({ ...d, chatId: e.target.value }));
                setTgSavedAt(null);
              }}
              placeholder={telegram.configured ? "••••••••" : "e.g. -1001234567890"}
            />
          </div>

          <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
            <button
              className="btn-sm gold"
              onClick={saveTelegram}
              disabled={telegram.saving}
            >
              {telegram.saving ? "Saving…" : "Save Telegram"}
            </button>
            <button className="btn-sm" onClick={() => setTgReveal((r) => !r)}>
              {tgReveal ? "Hide values" : "Reveal values"}
            </button>
            {tgSavedAt && !telegram.error && (
              <span style={{ color: "var(--vellum-60)", fontSize: 13 }}>Saved.</span>
            )}
            {telegram.error && (
              <span style={{ color: "var(--red)", fontSize: 13 }}>{telegram.error}</span>
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
