"use client";

import { useEffect, useMemo, useState } from "react";

type ForecasterPayload = {
  window_days: number;
  totals: {
    evaluated: number;
    skipped:   number;
    skip_rate: number;
  };
  by_category: Array<{
    category: string;
    settled:  number;
    wins:     number;
    win_rate: number;
    realized: number;
  }>;
  feeds: Array<{
    feed_name: string;
    state:     string | null;
    detail:    string | null;
    timestamp: string | null;
  }>;
};

const WINDOWS: Array<{ days: number; label: string }> = [
  { days: 1,  label: "1d"  },
  { days: 7,  label: "7d"  },
  { days: 30, label: "30d" },
  { days: 90, label: "90d" },
];

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return `${(v * 100).toFixed(digits)}%`;
}

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function feedPill(state: string | null): string {
  const s = (state || "").toLowerCase();
  if (s === "ok" || s === "healthy" || s === "up") return "pill-won";
  if (s === "warn" || s === "degraded" || s === "stale") return "pill-skip";
  if (s === "error" || s === "down" || s === "failed") return "pill-no";
  return "pill-open";
}

export default function AdminForecasterPage() {
  const [days, setDays]     = useState<number>(7);
  const [data, setData]     = useState<ForecasterPayload | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError]   = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    const load = async () => {
      try {
        const r = await fetch(
          `/api/admin/forecaster?days=${days}`,
          { cache: "no-store" },
        );
        if (cancelled) return;
        if (!r.ok) {
          setError(`HTTP ${r.status}: ${await r.text().catch(() => "request failed")}`);
          setData(null);
          return;
        }
        const res = (await r.json()) as ForecasterPayload;
        setData(res);
        setError(null);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (!cancelled) setLoaded(true);
      }
    };
    load();
    return () => { cancelled = true; };
  }, [days]);

  const categoriesSorted = useMemo(() => {
    if (!data) return [];
    return [...data.by_category].sort((a, b) => b.realized - a.realized);
  }, [data]);

  const totalRealized = useMemo(() => {
    if (!data) return 0;
    return data.by_category.reduce((acc, r) => acc + (r.realized || 0), 0);
  }, [data]);

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Forecaster health</h1>
            <p className="page-sub">Skip rate, P&amp;L by category, and feed status.</p>
          </div>
          <div className="tab-bar" style={{ padding: 0, margin: 0 }}>
            {WINDOWS.map((w) => (
              <button
                key={w.days}
                onClick={() => setDays(w.days)}
                className={`tab ${days === w.days ? "on" : ""}`}
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {error ? (
        <div className="panel">
          <div className="split-row">
            <div className="split-body">
              <div className="split-title">Could not load forecaster health</div>
              <div className="split-desc">{error}</div>
            </div>
          </div>
        </div>
      ) : !loaded ? (
        <div className="panel">
          <div className="split-row"><div className="split-body"><div className="split-desc">Loading...</div></div></div>
        </div>
      ) : !data ? null : (
        <>
          <div className="stat-row">
            <div className="stat-cell">
              <div className="stat-cell-label">Evaluated</div>
              <div className="stat-cell-val">{data.totals.evaluated.toLocaleString()}</div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Skipped</div>
              <div className="stat-cell-val">{data.totals.skipped.toLocaleString()}</div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Skip rate</div>
              <div className="stat-cell-val">{fmtPct(data.totals.skip_rate)}</div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Entered</div>
              <div className="stat-cell-val">
                {(data.totals.evaluated - data.totals.skipped).toLocaleString()}
              </div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Realized P&amp;L</div>
              <div className={`stat-cell-val ${
                totalRealized > 0 ? "cell-up"
                : totalRealized < 0 ? "cell-down" : ""
              }`}>{fmtMoney(totalRealized)}</div>
            </div>
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2 className="panel-title">By category ({data.window_days}d)</h2>
            </div>
            {categoriesSorted.length === 0 ? (
              <div className="split-row">
                <div className="split-body">
                  <div className="split-desc">No settled trades in this window.</div>
                </div>
              </div>
            ) : (
              <table className="table-simple">
                <thead>
                  <tr>
                    <th>Category</th>
                    <th>Settled</th>
                    <th>Wins</th>
                    <th>Win rate</th>
                    <th>Realized</th>
                  </tr>
                </thead>
                <tbody>
                  {categoriesSorted.map((r) => (
                    <tr key={r.category}>
                      <td>{r.category}</td>
                      <td className="mono">{r.settled}</td>
                      <td className="mono">{r.wins}</td>
                      <td className="mono">{fmtPct(r.win_rate)}</td>
                      <td className={`mono ${
                        r.realized > 0 ? "cell-up"
                        : r.realized < 0 ? "cell-down" : ""
                      }`}>{fmtMoney(r.realized)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2 className="panel-title">Feeds (last hour)</h2>
            </div>
            {data.feeds.length === 0 ? (
              <div className="split-row">
                <div className="split-body">
                  <div className="split-desc">No recent feed health samples.</div>
                </div>
              </div>
            ) : (
              <table className="table-simple">
                <thead>
                  <tr>
                    <th>Feed</th>
                    <th>State</th>
                    <th>Last seen</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {data.feeds.map((f) => (
                    <tr key={f.feed_name}>
                      <td className="mono">{f.feed_name}</td>
                      <td>
                        <span className={`pill ${feedPill(f.state)}`}>
                          {f.state || "-"}
                        </span>
                      </td>
                      <td className="mono">{fmtDateTime(f.timestamp)}</td>
                      <td className="split-desc">{f.detail || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}
    </main>
  );
}
