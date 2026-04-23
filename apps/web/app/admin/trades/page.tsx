"use client";

import { useState } from "react";

type Status = "open" | "won" | "lost" | "skipped";

type Trade = {
  id: string;
  user: string;
  market: string;
  side: "YES" | "NO";
  entry: number;
  mark: number;
  size: number;
  pnl: number;
  status: Status;
  when: string;
};

const TRADES: Trade[] = [
  { id: "trd_9a21", user: "u_41a0", market: "Fed cuts rates by May 2026",       side: "YES", entry: 0.42, mark: 0.51, size: 420, pnl: +37.80, status: "open",    when: "2026-04-21 13:42" },
  { id: "trd_9a1f", user: "u_72ed", market: "BTC above $120k end of Q2",         side: "NO",  entry: 0.31, mark: 0.27, size: 1040, pnl: +41.60, status: "open",    when: "2026-04-21 12:11" },
  { id: "trd_9a1a", user: "u_1e8f", market: "UK election called before July",    side: "YES", entry: 0.18, mark: 0.22, size: 220, pnl: +8.80,  status: "won",     when: "2026-04-21 09:04" },
  { id: "trd_9a12", user: "u_5c12", market: "ETH ETF approved in April",         side: "NO",  entry: 0.68, mark: 0.55, size: 310, pnl: -40.30, status: "lost",    when: "2026-04-20 22:48" },
  { id: "trd_9a0b", user: "u_41a0", market: "Oil above $95 by month end",        side: "YES", entry: 0.47, mark: 0.47, size: 200, pnl: 0.00,   status: "skipped", when: "2026-04-20 17:22" },
  { id: "trd_9a03", user: "u_72ed", market: "Taiwan election outcome - DPP win", side: "YES", entry: 0.60, mark: 0.74, size: 820, pnl: +114.80, status: "won",    when: "2026-04-20 10:33" },
];

const STATUS_CLASS: Record<Status, string> = {
  open: "pill-open",
  won: "pill-won",
  lost: "pill-lost",
  skipped: "pill-skip",
};

export default function AdminTradesPage() {
  const [filter, setFilter] = useState<"all" | Status>("all");

  const filtered = TRADES.filter((t) => filter === "all" || t.status === filter);

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Trades</h1>
            <p className="page-sub">Every trade across every user, with status and P&amp;L.</p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm">Export CSV</button>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div className="tab-bar" style={{ padding: 0, margin: 0 }}>
          {(["all", "open", "won", "lost", "skipped"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setFilter(k)}
              className={`tab ${filter === k ? "on" : ""}`}
              style={{ textTransform: "capitalize" }}
            >
              {k}
            </button>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">{filtered.length} of {TRADES.length} trades</h2>
        </div>

        <table className="table-simple">
          <thead>
            <tr>
              <th>ID</th>
              <th>User</th>
              <th>Market</th>
              <th>Side</th>
              <th>Entry</th>
              <th>Mark</th>
              <th>Size</th>
              <th>P&amp;L</th>
              <th>Status</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((t) => (
              <tr key={t.id}>
                <td className="mono" style={{ color: "var(--vellum-60)" }}>{t.id}</td>
                <td className="mono" style={{ color: "var(--vellum-60)" }}>{t.user}</td>
                <td>{t.market}</td>
                <td>
                  <span className={`pill ${t.side === "YES" ? "pill-yes" : "pill-no"}`}>{t.side}</span>
                </td>
                <td className="mono">{t.entry.toFixed(2)}</td>
                <td className="mono">{t.mark.toFixed(2)}</td>
                <td className="mono">${t.size.toLocaleString()}</td>
                <td className={`mono ${t.pnl > 0 ? "cell-up" : t.pnl < 0 ? "cell-down" : ""}`}>
                  {t.pnl > 0 ? "+" : ""}${t.pnl.toFixed(2)}
                </td>
                <td>
                  <span className={`pill ${STATUS_CLASS[t.status]}`}>{t.status}</span>
                </td>
                <td className="mono" style={{ color: "var(--vellum-60)" }}>{t.when}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
