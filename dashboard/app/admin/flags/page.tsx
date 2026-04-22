"use client";

import { useState } from "react";

type Flag = {
  key: string;
  label: string;
  desc: string;
  on: boolean;
  scope: "global" | "cohort" | "user";
  owner: string;
};

const INITIAL: Flag[] = [
  { key: "longshot_no_strategy",   label: "Longshot-NO strategy",        desc: "Enable systematic NO bets on markets between 3-8% with sufficient liquidity.",           on: true,  scope: "global", owner: "trading" },
  { key: "cross_market_arb",       label: "Cross-market arbitrage",      desc: "Detect mutually exclusive outcome sets that sum under 1.00 and lock in the discount.",   on: true,  scope: "global", owner: "trading" },
  { key: "microstructure_revert",  label: "Microstructure reversion",    desc: "Bet partial reversion after sharp moves with no corresponding news.",                     on: false, scope: "global", owner: "trading" },
  { key: "ensemble_v3",            label: "Ensemble v3 forecaster",      desc: "Route probability estimation through the v3 ensemble (Opus + Sonnet + Haiku).",          on: true,  scope: "global", owner: "research" },
  { key: "calibration_weekly",     label: "Weekly calibration suggestions", desc: "Surface calibration drift suggestions to users in the weekly review email.",          on: true,  scope: "global", owner: "product" },
  { key: "wallet_onboarding_v2",   label: "Wallet onboarding v2",        desc: "New signup flow with embedded smart wallet and scoped delegation.",                      on: false, scope: "cohort", owner: "growth" },
  { key: "admin_shadow_console",   label: "Operator shadow console",     desc: "Expose the live decision stream inside the admin panel for debugging.",                 on: true,  scope: "user",   owner: "platform" },
  { key: "hard_halt_drawdown",     label: "Hard halt on 40% drawdown",   desc: "Automatic halt of new positions when a user crosses 40% drawdown from peak.",            on: true,  scope: "global", owner: "trading" },
];

export default function AdminFlagsPage() {
  const [flags, setFlags] = useState<Flag[]>(INITIAL);

  const toggle = (key: string) =>
    setFlags((fs) => fs.map((f) => (f.key === key ? { ...f, on: !f.on } : f)));

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Feature flags</h1>
            <p className="page-sub">Toggle system-wide capabilities. Changes take effect within 30 seconds.</p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm">View audit log</button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Active flags</h2>
          <span className="panel-meta">{flags.filter((f) => f.on).length} of {flags.length} on</span>
        </div>

        {flags.map((f) => (
          <div className="split-row" key={f.key}>
            <div className="split-body">
              <div className="split-title">
                <span className="mono" style={{ color: "var(--vellum-40)", marginRight: 10, fontSize: 12 }}>
                  {f.key}
                </span>
                {f.label}
              </div>
              <div className="split-desc">{f.desc}</div>
              <div style={{ marginTop: 8, display: "flex", gap: 10 }}>
                <span className="pill pill-open" style={{ textTransform: "capitalize" }}>{f.scope}</span>
                <span className="pill pill-skip">owner · {f.owner}</span>
              </div>
            </div>
            <div className="split-right">
              <label className="toggle-switch">
                <input type="checkbox" checked={f.on} onChange={() => toggle(f.key)} />
                <span className="toggle-slider"></span>
              </label>
            </div>
          </div>
        ))}
      </div>
    </main>
  );
}
