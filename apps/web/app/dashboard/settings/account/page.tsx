"use client";

import { useEffect, useState } from "react";

// The Account page is for identity and account lifecycle only: display
// name, email (read-only), reset simulation history, and close account.
// Polymarket credentials live on the Connections tab, which is venue-aware
// (offshore vs Polymarket US) and shouldn't be duplicated here - having
// two places to set keys caused drift and confusion.

type Profile = { email: string; displayName: string };

export default function AccountPage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [nameSaving, setNameSaving] = useState(false);
  const [nameSavedAt, setNameSavedAt] = useState<number | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 10_000);
    (async () => {
      try {
        const r = await fetch("/api/profile", {
          cache: "no-store",
          signal: ctl.signal,
        });
        if (!r.ok) return;
        const j = (await r.json()) as Profile;
        if (cancelled) return;
        setProfile(j);
        setNameDraft(j.displayName ?? "");
      } catch {
        /* ignore */
      } finally {
        clearTimeout(timer);
      }
    })();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      ctl.abort();
    };
  }, []);

  const saveName = async () => {
    setNameError(null);
    setNameSaving(true);
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 10_000);
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
        signal: ctl.signal,
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setNameError(body?.error ?? "Couldn't save - try again.");
        return;
      }
      setProfile((p) => (p ? { ...p, displayName: trimmed } : p));
      setNameSavedAt(Date.now());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        setNameError("Save timed out - try again.");
      } else {
        setNameError("Couldn't save - try again.");
      }
    } finally {
      clearTimeout(timer);
      setNameSaving(false);
    }
  };

  // Reset-simulation state. Wipes only simulation history; live data is kept.
  const [resetSimBusy, setResetSimBusy] = useState(false);
  const [resetSimMsg, setResetSimMsg] = useState<string | null>(null);
  const [resetSimErr, setResetSimErr] = useState<string | null>(null);

  const resetSimulation = async () => {
    setResetSimErr(null);
    setResetSimMsg(null);
    const confirmed = window.confirm(
      "Reset simulation history? This clears all paper-trading positions and predictions. Live trades are not touched.",
    );
    if (!confirmed) return;
    setResetSimBusy(true);
    try {
      const r = await fetch("/api/reset-simulation", {
        method: "POST",
        cache: "no-store",
      });
      const body = (await r.json().catch(() => ({}))) as {
        positions_deleted?: number;
        predictions_deleted?: number;
        error?: string;
      };
      if (!r.ok) {
        setResetSimErr(body.error ?? "Reset failed, try again.");
        return;
      }
      const n = body.positions_deleted ?? 0;
      setResetSimMsg(
        n > 0
          ? `Cleared ${n} simulation position${n === 1 ? "" : "s"}. Dashboard will show fresh data.`
          : "No simulation history to clear.",
      );
    } catch {
      setResetSimErr("Reset failed, try again.");
    } finally {
      setResetSimBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Profile</h2>
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
          <h2 className="panel-title">Reset simulation history</h2>
        </div>
        <p className="panel-body">
          Clears your paper-trading positions and predictions so the dashboard
          starts from a clean slate. Your live trades, configuration, and
          credentials are kept exactly as they are.
        </p>
        <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center" }}>
          <button
            className="btn-sm"
            onClick={resetSimulation}
            disabled={resetSimBusy}
          >
            {resetSimBusy ? "Clearing…" : "Reset simulation data"}
          </button>
          {resetSimMsg && !resetSimErr && (
            <span style={{ color: "var(--vellum-60)", fontSize: 13 }}>
              {resetSimMsg}
            </span>
          )}
          {resetSimErr && (
            <span style={{ color: "var(--red)", fontSize: 13 }}>
              {resetSimErr}
            </span>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Close account</h2>
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
