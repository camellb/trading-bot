"use client";

const STATS = [
  { label: "Active users", value: "1,284", delta: "+42 this week" },
  { label: "Bankroll under management", value: "$3.42M", delta: "+$184k this week" },
  { label: "Trades (24h)", value: "4,812", delta: "+6% vs. yesterday" },
  { label: "System ROI (30d)", value: "+8.2%", delta: "median across users" },
];

const ALERTS = [
  { level: "warn", title: "Polymarket rate limit proximity", detail: "Hitting 82% of our REST budget peak. Consider backoff or extra key." },
  { level: "info", title: "Calibration drift - geopolitical tag", detail: "Bucket 60-70% running 4 pts hot across 42-user sample. Suggest cohort review." },
];

const FEED = [
  { t: "14:12", text: "User u_41a0 enabled live trading - $5k bankroll" },
  { t: "13:58", text: "Daily cap triggered for user u_1e8f" },
  { t: "13:42", text: "New signup - u_88ac · source: organic" },
  { t: "13:21", text: "Calibration pass completed - 47 suggestions queued" },
  { t: "12:55", text: "Polymarket WS reconnected after 11s drop" },
];

export default function AdminOverviewPage() {
  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Admin overview</h1>
            <p className="page-sub">Platform health at a glance. All counts live.</p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm">Open runbook</button>
            <button className="btn-sm danger">Pause all live trading</button>
          </div>
        </div>
      </div>

      <div className="stat-row">
        {STATS.map((s, i) => (
          <div className="stat-cell" key={i}>
            <div className="stat-cell-label">{s.label}</div>
            <div className="stat-cell-val">{s.value}</div>
            <div className="stat-cell-delta">{s.delta}</div>
          </div>
        ))}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Alerts</h2>
          <span className="panel-meta">{ALERTS.length} active</span>
        </div>
        {ALERTS.map((a, i) => (
          <div className="split-row" key={i}>
            <div className="split-body">
              <div className="split-title">
                <span className={`pill ${a.level === "warn" ? "pill-no" : "pill-open"}`} style={{ marginRight: 10 }}>
                  {a.level.toUpperCase()}
                </span>
                {a.title}
              </div>
              <div className="split-desc">{a.detail}</div>
            </div>
            <div className="split-right">
              <button className="btn-sm">Acknowledge</button>
            </div>
          </div>
        ))}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Recent activity</h2>
          <span className="panel-meta">Across all users</span>
        </div>
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {FEED.map((f, i) => (
            <li className="split-row" key={i}>
              <div className="split-body" style={{ display: "flex", gap: 16 }}>
                <span className="mono" style={{ fontSize: 12, color: "var(--vellum-40)", minWidth: 56 }}>{f.t}</span>
                <span style={{ color: "var(--vellum)" }}>{f.text}</span>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </main>
  );
}
