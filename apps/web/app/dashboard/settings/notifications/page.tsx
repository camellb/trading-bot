"use client";

import { useState } from "react";

type Prefs = {
  dailyDigest: boolean;
  weeklyReview: boolean;
  bigWins: boolean;
  bigLosses: boolean;
  riskEvents: boolean;
  calibrationSuggestions: boolean;
  productUpdates: boolean;
  pushTrades: boolean;
  pushRisk: boolean;
};

const DEFAULTS: Prefs = {
  dailyDigest: true,
  weeklyReview: true,
  bigWins: false,
  bigLosses: true,
  riskEvents: true,
  calibrationSuggestions: true,
  productUpdates: false,
  pushTrades: false,
  pushRisk: true,
};

export default function NotificationsPage() {
  const [p, setP] = useState<Prefs>(DEFAULTS);
  const t = (k: keyof Prefs) => setP((prev) => ({ ...prev, [k]: !prev[k] }));

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Email</h2>
          <span className="panel-meta">What lands in your inbox</span>
        </div>

        <Toggle label="Daily digest" desc="A short morning summary of yesterday's trades and today's focus." on={p.dailyDigest} onChange={() => t("dailyDigest")} />
        <Toggle label="Weekly review" desc="Every Sunday evening. Performance, calibration, suggestions." on={p.weeklyReview} onChange={() => t("weeklyReview")} />
        <Toggle label="Meaningful wins" desc="Trades resolving above +$100 P&L get a one-line email with reasoning." on={p.bigWins} onChange={() => t("bigWins")} />
        <Toggle label="Meaningful losses" desc="Trades resolving below -$100 P&L get an honest post-mortem email." on={p.bigLosses} onChange={() => t("bigLosses")} />
        <Toggle label="Risk events" desc="Daily cap, drawdown halt, or streak cooldown triggered." on={p.riskEvents} onChange={() => t("riskEvents")} />
        <Toggle label="Calibration suggestions" desc="Delfi found a pattern worth a config change, with backtest evidence." on={p.calibrationSuggestions} onChange={() => t("calibrationSuggestions")} />
        <Toggle label="Product updates" desc="Occasional emails about new features and strategies. Not marketing." on={p.productUpdates} onChange={() => t("productUpdates")} />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Push notifications</h2>
          <span className="panel-meta">Mobile and browser</span>
        </div>
        <Toggle label="Each trade" desc="Push a notification when Delfi opens or closes a position." on={p.pushTrades} onChange={() => t("pushTrades")} />
        <Toggle label="Risk events" desc="Push when a risk guardrail engages." on={p.pushRisk} onChange={() => t("pushRisk")} />

        <div style={{ marginTop: 16 }}>
          <button className="btn-sm">Enable browser push</button>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Quiet hours</h2>
          <span className="panel-meta">No notifications during these hours</span>
        </div>
        <p className="panel-body">
          Daily digest and weekly review still arrive on schedule, but push notifications and loss post-mortems
          will hold until your quiet hours end.
        </p>
        <div className="form-row" style={{ flexDirection: "row", gap: 16, maxWidth: "100%" }}>
          <div className="form-field" style={{ flex: 1 }}>
            <label>From</label>
            <input type="time" defaultValue="22:00" />
          </div>
          <div className="form-field" style={{ flex: 1 }}>
            <label>To</label>
            <input type="time" defaultValue="07:00" />
          </div>
        </div>
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 12 }}>
        <button className="btn-sm">Cancel</button>
        <button className="btn-sm gold">Save preferences</button>
      </div>
    </>
  );
}

function Toggle({ label, desc, on, onChange }: { label: string; desc: string; on: boolean; onChange: () => void }) {
  return (
    <div className="split-row">
      <div className="split-body">
        <div className="split-title">{label}</div>
        <div className="split-desc">{desc}</div>
      </div>
      <div className="split-right">
        <label className="toggle-switch">
          <input type="checkbox" checked={on} onChange={onChange} />
          <span className="toggle-slider"></span>
        </label>
      </div>
    </div>
  );
}
