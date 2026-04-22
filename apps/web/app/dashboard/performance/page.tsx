"use client";

import { useEffect, useMemo, useState } from "react";
import "../../styles/content.css";

type Range = "7d" | "30d" | "90d" | "all";

type Summary = {
  bankroll: number | null;
  equity: number | null;
  starting_cash: number | null;
  realized_pnl: number | null;
  brier: number | null;
  win_rate: number | null;
  settled_total: number | null;
};

type BrierPoint = { date: string | null; brier: number; n: number };
type BrierPayload = { points: BrierPoint[] };

type Bin = {
  lo: number;
  hi: number;
  n: number;
  mean_pred: number | null;
  mean_actual: number | null;
};

type CalibrationPayload = {
  source: string;
  total: number;
  resolved: number;
  unresolved: number;
  brier: number | null;
  mean_prob: number | null;
  mean_outcome: number | null;
  realized_pnl_usd: number | null;
  bins: Bin[];
};

type BankrollPoint = { date: string; bankroll: number };

type Diagnostics = {
  system?: { bankroll_series?: BankrollPoint[] };
};

async function getJSON<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

function sliceRange(series: BankrollPoint[], range: Range): BankrollPoint[] {
  if (!series.length) return [];
  if (range === "all") return series;
  const days = range === "7d" ? 7 : range === "30d" ? 30 : 90;
  return series.slice(-Math.max(2, days));
}

