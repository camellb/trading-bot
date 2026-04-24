"use client";

import { useEffect, useMemo, useState } from "react";
import { getJSON } from "@/lib/fetch-json";
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

function sliceRange(series: BankrollPoint[], range: Range): BankrollPoint[] {
  if (!series.length) return [];
  if (range === "all") return series;
  const days = range === "7d" ? 7 : range === "30d" ? 30 : 90;
  return series.slice(-Math.max(2, days));
}

function formatShortDate(s: string): string {
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatMoney(v: number): string {
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs >= 1000) {
    return `${sign}$${abs.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  }
  return `${sign}$${abs.toFixed(2)}`;
}

export default function PerformancePage() {
  const [range, setRange] = useState<Range>("all");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [calibration, setCalibration] = useState<CalibrationPayload | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [s, c, d] = await Promise.all([
        getJSON<Summary>("/api/summary"),
        getJSON<CalibrationPayload>("/api/calibration?source=polymarket"),
        getJSON<Diagnostics>("/api/diagnostics?scope=all"),
      ]);
      if (cancelled) return;
      setSummary(s);
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

  const calBins = useMemo(
    () => (calibration?.bins ?? []).filter((b) => b.n > 0),
    [calibration]
  );

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
            {roiPct != null ? `${roiPct >= 0 ? "+" : ""}${roiPct.toFixed(2)}%` : "-"}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">P&amp;L</div>
          <div className="stat-cell-val">
            {totalPnl != null ? `${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}` : "-"}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Win rate</div>
          <div className="stat-cell-val">
            {winRatePct != null ? `${winRatePct.toFixed(0)}%` : "-"}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Brier score</div>
          <div className="stat-cell-val">
            {brierScore != null ? brierScore.toFixed(3) : "-"}
          </div>
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
          <EquityChart points={sliced} />
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
          <h2 className="panel-title">Is Delfi's confidence honest?</h2>
          <span className="panel-meta">
            {calibration?.resolved != null ? `${calibration.resolved} resolved` : "Waiting"}
          </span>
        </div>
        <p className="panel-body" style={{ marginBottom: 20 }}>
          When Delfi says it's 70% confident a market will resolve YES, those trades
          should win roughly 70% of the time. Each row below groups your settled
          trades by how confident Delfi was, then shows how often they actually won.
          A gap under 10 points once a group has 20+ trades means Delfi's confidence
          is honest in that range.
        </p>
        {calBins.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {calBins.map((b, i) => {
              const expected = b.mean_pred != null ? b.mean_pred * 100 : null;
              const actual = b.mean_actual != null ? b.mean_actual * 100 : null;
              const delta = expected != null && actual != null ? actual - expected : null;
              const rangeLabel = `${Math.round(b.lo * 100)}-${Math.round(b.hi * 100)}%`;
              const smallSample = b.n < 20;
              const wide = delta != null && Math.abs(delta) > 10;
              const statusText = smallSample
                ? "Too few trades to tell"
                : wide
                ? "Off target"
                : "On target";
              const statusColor = smallSample
                ? "rgba(255,255,255,0.45)"
                : wide
                ? "var(--red, #e56b6f)"
                : "var(--teal, #4bd0c4)";
              const winsWord = b.n === 1 ? "trade" : "trades";
              return (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    padding: "14px 16px",
                    border: "1px solid rgba(255,255,255,0.06)",
                    borderRadius: 10,
                    gap: 16,
                    flexWrap: "wrap",
                  }}
                >
                  <div style={{ minWidth: 0, flex: "1 1 260px" }}>
                    <div style={{ fontSize: 14, color: "rgba(255,255,255,0.95)" }}>
                      When Delfi said it was{" "}
                      <strong style={{ color: "var(--gold)" }}>{rangeLabel}</strong>{" "}
                      confident
                    </div>
                    <div style={{ fontSize: 13, color: "rgba(255,255,255,0.65)", marginTop: 4 }}>
                      Those markets actually won{" "}
                      <strong style={{ color: "rgba(255,255,255,0.95)" }}>
                        {actual != null ? `${actual.toFixed(0)}%` : "-"}
                      </strong>{" "}
                      of the time across {b.n} {winsWord}.
                    </div>
                  </div>
                  <div style={{ textAlign: "right", fontSize: 12, color: statusColor, fontFamily: "ui-monospace, SFMono-Regular, monospace" }}>
                    {statusText}
                    {delta != null && !smallSample ? (
                      <div style={{ fontSize: 11, color: "rgba(255,255,255,0.55)", marginTop: 2 }}>
                        Gap: {delta >= 0 ? "+" : ""}{delta.toFixed(0)} pts
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="empty-state" style={{ padding: 32 }}>
            {loaded
              ? "No resolved predictions yet. Rows appear once your first markets settle."
              : "Loading..."}
          </div>
        )}
      </div>
    </div>
  );
}

function EquityChart({ points }: { points: BankrollPoint[] }) {
  const w = 1000;
  const h = 300;
  const padL = 72;
  const padR = 16;
  const padT = 16;
  const padB = 36;
  const values = points.map((p) => p.bankroll);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;
  const step = plotW / Math.max(1, points.length - 1);
  const coords = points.map((p, i) => {
    const x = padL + i * step;
    const y = padT + plotH * (1 - (p.bankroll - min) / range);
    return [x, y] as const;
  });
  const line = coords
    .map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1))
    .join(" ");
  const area =
    line +
    ` L ${coords[coords.length - 1][0].toFixed(1)} ${padT + plotH} L ${coords[0][0].toFixed(1)} ${padT + plotH} Z`;

  const mid = (min + max) / 2;
  const yTicks = [
    { v: max, y: padT },
    { v: mid, y: padT + plotH / 2 },
    { v: min, y: padT + plotH },
  ];

  const lastIdx = points.length - 1;
  const midIdx = Math.floor(lastIdx / 2);
  const xTicks = [
    { label: formatShortDate(points[0].date), x: coords[0][0], anchor: "start" as const },
    { label: formatShortDate(points[midIdx].date), x: coords[midIdx][0], anchor: "middle" as const },
    { label: formatShortDate(points[lastIdx].date), x: coords[lastIdx][0], anchor: "end" as const },
  ];

  const lastPoint = coords[coords.length - 1];
  const lastValue = points[points.length - 1].bankroll;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", display: "block" }}>
      <defs>
        <linearGradient id="perf-eq-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--gold)" stopOpacity="0.28" />
          <stop offset="100%" stopColor="var(--gold)" stopOpacity="0" />
        </linearGradient>
      </defs>
      {yTicks.map((t, i) => (
        <line
          key={`g${i}`}
          x1={padL}
          x2={w - padR}
          y1={t.y}
          y2={t.y}
          stroke="rgba(255,255,255,0.06)"
          strokeDasharray="2 4"
        />
      ))}
      {yTicks.map((t, i) => (
        <text
          key={`y${i}`}
          x={padL - 8}
          y={t.y + 4}
          fill="rgba(255,255,255,0.55)"
          fontSize="11"
          textAnchor="end"
          fontFamily="ui-monospace, SFMono-Regular, monospace"
        >
          {formatMoney(t.v)}
        </text>
      ))}
      {xTicks.map((t, i) => (
        <text
          key={`x${i}`}
          x={t.x}
          y={h - 10}
          fill="rgba(255,255,255,0.55)"
          fontSize="11"
          textAnchor={t.anchor}
          fontFamily="ui-monospace, SFMono-Regular, monospace"
        >
          {t.label}
        </text>
      ))}
      <path d={area} fill="url(#perf-eq-fill)" />
      <path
        d={line}
        fill="none"
        stroke="var(--gold)"
        strokeWidth="1.6"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx={lastPoint[0]} cy={lastPoint[1]} r="3" fill="var(--gold)" />
      <text
        x={lastPoint[0] - 6}
        y={lastPoint[1] - 8}
        fill="var(--gold)"
        fontSize="11"
        textAnchor="end"
        fontFamily="ui-monospace, SFMono-Regular, monospace"
      >
        {formatMoney(lastValue)}
      </text>
    </svg>
  );
}
