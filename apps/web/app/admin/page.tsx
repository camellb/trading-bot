"use client";

import { useEffect, useState } from "react";
import { getJSON } from "@/lib/fetch-json";

type Stats = {
  onboarded_users:     number;
  total_users:         number;
  active_subscribers:  number;
  bankroll_under_mgmt: number;
  open_positions:      number;
  trades_24h:          number;
  total_realized:      number;
  total_evaluations:   number;
};

type Alert = {
  level:     "warn" | "info";
  title:     string;
  detail:    string;
  timestamp: string | null;
};

type Activity = {
  timestamp:   string | null;
  kind:        string;
  description: string;
};

type OverviewPayload = {
  stats:    Stats;
  alerts:   Alert[];
  activity: Activity[];
};

function fmtMoney(v: number): string {
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const now = Date.now();
  const diffMs = now - d.getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function AdminOverviewPage() {
  const [data, setData]     = useState<OverviewPayload | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError]   = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await getJSON<OverviewPayload>("/api/admin/overview");
        if (cancelled) return;
        setData(res);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load overview");
      } finally {
        if (!cancelled) setLoaded(true);
      }
    };
    load();
    const timer = setInterval(load, 30_000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  const s = data?.stats;
  const statCells = [
    {
      label: "Active subscribers",
      value: s ? s.active_subscribers.toLocaleString() : "-",
      delta: s ? `${s.onboarded_users} onboarded / ${s.total_users} total` : "",
    },
    {
      label: "Bankroll under management",
      value: s ? `$${s.bankroll_under_mgmt.toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "-",
      delta: s ? `across ${s.active_subscribers} active users` : "",
    },
    {
      label: "Trades (24h)",
      value: s ? s.trades_24h.toLocaleString() : "-",
      delta: s ? `${s.open_positions} currently open` : "",
    },
    {
      label: "Realized P&L (all time)",
      value: s ? fmtMoney(s.total_realized) : "-",
      delta: s ? `${s.total_evaluations.toLocaleString()} evaluations run` : "",
    },
  ];

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Admin overview</h1>
            <p className="page-sub">
              {loaded
                ? "Platform health at a glance. Refreshes every 30s."
                : "Loading..."}
            </p>
          </div>
        </div>
      </div>

      {error ? (
        <div className="panel">
          <div className="split-row">
            <div className="split-body">
              <div className="split-title">Could not load overview</div>
              <div className="split-desc">{error}</div>
            </div>
          </div>
        </div>
      ) : null}

      <div className="stat-row">
        {statCells.map((c, i) => (
          <div className="stat-cell" key={i}>
            <div className="stat-cell-label">{c.label}</div>
            <div className="stat-cell-val">{c.value}</div>
            <div className="stat-cell-delta">{c.delta}</div>
          </div>
        ))}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Alerts</h2>
          <span className="panel-meta">
            {data ? `${data.alerts.length} active` : ""}
          </span>
        </div>
        {!loaded ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">Loading...</div></div></div>
        ) : !data || data.alerts.length === 0 ? (
          <div className="split-row">
            <div className="split-body">
              <div className="split-desc">No active alerts. All feeds healthy in the last hour.</div>
            </div>
          </div>
        ) : (
          data.alerts.map((a, i) => (
            <div className="split-row" key={i}>
              <div className="split-body">
                <div className="split-title">
                  <span className={`pill ${a.level === "warn" ? "pill-no" : "pill-open"}`} style={{ marginRight: 10 }}>
                    {a.level.toUpperCase()}
                  </span>
                  {a.title}
                </div>
                <div className="split-desc">{a.detail || fmtTime(a.timestamp)}</div>
              </div>
              <div className="split-right">
                <span className="mono" style={{ color: "var(--vellum-40)", fontSize: 12 }}>
                  {fmtTime(a.timestamp)}
                </span>
              </div>
            </div>
          ))
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Recent activity</h2>
          <span className="panel-meta">Last 24h</span>
        </div>
        {!loaded ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">Loading...</div></div></div>
        ) : !data || data.activity.length === 0 ? (
          <div className="split-row">
            <div className="split-body">
              <div className="split-desc">No activity in the last 24h.</div>
            </div>
          </div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {data.activity.map((a, i) => (
              <li className="split-row" key={i}>
                <div className="split-body" style={{ display: "flex", gap: 16 }}>
                  <span className="mono" style={{ fontSize: 12, color: "var(--vellum-40)", minWidth: 72 }}>
                    {fmtTime(a.timestamp)}
                  </span>
                  <span style={{ color: "var(--vellum)" }}>{a.description}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
