"use client";

import { useState } from "react";
import "../../styles/content.css";

type Filter = "all" | "open" | "scanning" | "recent";

type Position = {
  q: string;
  side: "YES" | "NO";
  entry: number;
  mark: number;
  size: number;
  pnl: number;
  pnlPct: number;
  pWin: number;
  closes: string;
  conviction: number;
};

type ScanItem = {
  q: string;
  price: number;
  forecast: number;
  side: "YES" | "NO";
  pWin: number;
  liquidity: string;
  closes: string;
  reason: string;
};

const OPEN: Position[] = [
  { q: "Fed cuts rates by 25bp in December?", side: "YES", entry: 44, mark: 52, size: 420, pnl: 71.4, pnlPct: 17.0, pWin: 0.78, closes: "18d", conviction: 0.81 },
  { q: "BTC closes above $120k by Dec 31?", side: "NO", entry: 38, mark: 33, size: 260, pnl: 34.2, pnlPct: 13.1, pWin: 0.74, closes: "42d", conviction: 0.72 },
  { q: "GPT-5 released in Q1 2026?", side: "YES", entry: 61, mark: 68, size: 180, pnl: 20.8, pnlPct: 11.5, pWin: 0.72, closes: "71d", conviction: 0.70 },
  { q: "US GDP Q4 > 2.5% advance est?", side: "NO", entry: 55, mark: 48, size: 140, pnl: 9.8, pnlPct: 7.0, pWin: 0.68, closes: "9d", conviction: 0.64 },
  { q: "Taylor Swift tour extension announced?", side: "YES", entry: 29, mark: 27, size: 80, pnl: -1.6, pnlPct: -2.0, pWin: 0.66, closes: "5d", conviction: 0.52 },
];

const SCAN: ScanItem[] = [
  { q: "SP500 closes green on Friday?", price: 58, forecast: 66, side: "YES", pWin: 0.66, liquidity: "$180k", closes: "2d", reason: "Breadth improving, VIX compressed, no macro catalyst. Forecast clears direction and p_win gates; watching expected return." },
  { q: "ETH flips BTC market cap in Q2?", price: 9, forecast: 4, side: "NO", pWin: 0.96, liquidity: "$40k", closes: "68d", reason: "Both Delfi and market sit well below 0.50 on YES, so NO p_win is very high. Longshot-NO candidate." },
  { q: "Congress passes stablecoin bill by July?", price: 42, forecast: 68, side: "YES", pWin: 0.68, liquidity: "$95k", closes: "88d", reason: "Bipartisan markup progressed. Slight tailwind from committee vote last week. Clears p_win floor." },
];

export default function PositionsPage() {
  const [filter, setFilter] = useState<Filter>("all");

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Positions</h1>
            <p className="page-sub">Every position Delfi is managing right now, plus the markets it's watching.</p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm">Export CSV</button>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div className="page-toolbar-left">
          <button className={`chip ${filter === "all" ? "on" : ""}`} onClick={() => setFilter("all")}>
            All
          </button>
          <button className={`chip ${filter === "open" ? "on" : ""}`} onClick={() => setFilter("open")}>
            Open ({OPEN.length})
          </button>
          <button className={`chip ${filter === "scanning" ? "on" : ""}`} onClick={() => setFilter("scanning")}>
            Scanning ({SCAN.length})
          </button>
          <button className={`chip ${filter === "recent" ? "on" : ""}`} onClick={() => setFilter("recent")}>
            Recently resolved
          </button>
        </div>
      </div>

      {(filter === "all" || filter === "open") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Open positions</h2>
            <span className="panel-meta">{OPEN.length} active · $1,080 deployed</span>
          </div>

          <table className="table-simple">
            <thead>
              <tr>
                <th>Market</th>
                <th>Side</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>Size</th>
                <th>P&amp;L</th>
                <th>p_win</th>
                <th>Closes</th>
              </tr>
            </thead>
            <tbody>
              {OPEN.map((p, i) => (
                <tr key={i}>
                  <td>{p.q}</td>
                  <td><span className={p.side === "YES" ? "pill pill-yes" : "pill pill-no"}>{p.side}</span></td>
                  <td className="mono">{p.entry}¢</td>
                  <td className="mono">{p.mark}¢</td>
                  <td className="mono">${p.size}</td>
                  <td className={`mono ${p.pnl >= 0 ? "cell-up" : "cell-down"}`}>
                    {p.pnl >= 0 ? "+" : ""}${p.pnl.toFixed(2)} <span style={{ opacity: 0.6 }}>({p.pnl >= 0 ? "+" : ""}{p.pnlPct.toFixed(1)}%)</span>
                  </td>
                  <td className="mono">{p.pWin.toFixed(2)}</td>
                  <td className="mono">{p.closes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(filter === "all" || filter === "scanning") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Delfi is scanning</h2>
            <span className="panel-meta">{SCAN.length} shortlisted · 384 evaluated today</span>
          </div>

          {SCAN.map((s, i) => (
            <div className="split-row" key={i}>
              <div className="split-body">
                <div className="split-title">{s.q}</div>
                <div className="split-desc">{s.reason}</div>
              </div>
              <div className="split-right">
                <div style={{ display: "flex", gap: 24, alignItems: "center" }}>
                  <div>
                    <div className="kv-label">Market</div>
                    <div className="mono" style={{ fontSize: 13 }}>{s.price}¢</div>
                  </div>
                  <div>
                    <div className="kv-label">Delfi</div>
                    <div className="mono" style={{ fontSize: 13, color: "var(--gold)" }}>{s.forecast}¢</div>
                  </div>
                  <div>
                    <div className="kv-label">Side · p_win</div>
                    <div className={`mono ${s.pWin >= 0.65 ? "cell-up" : "cell-down"}`} style={{ fontSize: 13 }}>
                      {s.side} · {s.pWin.toFixed(2)}
                    </div>
                  </div>
                  <div>
                    <div className="kv-label">Closes</div>
                    <div className="mono" style={{ fontSize: 13 }}>{s.closes}</div>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {filter === "recent" && (
        <div className="empty-state">
          Recently resolved positions will appear here once markets settle.
        </div>
      )}
    </div>
  );
}
