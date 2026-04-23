"use client";

import { useEffect, useState } from "react";

type ScannerPayload = {
  enabled:          boolean;
  interval_minutes: number;
  scan_limit:       number;
};

export default function AdminScannerPage() {
  const [data, setData]         = useState<ScannerPayload | null>(null);
  const [loaded, setLoaded]     = useState(false);
  const [error, setError]       = useState<string | null>(null);
  const [saving, setSaving]     = useState(false);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);
  const [intervalMin, setIntervalMin] = useState<number>(5);

  const load = async () => {
    try {
      const r = await fetch("/api/admin/scanner", { cache: "no-store" });
      if (!r.ok) {
        setError(`HTTP ${r.status}: ${await r.text().catch(() => "request failed")}`);
        setData(null);
        return;
      }
      const res = (await r.json()) as ScannerPayload;
      setData(res);
      setIntervalMin(res.interval_minutes);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoaded(true);
    }
  };

  useEffect(() => { load(); }, []);

  const toggleEnabled = async (next: boolean) => {
    if (!data) return;
    if (!next) {
      const ok = confirm(
        "Disable the scanner system-wide? No user's bot will evaluate or trade until re-enabled.",
      );
      if (!ok) return;
    }
    setSaving(true);
    setSavedMsg(null);
    setError(null);
    try {
      const r = await fetch("/api/admin/scanner", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      if (!r.ok) {
        const msg = await r.text().catch(() => "request failed");
        setError(`HTTP ${r.status}: ${msg}`);
        return;
      }
      setSavedMsg(next ? "Scanner enabled" : "Scanner disabled");
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const saveInterval = async () => {
    if (!data) return;
    if (intervalMin < 1 || intervalMin > 60) {
      setError("Interval must be between 1 and 60 minutes");
      return;
    }
    setSaving(true);
    setSavedMsg(null);
    setError(null);
    try {
      const r = await fetch("/api/admin/scanner", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ interval_minutes: intervalMin }),
      });
      if (!r.ok) {
        const msg = await r.text().catch(() => "request failed");
        setError(`HTTP ${r.status}: ${msg}`);
        return;
      }
      setSavedMsg(`Interval set to ${intervalMin} min`);
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div>
          <h1 className="page-h1">Scanner controls</h1>
          <p className="page-sub">
            System-wide market scan switch and cadence. Affects every user.
          </p>
        </div>
      </div>

      {error && (
        <div className="panel">
          <div className="split-row">
            <div className="split-body">
              <div className="split-title">Error</div>
              <div className="split-desc">{error}</div>
            </div>
          </div>
        </div>
      )}

      {!loaded ? (
        <div className="panel">
          <div className="split-row"><div className="split-body"><div className="split-desc">Loading...</div></div></div>
        </div>
      ) : !data ? null : (
        <>
          <div className="panel">
            <div className="panel-head">
              <h2 className="panel-title">Status</h2>
            </div>
            <div className="split-row">
              <div className="split-body">
                <div className="split-title">
                  <span className={`pill ${data.enabled ? "pill-won" : "pill-no"}`}>
                    {data.enabled ? "Running" : "Stopped"}
                  </span>
                </div>
                <div className="split-desc">
                  {data.enabled
                    ? `Scanning every ${data.interval_minutes} min, up to ${data.scan_limit} markets per cycle.`
                    : "Scheduler is halted. No user's bot will evaluate or trade."}
                </div>
              </div>
              <div className="split-right">
                {data.enabled ? (
                  <button
                    className="btn-sm danger"
                    onClick={() => toggleEnabled(false)}
                    disabled={saving}
                  >
                    {saving ? "Saving..." : "Stop scanner"}
                  </button>
                ) : (
                  <button
                    className="btn-sm gold"
                    onClick={() => toggleEnabled(true)}
                    disabled={saving}
                  >
                    {saving ? "Saving..." : "Start scanner"}
                  </button>
                )}
              </div>
            </div>
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2 className="panel-title">Scan interval</h2>
            </div>
            <div className="split-row">
              <div className="split-body">
                <div className="split-title">How often the scheduler runs a market scan</div>
                <div className="split-desc">
                  Range 1–60 minutes. Lower = fresher opportunities but more
                  compute cost. Cost-safe because each market is re-evaluated
                  at most once per 24h.
                </div>
              </div>
              <div className="split-right" style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                <input
                  type="number"
                  min={1}
                  max={60}
                  value={intervalMin}
                  onChange={(e) => setIntervalMin(Number(e.target.value))}
                  className="input-num"
                  style={{ width: "5rem" }}
                />
                <span className="split-desc">min</span>
                <button
                  className="btn-sm"
                  onClick={saveInterval}
                  disabled={saving || intervalMin === data.interval_minutes}
                >
                  {saving ? "Saving..." : "Save"}
                </button>
              </div>
            </div>
          </div>

          {savedMsg && (
            <div className="panel">
              <div className="split-row">
                <div className="split-body">
                  <div className="split-desc">{savedMsg}. Live - no restart needed.</div>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </main>
  );
}
