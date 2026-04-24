"use client";

import { useEffect, useMemo, useState } from "react";
import { getJSON } from "@/lib/fetch-json";
import { useViewMode } from "@/lib/view-mode";
import { createClient } from "@/lib/supabase/client";
import "../../styles/content.css";

type Range = "7d" | "30d" | "90d" | "365d" | "all";

const RANGES: { key: Range; label: string }[] = [
  { key: "7d", label: "7 DAYS" },
  { key: "30d", label: "30 DAYS" },
  { key: "90d", label: "90 DAYS" },
  { key: "365d", label: "365 DAYS" },
  { key: "all", label: "ALL TIME" },
];

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

const RANGE_DAYS: Record<Exclude<Range, "all">, number> = {
  "7d": 7,
  "30d": 30,
  "90d": 90,
  "365d": 365,
};

function sliceRange(series: BankrollPoint[], range: Range): BankrollPoint[] {
  if (!series.length) return [];
  if (range === "all") return series;
  const days = RANGE_DAYS[range];
  const cutoffMs = Date.now() - days * 86_400_000;
  // Prefer filtering by actual timestamp when points have valid dates.
  const filtered = series.filter((p) => {
    const t = new Date(p.date).getTime();
    return Number.isFinite(t) && t >= cutoffMs;
  });
  if (filtered.length >= 2) return filtered;
  // Fallback: keep the tail if timestamp filtering yields too little.
  return series.slice(-Math.max(2, days));
}

/**
 * Prepend the user's join date as a zero-baseline point so the curve starts
 * at $0 on the day they joined. Also appends today's cumulative value so the
 * X-axis always runs user-join → today.
 */
