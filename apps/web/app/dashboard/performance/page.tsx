"use client";

import { useState } from "react";
import "../../styles/content.css";

type Range = "7d" | "30d" | "90d" | "all";

const EQUITY: Record<Range, number[]> = {
  "7d": [14200, 14280, 14310, 14420, 14510, 14680, 14827],
  "30d": [13100, 13240, 13380, 13440, 13510, 13620, 13750, 13810, 13900, 13990, 14080, 14160, 14210, 14280, 14340, 14380, 14420, 14470, 14510, 14560, 14610, 14640, 14680, 14710, 14750, 14780, 14800, 14815, 14820, 14827],
  "90d": [11200, 11400, 11580, 11720, 11890, 12040, 12220, 12380, 12510, 12660, 12820, 12950, 13080, 13210, 13340, 13470, 13590, 13680, 13780, 13860, 13950, 14020, 14100, 14180, 14240, 14320, 14380, 14440, 14510, 14570, 14620, 14680, 14720, 14760, 14790, 14810, 14820, 14823, 14825, 14827],
  "all": [10000, 10120, 10220, 10340, 10460, 10580, 10710, 10840, 10990, 11150, 11320, 11480, 11620, 11790, 11940, 12080, 12220, 12380, 12510, 12660, 12820, 12950, 13080, 13210, 13340, 13470, 13590, 13680, 13780, 13860, 13950, 14020, 14100, 14180, 14240, 14320, 14380, 14440, 14510, 14570, 14620, 14680, 14720, 14760, 14790, 14810, 14820, 14823, 14825, 14827],
};

const STRATS = [
  { name: "Three-gate forecast", trades: 68, winRate: 64, roi: 18.2, brier: 0.081, contribution: 48 },
  { name: "Longshot NO", trades: 22, winRate: 82, roi: 12.4, brier: 0.072, contribution: 22 },
  { name: "Cross-market arbitrage", trades: 14, winRate: 100, roi: 4.1, brier: 0.000, contribution: 8 },
  { name: "Microstructure reversion", trades: 18, winRate: 58, roi: 3.2, brier: 0.095, contribution: 6 },
];

const BUCKETS = [
  { label: "0-10%", expected: 5, actual: 4, count: 22 },
  { label: "10-20%", expected: 15, actual: 13, count: 34 },
  { label: "20-30%", expected: 25, actual: 28, count: 28 },
  { label: "30-40%", expected: 35, actual: 32, count: 24 },
  { label: "40-50%", expected: 45, actual: 48, count: 18 },
  { label: "50-60%", expected: 55, actual: 56, count: 16 },
  { label: "60-70%", expected: 65, actual: 69, count: 22 },
  { label: "70-80%", expected: 75, actual: 78, count: 19 },
  { label: "80-90%", expected: 85, actual: 88, count: 15 },
  { label: "90-100%", expected: 95, actual: 97, count: 11 },
];

export default function PerformancePage() {
  const [range, setRange] = useState<Range>("all");
  const data = EQUITY[range];
  const start = data[0];
  const end = data[data.length - 1];
  const delta = end - start;
  const deltaPct = ((end - start) / start) * 100;

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Performance</h1>
            <p className="page-sub">Return on investment across the portfolio. ROI is the only metric that counts.</p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm">Export report</button>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div className="page-toolbar-left">
          {(["7d", "30d", "90d", "all"] as Range[]).map((r) => (
            <button key={r} className={`chip ${range === r ? "on" : ""}`} onClick={() => setRange(r)}>
              {r === "all" ? "Since start" : r.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="stat-row">
        <div className="stat-cell">
          <div className="stat-cell-label">ROI</div>
          <div className="stat-cell-val">+{deltaPct.toFixed(2)}%</div>
          <div className="stat-cell-delta">Net of costs</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">P&amp;L</div>
          <div className="stat-cell-val">+${delta.toLocaleString()}</div>
          <div className="stat-cell-delta">Realized + unrealized</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Win rate</div>
          <div className="stat-cell-val">66%</div>
          <div className="stat-cell-delta">Diagnostic</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Brier score</div>
          <div className="stat-cell-val">0.083</div>
          <div className="stat-cell-delta">Lower is better</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Equity curve</h2>
          <span className="panel-meta">{data.length} days</span>
        </div>
        <EquityChart data={data} />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Strategy attribution</h2>
          <span className="panel-meta">ROI by approach</span>
        </div>
        <table className="table-simple">
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Trades</th>
              <th>Win rate</th>
              <th>ROI</th>
              <th>Brier</th>
              <th>% of P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {STRATS.map((s, i) => (
              <tr key={i}>
                <td>{s.name}</td>
                <td className="mono">{s.trades}</td>
                <td className="mono">{s.winRate}%</td>
                <td className="mono cell-up">+{s.roi.toFixed(1)}%</td>
                <td className="mono">{s.brier.toFixed(3)}</td>
                <td className="mono">{s.contribution}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Calibration</h2>
          <span className="panel-meta">Predicted vs. realized</span>
        </div>
        <p className="panel-body" style={{ marginBottom: 20 }}>
          When Delfi says 70%, does it happen 70% of the time? Each row shows its forecasts grouped into
          probability buckets. A well-calibrated forecaster is within 5 percentage points across all buckets
          once sample size is meaningful (n ≥ 20).
        </p>
        <table className="table-simple">
          <thead>
            <tr>
              <th>Bucket</th>
              <th>Expected</th>
              <th>Actual</th>
              <th>Delta</th>
              <th>Sample</th>
            </tr>
          </thead>
          <tbody>
            {BUCKETS.map((b, i) => {
              const d = b.actual - b.expected;
              return (
                <tr key={i}>
                  <td>{b.label}</td>
                  <td className="mono">{b.expected}%</td>
                  <td className="mono">{b.actual}%</td>
                  <td className={`mono ${Math.abs(d) > 5 ? "cell-down" : ""}`}>
                    {d >= 0 ? "+" : ""}{d} pts
                  </td>
                  <td className="mono">{b.count}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function EquityChart({ data }: { data: number[] }) {
  const w = 1000, h = 260, pad = 8;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const step = (w - pad * 2) / (data.length - 1);
  const points = data.map((v, i) => {
    const x = pad + i * step;
    const y = pad + (h - pad * 2) * (1 - (v - min) / range);
    return [x, y] as const;
  });
  const line = points.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const area = line + ` L ${points[points.length - 1][0].toFixed(1)} ${h - pad} L ${points[0][0].toFixed(1)} ${h - pad} Z`;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", display: "block" }} preserveAspectRatio="none">
      <defs>
        <linearGradient id="perf-eq-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--gold)" stopOpacity="0.28" />
          <stop offset="100%" stopColor="var(--gold)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#perf-eq-fill)" />
      <path d={line} fill="none" stroke="var(--gold)" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
