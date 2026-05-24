"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  useConnections,
  EMPTY_OFFSHORE,
  EMPTY_US,
  venueLabel,
  type OffshoreCreds,
  type UsCreds,
  type Venue,
} from "../../../../lib/credentials";

// The Connections page is where a user picks their venue (offshore
// Polymarket vs Polymarket US) and fills in the API credentials for
// whichever side they trade on. The onboarding flow already writes the
// initial venue to user_config; this page lets them switch and manage
// keys for both sides independently so switching doesn't wipe the other
// side's creds.

export default function ConnectionsPage() {
  return (
    <Suspense fallback={null}>
      <ConnectionsPageInner />
    </Suspense>
  );
}

function ConnectionsPageInner() {
  const params = useSearchParams();
  const setupFlag = params?.get("setup");

  const conn = useConnections();

  const [offDraft, setOffDraft] = useState<OffshoreCreds>({ ...EMPTY_OFFSHORE });
  const [usDraft,  setUsDraft]  = useState<UsCreds>({ ...EMPTY_US });
  const [offReveal, setOffReveal] = useState(false);
  const [usReveal,  setUsReveal]  = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Pre-fill non-sensitive fields once the server state hydrates. Wallet
  // is non-sensitive; keys/secrets/passphrases are never echoed back.
  useEffect(() => {
    if (conn.hydrated) {
      setOffDraft((d) => ({
        ...d,
        walletAddress: conn.status.polymarket.walletAddress,
      }));
    }
  }, [conn.hydrated, conn.status.polymarket.walletAddress]);

  const activeVenue = conn.status.venue;

  const switchVenue = async (venue: Venue) => {
    if (venue === activeVenue) return;
    setSavedAt(null);
    const ok = await conn.save({ venue });
    if (ok) setSavedAt(Date.now());
  };

  const saveOffshore = async () => {
    setSavedAt(null);
    const ok = await conn.save({
      polymarket: {
        apiKey: offDraft.apiKey,
        apiSecret: offDraft.apiSecret,
        passphrase: offDraft.passphrase,
        walletAddress: offDraft.walletAddress,
      },
    });
    if (ok) {
      // Clear secret draft fields after save - the server never echoes
      // them back so keeping them in React state would be deceptive.
      // Wallet stays (non-sensitive).
      setOffDraft((d) => ({
        ...d,
        apiKey: "",
        apiSecret: "",
        passphrase: "",
      }));
      setSavedAt(Date.now());
    }
  };

  const saveUs = async () => {
    setSavedAt(null);
    const ok = await conn.save({
      polymarketUs: {
        apiKey: usDraft.apiKey,
        apiSecret: usDraft.apiSecret,
        passphrase: usDraft.passphrase,
      },
    });
    if (ok) {
      setUsDraft({ apiKey: "", apiSecret: "", passphrase: "" });
      setSavedAt(Date.now());
    }
  };

  const setupBanner = useMemo(() => {
    if (setupFlag !== "live") return null;
    if (conn.canGoLive) return null;
    const label = venueLabel(activeVenue);
    return (
      <div className="panel" style={{ borderColor: "var(--gold-60)" }}>
        <div className="panel-head">
          <h2 className="panel-title">One more step - connect {label}</h2>
          <span className="panel-meta">Live mode blocked until set</span>
        </div>
        <p className="panel-body">
          You picked live trading during onboarding. Delfi needs {label} API
          credentials before it can place real trades. Fill them in below -
          they are stored encrypted in your account and never shared.
        </p>
      </div>
    );
  }, [setupFlag, conn.canGoLive, activeVenue]);

  return (
    <>
      {setupBanner}

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Venue</h2>
          <span className="panel-meta">
            Active: {venueLabel(activeVenue)}
            {conn.canGoLive ? " · ready for live" : " · keys missing"}
          </span>
        </div>

        <p className="panel-body" style={{ marginTop: 0, marginBottom: 18 }}>
          Polymarket runs two separate platforms. Delfi can trade on either,
          one at a time. Your keys for each side are kept separately so
          switching venues doesn't wipe the other side's credentials.
        </p>

        <div className="ob-choices">
          <button
            className={`ob-choice ${activeVenue === "polymarket" ? "selected" : ""}`}
            onClick={() => switchVenue("polymarket")}
            disabled={conn.saving}
          >
            <div className="ob-choice-head">
              <div className="ob-choice-title">Polymarket</div>
              <div className="ob-choice-meta">
                Offshore · USDC on Polygon
                {conn.status.polymarket.readyForLive ? " · ready" : ""}
              </div>
            </div>
            <div className="ob-choice-body">
              The original offshore Polymarket. Deeper liquidity and the
              widest market catalog. Not available to US residents.
            </div>
          </button>

          <button
            className={`ob-choice ${activeVenue === "polymarket_us" ? "selected" : ""}`}
            onClick={() => switchVenue("polymarket_us")}
            disabled={conn.saving}
          >
            <div className="ob-choice-head">
              <div className="ob-choice-title">Polymarket US</div>
              <div className="ob-choice-meta">
                CFTC-regulated DCM · USD
                {conn.status.polymarketUs.readyForLive ? " · ready" : ""}
              </div>
            </div>
            <div className="ob-choice-body">
              The CFTC-regulated Designated Contract Market. Settles in USD,
              no wallet needed. Smaller market catalog today, but required if
              you live in the United States.
            </div>
          </button>
        </div>

        <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
          {savedAt && !conn.error && (
            <span style={{ color: "var(--vellum-60)", fontSize: 13 }}>Saved.</span>
          )}
          {conn.error && (
            <span style={{ color: "var(--red)", fontSize: 13 }}>{conn.error}</span>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">
            Polymarket credentials
            {activeVenue === "polymarket" ? (
              <span className="panel-meta" style={{ marginLeft: 10 }}>active venue</span>
            ) : null}
          </h2>
          {!conn.status.polymarket.readyForLive && (
            <span className="panel-meta">
              Missing: {[
                !conn.status.polymarket.apiKeySet && "API key",
                !conn.status.polymarket.apiSecretSet && "API secret",
                !conn.status.polymarket.walletAddress.trim() && "wallet",
              ].filter(Boolean).join(", ")}
            </span>
          )}
        </div>

        <p className="panel-body" style={{ marginTop: 0, marginBottom: 18 }}>
          Offshore Polymarket on Polygon. Delfi needs an API key, secret, and
          wallet address to place real trades. Keys are stored encrypted in
          your Delfi account and are only decrypted by the trading engine.
          We never show secrets back - leave a field blank to keep what we
          already have; type a new value to replace it; type a single space
          to clear it.
        </p>

        <div className="form-row">
          <div className="form-field">
            <label>
              Polymarket API key <span style={{ color: "var(--gold-60)" }}>·required</span>
              {conn.status.polymarket.apiKeySet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={offReveal ? "text" : "password"}
              value={offDraft.apiKey}
              onChange={(e) => {
                setOffDraft((d) => ({ ...d, apiKey: e.target.value }));
                setSavedAt(null);
              }}
              placeholder={conn.status.polymarket.apiKeySet ? "••••••••" : "pk_live_…"}
            />
            <div className="form-hint">Create in your Polymarket account under Settings → API.</div>
          </div>

          <div className="form-field">
            <label>
              Polymarket API secret <span style={{ color: "var(--gold-60)" }}>·required</span>
              {conn.status.polymarket.apiSecretSet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={offReveal ? "text" : "password"}
              value={offDraft.apiSecret}
              onChange={(e) => {
                setOffDraft((d) => ({ ...d, apiSecret: e.target.value }));
                setSavedAt(null);
              }}
              placeholder={conn.status.polymarket.apiSecretSet ? "••••••••" : "Shown once at creation"}
            />
            <div className="form-hint">Pair of the API key.</div>
          </div>

          <div className="form-field">
            <label>
              Polymarket passphrase
              {conn.status.polymarket.passphraseSet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={offReveal ? "text" : "password"}
              value={offDraft.passphrase}
              onChange={(e) => {
                setOffDraft((d) => ({ ...d, passphrase: e.target.value }));
                setSavedAt(null);
              }}
              placeholder="Optional"
            />
            <div className="form-hint">Only required if you set one when generating the key.</div>
          </div>

          <div className="form-field">
            <label>Wallet address <span style={{ color: "var(--gold-60)" }}>·required</span></label>
            <input
              value={offDraft.walletAddress}
              onChange={(e) => {
                setOffDraft((d) => ({ ...d, walletAddress: e.target.value }));
                setSavedAt(null);
              }}
              placeholder="0x…"
            />
            <div className="form-hint">Polygon address that will hold positions and receive fills.</div>
          </div>

          <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
            <button
              className="btn-sm gold"
              onClick={saveOffshore}
              disabled={conn.saving}
            >
              {conn.saving ? "Saving…" : "Save Polymarket credentials"}
            </button>
            <button
              type="button"
              className="btn-sm"
              onClick={() => setOffReveal((r) => !r)}
              disabled={
                !offDraft.apiKey &&
                !offDraft.apiSecret &&
                !offDraft.passphrase
              }
              title={
                !offDraft.apiKey &&
                !offDraft.apiSecret &&
                !offDraft.passphrase
                  ? "Type a key, secret, or passphrase first. Saved values are never sent back to the browser."
                  : undefined
              }
            >
              {offReveal ? "Hide values" : "Reveal values"}
            </button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">
            Polymarket US credentials
            {activeVenue === "polymarket_us" ? (
              <span className="panel-meta" style={{ marginLeft: 10 }}>active venue</span>
            ) : null}
          </h2>
          {!conn.status.polymarketUs.readyForLive && (
            <span className="panel-meta">
              Missing: {[
                !conn.status.polymarketUs.apiKeySet && "API key",
                !conn.status.polymarketUs.apiSecretSet && "API secret",
              ].filter(Boolean).join(", ")}
            </span>
          )}
        </div>

        <p className="panel-body" style={{ marginTop: 0, marginBottom: 18 }}>
          CFTC-regulated DCM. Settles in USD, no wallet needed. Issue an API
          key + secret from your Polymarket US account and paste them below.
          Passphrase is only required if you set one when generating the key.
        </p>

        <div className="form-row">
          <div className="form-field">
            <label>
              API key <span style={{ color: "var(--gold-60)" }}>·required</span>
              {conn.status.polymarketUs.apiKeySet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={usReveal ? "text" : "password"}
              value={usDraft.apiKey}
              onChange={(e) => {
                setUsDraft((d) => ({ ...d, apiKey: e.target.value }));
                setSavedAt(null);
              }}
              placeholder={conn.status.polymarketUs.apiKeySet ? "••••••••" : ""}
            />
          </div>

          <div className="form-field">
            <label>
              API secret <span style={{ color: "var(--gold-60)" }}>·required</span>
              {conn.status.polymarketUs.apiSecretSet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={usReveal ? "text" : "password"}
              value={usDraft.apiSecret}
              onChange={(e) => {
                setUsDraft((d) => ({ ...d, apiSecret: e.target.value }));
                setSavedAt(null);
              }}
              placeholder={conn.status.polymarketUs.apiSecretSet ? "••••••••" : ""}
            />
          </div>

          <div className="form-field">
            <label>
              Passphrase
              {conn.status.polymarketUs.passphraseSet && (
                <span style={{ marginLeft: 8, color: "var(--vellum-60)", fontSize: 12 }}>
                  (saved)
                </span>
              )}
            </label>
            <input
              type={usReveal ? "text" : "password"}
              value={usDraft.passphrase}
              onChange={(e) => {
                setUsDraft((d) => ({ ...d, passphrase: e.target.value }));
                setSavedAt(null);
              }}
              placeholder="Optional"
            />
            <div className="form-hint">Only required if you set one when generating the key.</div>
          </div>

          <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
            <button
              className="btn-sm gold"
              onClick={saveUs}
              disabled={conn.saving}
            >
              {conn.saving ? "Saving…" : "Save Polymarket US credentials"}
            </button>
            <button
              type="button"
              className="btn-sm"
              onClick={() => setUsReveal((r) => !r)}
              disabled={
                !usDraft.apiKey &&
                !usDraft.apiSecret &&
                !usDraft.passphrase
              }
              title={
                !usDraft.apiKey &&
                !usDraft.apiSecret &&
                !usDraft.passphrase
                  ? "Type a key, secret, or passphrase first. Saved values are never sent back to the browser."
                  : undefined
              }
            >
              {usReveal ? "Hide values" : "Reveal values"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
