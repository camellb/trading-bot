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

function formatDateTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const date = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const time = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${date} · ${time}`;
}

function shortReason(full: string | null, max = 80): string {
  if (!full) return "-";
  const clean = full.trim().replace(/\s+/g, " ");
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  const base = lastSpace > max * 0.5 ? cut.slice(0, lastSpace) : cut;
  return base + "…";
}

function normalizeDecision(raw: string | null): "BUY YES" | "BUY NO" | "SKIP" {
  const up = (raw ?? "").toUpperCase();
  if (up === "BUY_YES" || up === "YES") return "BUY YES";
  if (up === "BUY_NO" || up === "NO") return "BUY NO";
  return "SKIP";
}

export default function PositionsPage() {
  const [filter, setFilter] = useState<Filter>("all");
  const [positions, setPositions] = useState<PositionsPayload | null>(null);
  const [evaluations, setEvals] = useState<EvalsPayload | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [expandedEvals, setExpandedEvals] = useState<Set<number>>(new Set());

  const toggleEval = (id: number) => {
    setExpandedEvals(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

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
          </div>

          {evals.length === 0 ? (
            <div className="empty-state">
              {loaded
                ? "No recent evaluations. The bot scans Polymarket periodically - check back shortly."
                : "Loading..."}
            </div>
          ) : (
            evals.map((s) => {
              const decision = normalizeDecision(s.recommendation);
              const marketPct =
                s.market_price_yes != null ? Math.round(s.market_price_yes * 100) : null;
              const delfiPct =
                s.claude_probability != null ? Math.round(s.claude_probability * 100) : null;
              const pWinPct =
                marketPct != null && delfiPct != null
                  ? Math.round((marketPct + delfiPct) / 2)
                  : null;
              const reasonFull = (s.reasoning ?? "").trim();
              const reasonShort = shortReason(reasonFull, 80);
              const hasMore = reasonFull.length > 0 && reasonShort.endsWith("…");
              const isExpanded = expandedEvals.has(s.id);
              const decisionColor =
                decision === "SKIP" ? "var(--vellum-60)" : "var(--gold)";
              return (
                <div
                  className="split-row"
                  key={s.id}
                  style={{ flexDirection: "column", alignItems: "stretch" }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "flex-start",
                      gap: 20,
                      width: "100%",
                    }}
                  >
                    <div className="split-title" style={{ flex: 1 }}>
                      {s.question}
                    </div>
                    <div
                      style={{
                        display: "flex",
                        gap: 22,
                        alignItems: "flex-start",
                        flexShrink: 0,
                      }}
                    >
                      <div>
                        <div className="kv-label">Market</div>
                        <div className="mono" style={{ fontSize: 13 }}>
                          {marketPct != null ? `${marketPct}%` : "-"}
                        </div>
                      </div>
                      <div>
                        <div className="kv-label">Delfi</div>
                        <div className="mono" style={{ fontSize: 13, color: "var(--gold)" }}>
                          {delfiPct != null ? `${delfiPct}%` : "-"}
                        </div>
                      </div>
                      <div>
                        <div className="kv-label">p_win</div>
                        <div className="mono" style={{ fontSize: 13 }}>
                          {pWinPct != null ? `${pWinPct}%` : "-"}
                        </div>
                      </div>
                      <div>
                        <div className="kv-label">Decision</div>
                        <div
                          className="mono"
                          style={{ fontSize: 13, color: decisionColor, whiteSpace: "nowrap" }}
                        >
                          {decision}
                        </div>
                      </div>
                      <div>
                        <div className="kv-label">When</div>
                        <div className="mono" style={{ fontSize: 13, whiteSpace: "nowrap" }}>
                          {formatDateTime(s.evaluated_at)}
                        </div>
                      </div>
                    </div>
                  </div>
                  <div
                    onClick={hasMore ? () => toggleEval(s.id) : undefined}
                    style={{
                      marginTop: 10,
                      color: "var(--vellum-60)",
                      lineHeight: 1.55,
                      cursor: hasMore ? "pointer" : "default",
                      display: "flex",
                      alignItems: "flex-start",
                      gap: 10,
                      fontSize: 13,
                    }}
                  >
                    <div className="kv-label" style={{ flexShrink: 0, paddingTop: 2 }}>Reason</div>
                    <span style={{ flex: 1 }}>
                      {reasonFull
                        ? isExpanded
                          ? reasonFull
                          : reasonShort
                        : "No reasoning recorded."}
                    </span>
                    {hasMore && (
                      <span
                        className="mono"
                        style={{
                          color: "var(--vellum-40)",
                          fontSize: 12,
                          flexShrink: 0,
                          paddingTop: 2,
                        }}
                      >
                        {isExpanded ? "▲ collapse" : "▼ show more"}
                      </span>
                    )}
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