function withBaseline(
  series: BankrollPoint[],
  joinedAt: Date | null,
): BankrollPoint[] {
  if (!series.length) {
    if (joinedAt) {
      const today = new Date().toISOString();
      return [
        { date: joinedAt.toISOString(), bankroll: 0 },
        { date: today, bankroll: 0 },
      ];
    }
    return [];
  }
  const out = series.slice();
  if (joinedAt) {
    const firstDate = new Date(out[0].date).getTime();
    if (!Number.isFinite(firstDate) || joinedAt.getTime() < firstDate - 60_000) {
      out.unshift({ date: joinedAt.toISOString(), bankroll: 0 });
    }
  }
  const last = out[out.length - 1];
  const lastDate = new Date(last.date).getTime();
  const now = Date.now();
  // Extend to today when the last settled point is older than ~6 hours.
  if (!Number.isFinite(lastDate) || now - lastDate > 6 * 3600 * 1000) {
    out.push({ date: new Date(now).toISOString(), bankroll: last.bankroll });
  }
  return out;
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
  const [joinedAt, setJoinedAt] = useState<Date | null>(null);

  useEffect(() => {
    let cancelled = false;
    const supabase = createClient();
    supabase.auth.getUser().then(({ data }) => {
      if (cancelled) return;
      const iso = data.user?.created_at;
      if (iso) {
        const d = new Date(iso);
        if (!Number.isNaN(d.getTime())) setJoinedAt(d);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const { version: viewModeVersion } = useViewMode();

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    setSummary(null);
    setCalibration(null);
    setDiag(null);
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
  }, [viewModeVersion]);

  const bankrollSeries = diag?.system?.bankroll_series ?? [];
  const baselined = useMemo(
    () => withBaseline(bankrollSeries, joinedAt),
    [bankrollSeries, joinedAt],
  );
  const sliced = useMemo(
    () => sliceRange(baselined, range),
    [baselined, range],
  );

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
          {RANGES.map((r) => (
            <button key={r.key} className={`chip ${range === r.key ? "on" : ""}`} onClick={() => setRange(r.key)}>
              {r.label}
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
          <h2 className="panel-title">Equity chart</h2>
          <span className="panel-meta">
            {sliced.length > 1 ? (() => {
              const first = new Date(sliced[0].date).getTime();
              const last = new Date(sliced[sliced.length - 1].date).getTime();
              const days = Math.max(1, Math.round((last - first) / 86_400_000));
              return `${days} ${days === 1 ? "day" : "days"}`;
            })() : "No data"}
          </span>
        </div>
        {sliced.length > 1 ? (
          <EquityChart points={sliced} startingCash={startingCash ?? 1000} />
        ) : (
          <div className="empty-state" style={{ padding: 40 }}>
            {loaded
              ? "Not enough history to draw an equity chart yet."
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

function EquityChart({ points, startingCash }: { points: BankrollPoint[]; startingCash: number }) {
  const w = 1000;
  const h = 300;
  const padL = 88;
  const padR = 24;
  const padT = 24;
  const padB = 44;
  // The incoming series is cumulative P&L (starts at 0). Equity is
  // starting cash plus cumulative P&L, so we render the actual dollar
  // equity the user holds at each point in time.
  const equity = points.map((p) => startingCash + p.bankroll);
  const rawMin = Math.min(...equity);
  const rawMax = Math.max(...equity);
  // Always include the starting-cash line in the visible range so the
  // user can see where they began. Add a small 8% pad above and below.
  const baseMin = Math.min(startingCash, rawMin);
  const baseMax = Math.max(startingCash, rawMax);
  const span = Math.max(1, baseMax - baseMin);
  const yMin = baseMin - span * 0.08;
  const yMax = baseMax + span * 0.08;
  const range = yMax - yMin || 1;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;
  // X-axis is time-based so equal days produce equal horizontal spacing even
  // when the underlying series has sparse settlement points.
  const xTimes = points.map((p) => {
    const t = new Date(p.date).getTime();
    return Number.isFinite(t) ? t : 0;
  });
  const tMin = Math.min(...xTimes);
  const tMax = Math.max(...xTimes);
  const tSpan = tMax - tMin || 1;
  const coords = points.map((p, i) => {
    const x = padL + plotW * ((xTimes[i] - tMin) / tSpan);
    const y = padT + plotH * (1 - (equity[i] - yMin) / range);
    return [x, y] as const;
  });
  const line = coords
    .map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1))
    .join(" ");

  // Y-axis: always include the starting cash as a labelled tick so the
  // "$1,000 at start" baseline is unmissable.
  const startY = padT + plotH * (1 - (startingCash - yMin) / range);
  const yTicks = [
    { v: yMax, y: padT, isStart: false },
    { v: startingCash, y: startY, isStart: true },
    { v: yMin, y: padT + plotH, isStart: false },
  ];

  const lastIdx = points.length - 1;
  const midIdx = Math.floor(lastIdx / 2);
  const xTicks = [
    { label: formatShortDate(points[0].date), x: coords[0][0], anchor: "start" as const, isStart: true },
    { label: formatShortDate(points[midIdx].date), x: coords[midIdx][0], anchor: "middle" as const, isStart: false },
    { label: formatShortDate(points[lastIdx].date), x: coords[lastIdx][0], anchor: "end" as const, isStart: false },
  ];

  const firstPoint = coords[0];
  const lastPoint = coords[coords.length - 1];
  const lastEquity = equity[equity.length - 1];

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", display: "block" }}>
      {yTicks.map((t, i) => (
        <line
          key={`g${i}`}
          x1={padL}
          x2={w - padR}
          y1={t.y}
          y2={t.y}
          stroke={t.isStart ? "rgba(255,255,255,0.22)" : "rgba(255,255,255,0.06)"}
          strokeDasharray={t.isStart ? "4 4" : "2 4"}
        />
      ))}
      {yTicks.map((t, i) => (
        <text
          key={`y${i}`}
          x={padL - 10}
          y={t.y + 4}
          fill={t.isStart ? "rgba(255,255,255,0.78)" : "rgba(255,255,255,0.5)"}
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
          y={h - 14}
          fill={t.isStart ? "rgba(255,255,255,0.78)" : "rgba(255,255,255,0.5)"}
          fontSize="11"
          textAnchor={t.anchor}
          fontFamily="ui-monospace, SFMono-Regular, monospace"
        >
          {t.label}
        </text>
      ))}
      {xTicks.map((t, i) =>
        t.isStart ? (
          <text
            key={`x-start-${i}`}
            x={t.x}
            y={h - 28}
            fill="rgba(255,255,255,0.45)"
            fontSize="10"
            textAnchor={t.anchor}
            fontFamily="ui-monospace, SFMono-Regular, monospace"
            letterSpacing="0.08em"
          >
            START
          </text>
        ) : null,
      )}
      <path
        d={line}
        fill="none"
        stroke="var(--gold)"
        strokeWidth="2"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx={firstPoint[0]} cy={firstPoint[1]} r="3.5" fill="var(--gold)" opacity="0.65" />
      <circle cx={lastPoint[0]} cy={lastPoint[1]} r="3.5" fill="var(--gold)" />
      <text
        x={lastPoint[0] - 8}
        y={lastPoint[1] - 10}
        fill="var(--gold)"
        fontSize="11"
        textAnchor="end"
        fontFamily="ui-monospace, SFMono-Regular, monospace"
      >
        {formatMoney(lastEquity)}
      </text>
    </svg>
  );
}
