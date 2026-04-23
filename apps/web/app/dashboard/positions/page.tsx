"use client";

import { useEffect, useState } from "react";
import "../../styles/content.css";

type Filter = "all" | "open" | "scanning" | "recent";

type OpenPosition = {
  id: number;
  question: string;
  side: "YES" | "NO";
  shares: number;
  entry_price: number;
  cost_usd: number;
  claude_probability: number | null;
  confidence: number | null;
  expected_resolution_at: string | null;
  created_at: string | null;
  category: string | null;
};

type SettledPosition = {
  id: number;
  question: string;
  side: "YES" | "NO";
  cost_usd: number;
  entry_price: number;
  claude_probability: number | null;
  confidence: number | null;
  settlement_outcome: string | null;
  realized_pnl_usd: number | null;
  settled_at: string | null;
  category: string | null;
};

type PositionsPayload = { open: OpenPosition[]; settled: SettledPosition[] };

type Evaluation = {
  id: number;
  evaluated_at: string | null;
  question: string;
  category: string | null;
  market_price_yes: number | null;
  claude_probability: number | null;
  confidence: number | null;
  ev_bps: number | null;
  recommendation: string | null;
  reasoning: string | null;
  slug: string | null;
};

type EvalsPayload = { evaluations: Evaluation[] };

async function getJSON<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