export default function PerformancePage() {
  const [range, setRange] = useState<Range>("all");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [brier, setBrier] = useState<BrierPayload | null>(null);
  const [calibration, setCalibration] = useState<CalibrationPayload | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [s, b, c, d] = await Promise.all([
        getJSON<Summary>("/api/summary"),
        getJSON<BrierPayload>("/api/brier-trend?source=polymarket"),
        getJSON<CalibrationPayload>("/api/calibration?source=polymarket"),
        getJSON<Diagnostics>("/api/diagnostics?scope=all"),
      ]);
      if (cancelled) return;
      setSummary(s);
      setBrier(b);
      setCalibration(c);
      setDiag(d);
      setLoaded(true);
    };
    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const bankrollSeries = diag?.system?.bankroll_series ?? [];
  const sliced = useMemo(() => sliceRange(bankrollSeries, range), [bankrollSeries, range]);

  const startingCash = summary?.starting_cash ?? null;
  const equity = summary?.equity ?? summary?.bankroll ?? null;
  const realizedPnl = summary?.realized_pnl ?? 0;

  const roiPct = startingCash && startingCash > 0 && equity != null
    ? ((equity - startingCash) / startingCash) * 100
    : null;
  const totalPnl = equity != null && startingCash != null ? equity - startingCash : realizedPnl;

  const winRatePct = summary?.win_rate != null ? summary.win_rate * 100 : null;
  const brierScore = summary?.brier ?? calibration?.brier ?? null;

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Performance</h1>
            <p className="page-sub">Return on investment across the portfolio. ROI is the only metric that counts.</p>
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
          <div className="stat-cell-val">
            {roiPct != null ? `${roiPct >= 0 ? "+" : ""}${roiPct.toFixed(2)}%` : "—"}
          </div>
          <div className="stat-cell-delta">Net of costs</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">P&amp;L</div>
          <div className="stat-cell-val">
            {totalPnl != null ? `${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}` : "—"}
          </div>
          <div className="stat-cell-delta">Realized + unrealized</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Win rate</div>
          <div className="stat-cell-val">
            {winRatePct != null ? `${winRatePct.toFixed(0)}%` : "—"}
          </div>
          <div className="stat-cell-delta">
            {summary?.settled_total != null ? `${summary.settled_total} settled` : "Diagnostic"}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Brier score</div>
          <div className="stat-cell-val">
            {brierScore != null ? brierScore.toFixed(3) : "—"}
          </div>
          <div className="stat-cell-delta">Lower is better</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Equity curve</h2>
          <span className="panel-meta">
            {sliced.length > 0 ? `${sliced.length} days` : "No data"}
          </span>
        </div>
        {sliced.length > 1 ? (
          <EquityChart data={sliced.map((p) => p.bankroll)} />
        ) : (
          <div className="empty-state" style={{ padding: 40 }}>
            {loaded
              ? "Not enough history to draw an equity curve yet."
              : "Loading..."}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Brier trend</h2>
          <span className="panel-meta">
            {brier?.points.length ? `${brier.points.length} resolved predictions` : "No data"}
          </span>
        </div>
        <p className="panel-body" style={{ marginBottom: 12 }}>
          Running Brier score as resolutions come in. Lower is better; 0.25 is chance-level on binary
          markets. A trend downward means Delfi's forecasts are getting more accurate over time.
        </p>
        {brier && brier.points.length > 1 ? (
          <BrierChart data={brier.points} />
        ) : (
          <div className="empty-state" style={{ padding: 40 }}>
            {loaded
              ? "Waiting for at least two resolved predictions."
              : "Loading..."}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Calibration</h2>
          <span className="panel-meta">
            {calibration?.resolved != null ? `${calibration.resolved} resolved` : "Predicted vs. realized"}
          </span>
        </div>
        <p className="panel-body" style={{ marginBottom: 20 }}>
          When Delfi says 70%, does it happen 70% of the time? Each row shows its forecasts grouped into
          probability buckets. A well-calibrated forecaster is within 5 percentage points across all buckets
          once sample size is meaningful (n ≥ 20).
        </p>
        {calibration && calibration.bins.length > 0 ? (
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
              {calibration.bins.map((b, i) => {
                const expected = b.mean_pred != null ? b.mean_pred * 100 : null;
                const actual = b.mean_actual != null ? b.mean_actual * 100 : null;
                const delta = expected != null && actual != null ? actual - expected : null;
                const label = `${Math.round(b.lo * 100)}-${Math.round(b.hi * 100)}%`;
                return (
                  <tr key={i}>
                    <td>{label}</td>
                    <td className="mono">{expected != null ? `${expected.toFixed(0)}%` : "—"}</td>
                    <td className="mono">{actual != null ? `${actual.toFixed(0)}%` : "—"}</td>
                    <td className={`mono ${delta != null && Math.abs(delta) > 5 ? "cell-down" : ""}`}>
                      {delta != null
                        ? `${delta >= 0 ? "+" : ""}${delta.toFixed(0)} pts`
                        : "—"}
                    </td>
                    <td className="mono">{b.n}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="empty-state" style={{ padding: 32 }}>
            {loaded
              ? "No resolved predictions yet — calibration buckets populate after markets settle."
              : "Loading..."}
          </div>
        )}
      </div>
    </div>
  );
}

function EquityChart({ data }: { data: number[] }) {
  const w = 1000, h = 260, pad = 8;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const step = (w - pad * 2) / Math.max(1, data.length - 1);
  const points = data.map((v, i) => {
    const x = pad + i * step;
    const y = pad + (h - pad * 2) * (1 - (v - min) / range);
    return [x, y] as const;
  });
  const line = points
    .map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1))
    .join(" ");
  const area =
    line +
    ` L ${points[points.length - 1][0].toFixed(1)} ${h - pad} L ${points[0][0].toFixed(1)} ${h - pad} Z`;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", display: "block" }} preserveAspectRatio="none">
      <defs>
        <linearGradient id="perf-eq-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--gold)" stopOpacity="0.28" />
          <stop offset="100%" stopColor="var(--gold)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#perf-eq-fill)" />
      <path
        d={line}
        fill="none"
        stroke="var(--gold)"
        strokeWidth="1.6"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

function BrierChart({ data }: { data: BrierPoint[] }) {
  const w = 1000, h = 220, pad = 8;
  const values = data.map((p) => p.brier);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const step = (w - pad * 2) / Math.max(1, data.length - 1);
  const points = data.map((p, i) => {
    const x = pad + i * step;
    const y = pad + (h - pad * 2) * ((p.brier - min) / range);
    return [x, y] as const;
  });
  const line = points
    .map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1))
    .join(" ");

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", display: "block" }} preserveAspectRatio="none">
      <path
        d={line}
        fill="none"
        stroke="var(--teal)"
        strokeWidth="1.6"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
