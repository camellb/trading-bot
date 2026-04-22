"use client";

import { useState } from "react";

const SESSIONS = [
  { device: "MacBook Pro · Chrome", location: "New York, US", ip: "73.22.18.xx", when: "Active now", current: true },
  { device: "iPhone 15 · Safari", location: "New York, US", ip: "10.0.1.xx", when: "2 hours ago", current: false },
  { device: "Windows · Firefox", location: "Austin, US", ip: "68.14.22.xx", when: "3 days ago", current: false },
];

export default function SecurityPage() {
  const [twoFA, setTwoFA] = useState(true);
  const [current, setCurrent] = useState("");
  const [next1, setNext1] = useState("");
  const [next2, setNext2] = useState("");

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Password</h2>
          <span className="panel-meta">Change your password</span>
        </div>

        <div className="form-row">
          <div className="form-field">
            <label>Current password</label>
            <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} />
          </div>
          <div className="form-field">
            <label>New password</label>
            <input type="password" value={next1} onChange={(e) => setNext1(e.target.value)} />
            <div className="form-hint">Minimum 12 characters. Use a passphrase or a password manager.</div>
          </div>
          <div className="form-field">
            <label>Confirm new password</label>
            <input type="password" value={next2} onChange={(e) => setNext2(e.target.value)} />
          </div>
          <div style={{ marginTop: 12 }}>
            <button className="btn-sm gold">Update password</button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Two-factor authentication</h2>
          <span className="panel-meta">Required for live trading</span>
        </div>

        <div className="split-row">
          <div className="split-body">
            <div className="split-title">Authenticator app</div>
            <div className="split-desc">
              Use a TOTP app like 1Password, Authy, or Google Authenticator. Required when live trading is
              enabled and strongly recommended always.
            </div>
          </div>
          <div className="split-right">
            <label className="toggle-switch">
              <input type="checkbox" checked={twoFA} onChange={() => setTwoFA((v) => !v)} />
              <span className="toggle-slider"></span>
            </label>
          </div>
        </div>

        <div className="split-row">
          <div className="split-body">
            <div className="split-title">Backup codes</div>
            <div className="split-desc">One-time codes to recover access if you lose your authenticator.</div>
          </div>
          <div className="split-right">
            <button className="btn-sm">View codes</button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Active sessions</h2>
          <span className="panel-meta">Where you're signed in</span>
        </div>

        <table className="table-simple">
          <thead>
            <tr>
              <th>Device</th>
              <th>Location</th>
              <th>IP</th>
              <th>Last seen</th>
              <th style={{ textAlign: "right" }}></th>
            </tr>
          </thead>
          <tbody>
            {SESSIONS.map((s, i) => (
              <tr key={i}>
                <td>
                  {s.device}
                  {s.current && <span className="pill pill-open" style={{ marginLeft: 8 }}>Current</span>}
                </td>
                <td>{s.location}</td>
                <td className="mono">{s.ip}</td>
                <td>{s.when}</td>
                <td style={{ textAlign: "right" }}>
                  {!s.current && <button className="btn-sm danger">Revoke</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div style={{ marginTop: 16 }}>
          <button className="btn-sm danger">Sign out all other sessions</button>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Trading delegation</h2>
          <span className="panel-meta">Smart wallet authorization</span>
        </div>

        <div className="kv-grid">
          <div className="kv-label">Scope</div>
          <div className="kv-val">Polymarket · Buy &amp; sell · No withdrawal</div>
          <div className="kv-label">Authorized on</div>
          <div className="kv-val mono">2026-02-11</div>
          <div className="kv-label">Expires</div>
          <div className="kv-val mono">2026-08-11</div>
          <div className="kv-label">Spend cap</div>
          <div className="kv-val">$20,000 per 30-day period</div>
        </div>

        <div style={{ marginTop: 20, display: "flex", gap: 12 }}>
          <button className="btn-sm">Renew delegation</button>
          <button className="btn-sm danger">Revoke</button>
        </div>
      </div>
    </>
  );
}
