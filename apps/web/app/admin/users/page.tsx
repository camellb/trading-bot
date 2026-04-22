"use client";

import { useState } from "react";

type User = {
  id: string;
  name: string;
  email: string;
  plan: "Free" | "Pro" | "Premium";
  bankroll: number;
  pnl: number;
  mode: "simulation" | "live";
  status: "active" | "halted" | "churned";
  joined: string;
};

const USERS: User[] = [
  { id: "u_41a0", name: "Alex Morgan",   email: "alex@morgan.co",   plan: "Pro",     bankroll: 14827, pnl: 4827,   mode: "live",       status: "active",  joined: "2026-02-11" },
  { id: "u_1e8f", name: "Priya Sharma",  email: "priya@hey.io",     plan: "Pro",     bankroll: 8410,  pnl: -612,   mode: "live",       status: "halted",  joined: "2026-03-04" },
  { id: "u_88ac", name: "Noah Tanaka",   email: "noah@foo.bar",     plan: "Free",    bankroll: 1000,  pnl: 84,     mode: "simulation", status: "active",  joined: "2026-04-21" },
  { id: "u_72ed", name: "Sara Al-Mansour", email: "sara@veld.app",  plan: "Premium", bankroll: 52000, pnl: 11240,  mode: "live",       status: "active",  joined: "2026-01-18" },
  { id: "u_5c12", name: "Diego Hernandez", email: "d@hernandez.mx", plan: "Pro",     bankroll: 3210,  pnl: 180,    mode: "live",       status: "active",  joined: "2026-03-22" },
  { id: "u_22bb", name: "Mira Kowalski", email: "mira@kw.pl",       plan: "Free",    bankroll: 500,   pnl: 12,     mode: "simulation", status: "churned", joined: "2026-01-08" },
];

export default function AdminUsersPage() {
  const [q, setQ] = useState("");
  const filtered = USERS.filter((u) =>
    !q ||
    u.name.toLowerCase().includes(q.toLowerCase()) ||
    u.email.toLowerCase().includes(q.toLowerCase()) ||
    u.id.toLowerCase().includes(q.toLowerCase())
  );

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Users</h1>
            <p className="page-sub">All accounts, their bankroll, and their current state.</p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm">Export CSV</button>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div style={{ flex: 1, maxWidth: 360 }}>
          <input
            className="ob-input"
            placeholder="Search by name, email, or id"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">{filtered.length} of {USERS.length} users</h2>
        </div>

        <table className="table-simple">
          <thead>
            <tr>
              <th>ID</th>
              <th>Name</th>
              <th>Plan</th>
              <th>Mode</th>
              <th>Bankroll</th>
              <th>P&amp;L</th>
              <th>Joined</th>
              <th>Status</th>
              <th style={{ textAlign: "right" }}></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((u) => (
              <tr key={u.id}>
                <td className="mono" style={{ color: "var(--vellum-60)" }}>{u.id}</td>
                <td>
                  <div>{u.name}</div>
                  <div className="split-desc">{u.email}</div>
                </td>
                <td>{u.plan}</td>
                <td>
                  <span className={`pill ${u.mode === "live" ? "pill-no" : "pill-open"}`}>
                    {u.mode === "live" ? "LIVE" : "SIM"}
                  </span>
                </td>
                <td className="mono">${u.bankroll.toLocaleString()}</td>
                <td className={`mono ${u.pnl >= 0 ? "cell-up" : "cell-down"}`}>
                  {u.pnl >= 0 ? "+" : ""}${u.pnl.toLocaleString()}
                </td>
                <td className="mono">{u.joined}</td>
                <td>
                  <span className={`pill ${u.status === "active" ? "pill-won" : u.status === "halted" ? "pill-lost" : "pill-skip"}`}>
                    {u.status}
                  </span>
                </td>
                <td style={{ textAlign: "right" }}>
                  <button className="btn-sm">View</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