function daysFromNow(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return "-";
  const ms = d - Date.now();
  if (ms <= 0) return "now";
  const days = Math.round(ms / 86_400_000);
  if (days === 0) {
    const hours = Math.max(1, Math.round(ms / 3_600_000));
    return `${hours}h`;
  }
  return `${days}d`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function PositionsPage() {
  const [filter, setFilter] = useState<Filter>("all");
  const [positions, setPositions] = useState<PositionsPayload | null>(null);
  const [evaluations, setEvals] = useState<EvalsPayload | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [p, e] = await Promise.all([
        getJSON<PositionsPayload>("/api/positions"),
        getJSON<EvalsPayload>("/api/evaluations?limit=25"),
      ]);
      if (cancelled) return;
      setPositions(p);
      setEvals(e);
      setLoaded(true);
    };
    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const open    = positions?.open ?? [];
  const settled = positions?.settled ?? [];
  const evals   = evaluations?.evaluations ?? [];
  const deployed = open.reduce((s, p) => s + (p.cost_usd ?? 0), 0);

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Positions</h1>
            <p className="page-sub">Every position Delfi is managing right now, plus the markets it's watching.</p>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div className="page-toolbar-left">
          <button className={`chip ${filter === "all" ? "on" : ""}`} onClick={() => setFilter("all")}>
            All
          </button>
          <button className={`chip ${filter === "open" ? "on" : ""}`} onClick={() => setFilter("open")}>
            Open ({open.length})
          </button>
          <button className={`chip ${filter === "scanning" ? "on" : ""}`} onClick={() => setFilter("scanning")}>
            Scanning ({evals.length})
          </button>
          <button className={`chip ${filter === "recent" ? "on" : ""}`} onClick={() => setFilter("recent")}>
            Recently resolved ({settled.length})
          </button>
        </div>
      </div>

      {(filter === "all" || filter === "open") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Open positions</h2>
            <span className="panel-meta">
              {open.length} active · ${deployed.toFixed(0)} deployed
            </span>
          </div>

          {open.length === 0 ? (
            <div className="empty-state">
              {loaded
                ? "No open positions yet. Delfi is evaluating markets - positions will appear once a trade clears all gates."
                : "Loading..."}
            </div>
          ) : (
            <table className="table-simple">
              <thead>
                <tr>
                  <th>Market</th>
                  <th>Category</th>
                  <th>Side</th>
                  <th>Entry</th>
                  <th>Size</th>
                  <th>p_win</th>
                  <th>Confidence</th>
                  <th>Closes</th>
                </tr>
              </thead>
              <tbody>
                {open.map((p) => {
                  const entryCents = Math.round(p.entry_price * 100);
                  const pWin = p.claude_probability;
                  const conf = p.confidence;
                  return (
                    <tr key={p.id}>
                      <td>{p.question}</td>
                      <td>{p.category ?? "-"}</td>
                      <td>
                        <span className={p.side === "YES" ? "pill pill-yes" : "pill pill-no"}>
                          {p.side}
                        </span>
                      </td>
                      <td className="mono">{entryCents}¢</td>
                      <td className="mono">${p.cost_usd.toFixed(0)}</td>
                      <td className="mono">{pWin != null ? pWin.toFixed(2) : "-"}</td>
                      <td className="mono">{conf != null ? conf.toFixed(2) : "-"}</td>
                      <td className="mono">{daysFromNow(p.expected_resolution_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {(filter === "all" || filter === "scanning") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Recent evaluations</h2>
            <span className="panel-meta">
              {evals.length} markets analyzed - most recent first
            </span>
          </div>

          {evals.length === 0 ? (
            <div className="empty-state">
              {loaded
                ? "No recent evaluations. The bot scans Polymarket periodically - check back shortly."
                : "Loading..."}
            </div>
          ) : (
            evals.map((s) => {
              const rec = (s.recommendation ?? "").toUpperCase();
              const traded = rec === "YES" || rec === "NO" || rec === "BUY";
              const marketPct =
                s.market_price_yes != null ? Math.round(s.market_price_yes * 100) : null;
              const delfiPct =
                s.claude_probability != null ? Math.round(s.claude_probability * 100) : null;
              const pWin =
                s.claude_probability != null
                  ? rec === "NO"
                    ? 1 - s.claude_probability
                    : s.claude_probability
                  : null;
              return (
                <div className="split-row" key={s.id}>
                  <div className="split-body">
                    <div className="split-title">{s.question}</div>
                    <div className="split-desc">
                      {s.reasoning ?? "No reasoning recorded."}
                    </div>
                  </div>
                  <div className="split-right">
                    <div style={{ display: "flex", gap: 24, alignItems: "center" }}>
                      <div>
                        <div className="kv-label">Market</div>
                        <div className="mono" style={{ fontSize: 13 }}>
                          {marketPct != null ? `${marketPct}¢` : "-"}
                        </div>
                      </div>
                      <div>
                        <div className="kv-label">Delfi</div>
                        <div className="mono" style={{ fontSize: 13, color: "var(--gold)" }}>
                          {delfiPct != null ? `${delfiPct}¢` : "-"}
                        </div>
                      </div>
                      <div>
                        <div className="kv-label">Decision</div>
                        <div
                          className={`mono ${traded ? "cell-up" : ""}`}
                          style={{ fontSize: 13 }}
                        >
                          {rec || "-"}
                          {pWin != null ? ` · ${pWin.toFixed(2)}` : ""}
                        </div>
                      </div>
                      <div>
                        <div className="kv-label">Evaluated</div>
                        <div className="mono" style={{ fontSize: 13 }}>
                          {formatDate(s.evaluated_at)}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      )}

      {(filter === "all" || filter === "recent") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Recently resolved</h2>
            <span className="panel-meta">{settled.length} settled</span>
          </div>

          {settled.length === 0 ? (
            <div className="empty-state">
              Recently resolved positions will appear here once markets settle.
            </div>
          ) : (
            <table className="table-simple">
              <thead>
                <tr>
                  <th>Market</th>
                  <th>Side</th>
                  <th>Outcome</th>
                  <th>Size</th>
                  <th>P&amp;L</th>
                  <th>D YES %</th>
                  <th>M YES %</th>
                  <th>D CONF</th>
                  <th>Settled</th>
                </tr>
              </thead>
              <tbody>
                {settled.map((s) => {
                  const pnl = s.realized_pnl_usd ?? 0;
                  const delfiYes = s.claude_probability;
                  const marketYes =
                    s.entry_price != null
                      ? s.side === "YES"
                        ? s.entry_price
                        : 1 - s.entry_price
                      : null;
                  return (
                    <tr key={s.id}>
                      <td>{s.question}</td>
                      <td>
                        <span className={s.side === "YES" ? "pill pill-yes" : "pill pill-no"}>
                          {s.side}
                        </span>
                      </td>
                      <td className="mono">{s.settlement_outcome ?? "-"}</td>
                      <td className="mono">${s.cost_usd.toFixed(0)}</td>
                      <td className={`mono ${pnl >= 0 ? "cell-up" : "cell-down"}`}>
                        {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
                      </td>
                      <td className="mono">
                        {delfiYes != null ? `${Math.round(delfiYes * 100)}%` : "-"}
                      </td>
                      <td className="mono">
                        {marketYes != null ? `${Math.round(marketYes * 100)}%` : "-"}
                      </td>
                      <td className="mono">
                        {s.confidence != null ? s.confidence.toFixed(2) : "-"}
                      </td>
                      <td className="mono">{formatDate(s.settled_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
