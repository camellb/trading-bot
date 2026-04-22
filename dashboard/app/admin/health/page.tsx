"use client";

type Status = "up" | "degraded" | "down";

type Service = {
  name: string;
  status: Status;
  latencyMs: number;
  uptime30d: number;
  lastIncident: string;
};

const SERVICES: Service[] = [
  { name: "Polymarket REST",       status: "up",       latencyMs: 142, uptime30d: 99.98, lastIncident: "No incidents" },
  { name: "Polymarket WebSocket",  status: "up",       latencyMs: 38,  uptime30d: 99.82, lastIncident: "2026-04-12 · 11s drop" },
  { name: "Anthropic API",          status: "up",       latencyMs: 820, uptime30d: 99.91, lastIncident: "No incidents" },
  { name: "Postgres (primary)",    status: "up",       latencyMs: 4,   uptime30d: 100.00, lastIncident: "No incidents" },
  { name: "Postgres (replica)",    status: "degraded", latencyMs: 82,  uptime30d: 99.71, lastIncident: "2026-04-21 · replication lag 14s" },
  { name: "Redis cache",           status: "up",       latencyMs: 1,   uptime30d: 100.00, lastIncident: "No incidents" },
  { name: "Background worker",     status: "up",       latencyMs: 12,  uptime30d: 99.95, lastIncident: "2026-04-08 · restart" },
  { name: "Email delivery (Postmark)", status: "up",   latencyMs: 210, uptime30d: 99.88, lastIncident: "No incidents" },
];

const QUEUES = [
  { name: "market-ingest",     depth: 42,   rate: "2.1k/min", lag: "under 1s" },
  { name: "forecast-pending",  depth: 8,    rate: "120/min",  lag: "under 5s" },
  { name: "trade-execute",     depth: 0,    rate: "80/min",   lag: "real-time" },
  { name: "calibration-batch", depth: 1204, rate: "slow drip", lag: "scheduled" },
];

const LATENCY_BUCKETS = [
  { label: "p50", anthropic: 620, polymarket: 88,  db: 2 },
  { label: "p90", anthropic: 1180, polymarket: 210, db: 5 },
  { label: "p99", anthropic: 2200, polymarket: 480, db: 14 },
];

const STATUS_PILL: Record<Status, string> = {
  up: "pill-won",
  degraded: "pill-no",
  down: "pill-lost",
};

export default function AdminHealthPage() {
  const degraded = SERVICES.filter((s) => s.status !== "up").length;

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">System health</h1>
            <p className="page-sub">
              {degraded === 0 ? "All services operating normally." : `${degraded} service${degraded === 1 ? "" : "s"} degraded — see below.`}
            </p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm">Open status page</button>
            <button className="btn-sm danger">Trigger incident</button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Services</h2>
          <span className="panel-meta">Realtime · 30s refresh</span>
        </div>

        <table className="table-simple">
          <thead>
            <tr>
              <th>Service</th>
              <th>Status</th>
              <th>Latency</th>
              <th>Uptime (30d)</th>
              <th>Last incident</th>
            </tr>
          </thead>
          <tbody>
            {SERVICES.map((s) => (
              <tr key={s.name}>
                <td>{s.name}</td>
                <td>
                  <span className={`pill ${STATUS_PILL[s.status]}`} style={{ textTransform: "capitalize" }}>
                    {s.status}
                  </span>
                </td>
                <td className="mono">{s.latencyMs} ms</td>
                <td className="mono">{s.uptime30d.toFixed(2)}%</td>
                <td className="mono" style={{ color: "var(--vellum-60)" }}>{s.lastIncident}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Queue depth</h2>
          <span className="panel-meta">Background pipelines</span>
        </div>

        <table className="table-simple">
          <thead>
            <tr>
              <th>Queue</th>
              <th>Depth</th>
              <th>Throughput</th>
              <th>Lag</th>
            </tr>
          </thead>
          <tbody>
            {QUEUES.map((q) => (
              <tr key={q.name}>
                <td className="mono">{q.name}</td>
                <td className="mono">{q.depth.toLocaleString()}</td>
                <td className="mono">{q.rate}</td>
                <td className="mono" style={{ color: "var(--vellum-60)" }}>{q.lag}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Latency percentiles</h2>
          <span className="panel-meta">Last 1h</span>
        </div>

        <table className="table-simple">
          <thead>
            <tr>
              <th>Percentile</th>
              <th>Anthropic</th>
              <th>Polymarket</th>
              <th>Postgres</th>
            </tr>
          </thead>
          <tbody>
            {LATENCY_BUCKETS.map((b) => (
              <tr key={b.label}>
                <td className="mono">{b.label}</td>
                <td className="mono">{b.anthropic} ms</td>
                <td className="mono">{b.polymarket} ms</td>
                <td className="mono">{b.db} ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
