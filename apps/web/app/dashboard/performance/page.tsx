"use client";

import React, { useEffect, useMemo, useState } from "react";
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

// The bot's `_bankroll_series_impl` emits each point as
// `{ts: ISO string, pnl: number, bankroll: number}` where `bankroll` is
// cumulative realised P&L (starts at $0 because diagnostics calls
// bankroll_series with starting_cash=None). We accept either `ts` or
// `date` so the field rename can land in either repo without breaking.
type BankrollPoint = {
  ts?:       string;
  date?:     string;
  pnl?:      number;
  bankroll:  number;
};

function pointDate(p: BankrollPoint): string {
  return p.date ?? p.ts ?? "";
}

type Diagnostics = {
  system?: { bankroll_series?: BankrollPoint[] };
};

type SettledPosition = {
  id: number;
  question: string;
  side: "YES" | "NO";
  cost_usd: number;
  entry_price: number;
  claude_probability: number | null;
  confidence: number | null;
  settlement_outcome: string | null;
  realized_pnl_usd: number | null;
  settled_at: string | null;
  category: string | null;
  slug: string | null;
};

type PositionsPayload = { open: unknown[]; settled: SettledPosition[] };

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
    const t = new Date(pointDate(p)).getTime();
    return Number.isFinite(t) && t >= cutoffMs;
  });
  if (filtered.length >= 2) return filtered;
  // Fallback: keep the tail if timestamp filtering yields too little.
  return series.slice(-Math.max(2, days));
}

/**
 * Prepend the user's join date as a zero-baseline point so the curve starts
 * at $0 on the day they joined. Also appends today's cumulative value so the
 * X-axis always runs user-join -> today.
 */
function withBaseline(
  series: BankrollPoint[],
  joinedAt: Date | null,
): BankrollPoint[] {
  if (!series.length) {
    if (joinedAt) {
      const today = new Date().toISOString();
      return [
        { ts: joinedAt.toISOString(), bankroll: 0 },
        { ts: today, bankroll: 0 },
      ];
    }
    return [];
  }
  const out = series.slice();
  if (joinedAt) {
    const firstDate = new Date(pointDate(out[0])).getTime();
    if (!Number.isFinite(firstDate) || joinedAt.getTime() < firstDate - 60_000) {
      out.unshift({ ts: joinedAt.toISOString(), bankroll: 0 });
    }
  }
  const last = out[out.length - 1];
  const lastDate = new Date(pointDate(last)).getTime();
  const now = Date.now();
  // Extend to today when the last settled point is older than ~6 hours.
  if (!Number.isFinite(lastDate) || now - lastDate > 6 * 3600 * 1000) {
    out.push({ ts: new Date(now).toISOString(), bankroll: last.bankroll });
  }
  return out;
}

function filterTradesByRange(
  rows: SettledPosition[],
  range: Range,
): SettledPosition[] {
  if (range === "all") return rows;
  const cutoffMs = Date.now() - RANGE_DAYS[range] * 86_400_000;
  return rows.filter((r) => {
    if (!r.settled_at) return false;
    const t = new Date(r.settled_at).getTime();
    return Number.isFinite(t) && t >= cutoffMs;
  });
}

function formatShortDate(s: string): string {
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatDate(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function formatMoney(v: number): string {
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs >= 1000) {
    return `${sign}$${abs.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  }
  return `${sign}$${abs.toFixed(2)}`;
}

function formatSigned(v: number, digits = 2): string {
  const sign = v >= 0 ? "+" : "";
  return `${sign}$${v.toFixed(digits)}`;
}

function formatPct(v: number, digits = 1): string {
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}%`;
}

function isWin(row: SettledPosition): boolean | null {
  if (!row.settlement_outcome) return null;
  return row.side === row.settlement_outcome;
}

type CategoryStats = {
  category: string;
  n: number;
  wins: number;
  losses: number;
  totalPnl: number;
  totalCost: number;
  avgConfidence: number | null;
};

type CohortStats = {
  label: string;
  range: string;
  n: number;
  wins: number;
  totalPnl: number;
  totalCost: number;
};

function groupByCategory(rows: SettledPosition[]): CategoryStats[] {
  const buckets = new Map<string, CategoryStats>();
  for (const r of rows) {
    const w = isWin(r);
    if (w == null) continue; // skip void markets
    const key = r.category ?? "Uncategorised";
    const b = buckets.get(key) ?? {
      category: key,
      n: 0,
      wins: 0,
      losses: 0,
      totalPnl: 0,
      totalCost: 0,
      avgConfidence: null,
    };
    b.n += 1;
    if (w) b.wins += 1; else b.losses += 1;
    b.totalPnl += r.realized_pnl_usd ?? 0;
    b.totalCost += r.cost_usd ?? 0;
    if (r.confidence != null) {
      const prev = b.avgConfidence ?? 0;
      b.avgConfidence = (prev * (b.n - 1) + r.confidence) / b.n;
    }
    buckets.set(key, b);
  }
  return Array.from(buckets.values()).sort((a, b) => b.totalPnl - a.totalPnl);
}

function groupByConfidence(rows: SettledPosition[]): CohortStats[] {
  const cohorts: CohortStats[] = [
    { label: "Low", range: "< 0.40", n: 0, wins: 0, totalPnl: 0, totalCost: 0 },
    { label: "Medium", range: "0.40 - 0.70", n: 0, wins: 0, totalPnl: 0, totalCost: 0 },
    { label: "High", range: ">= 0.70", n: 0, wins: 0, totalPnl: 0, totalCost: 0 },
  ];
  for (const r of rows) {
    const w = isWin(r);
    if (w == null) continue;
    const c = r.confidence;
    if (c == null) continue;
    let idx: number;
    if (c < 0.4) idx = 0;
    else if (c < 0.7) idx = 1;
    else idx = 2;
    const b = cohorts[idx];
    b.n += 1;
    if (w) b.wins += 1;
    b.totalPnl += r.realized_pnl_usd ?? 0;
    b.totalCost += r.cost_usd ?? 0;
  }
  return cohorts;
}

type Overall = {
  trades: number;
  wins: number;
  losses: number;
  totalWagered: number;
  totalPnl: number;
  avgStake: number;
  bestWin: number;
  worstLoss: number;
  currentStreak: { kind: "win" | "loss" | "none"; count: number };
};

function computeOverall(rows: SettledPosition[]): Overall {
  let trades = 0;
  let wins = 0;
  let losses = 0;
  let totalWagered = 0;
  let totalPnl = 0;
  let bestWin = 0;
  let worstLoss = 0;
  for (const r of rows) {
    const w = isWin(r);
    if (w == null) continue;
    trades += 1;
    if (w) wins += 1; else losses += 1;
    totalWagered += r.cost_usd ?? 0;
    const pnl = r.realized_pnl_usd ?? 0;
    totalPnl += pnl;
    if (pnl > bestWin) bestWin = pnl;
    if (pnl < worstLoss) worstLoss = pnl;
  }
  // Current streak: rows are settled_at DESC, so walk from the top and count
  // consecutive W or L until the sign flips.
  let streakCount = 0;
  let streakKind: "win" | "loss" | "none" = "none";
  for (const r of rows) {
    const w = isWin(r);
    if (w == null) continue;
    if (streakKind === "none") {
      streakKind = w ? "win" : "loss";
      streakCount = 1;
    } else if ((streakKind === "win" && w) || (streakKind === "loss" && !w)) {
      streakCount += 1;
    } else {
      break;
    }
  }
  return {
    trades,
    wins,
    losses,
    totalWagered,
    totalPnl,
    avgStake: trades > 0 ? totalWagered / trades : 0,
    bestWin,
    worstLoss,
    currentStreak: { kind: streakKind, count: streakCount },
  };
}

export default function PerformancePage() {
  const [range, setRange] = useState<Range>("all");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [calibration, setCalibration] = useState<CalibrationPayload | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [positions, setPositions] = useState<PositionsPayload | null>(null);
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
    setPositions(null);
    const load = async () => {
      const [s, c, d, p] = await Promise.all([
        getJSON<Summary>("/api/summary"),
        getJSON<CalibrationPayload>("/api/calibration?source=polymarket"),
        getJSON<Diagnostics>("/api/diagnostics?scope=all"),
        getJSON<PositionsPayload>("/api/positions?limit=500"),
      ]);
      if (cancelled) return;
      setSummary(s);
      setCalibration(c);
      setDiag(d);
      setPositions(p);
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

  const allSettled = positions?.settled ?? [];
  const rangedSettled = useMemo(
    () => filterTradesByRange(allSettled, range),
    [allSettled, range],
  );

  const overall = useMemo(() => computeOverall(rangedSettled), [rangedSettled]);
  const categoryStats = useMemo(
    () => groupByCategory(rangedSettled),
    [rangedSettled],
  );
  const cohortStats = useMemo(
    () => groupByConfidence(rangedSettled),
    [rangedSettled],
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

  const rangeLabel = RANGES.find((r) => r.key === range)?.label ?? "ALL TIME";

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
          <div className="stat-cell-label">ROI (lifetime)</div>
          <div className="stat-cell-val">
            {roiPct != null ? `${roiPct >= 0 ? "+" : ""}${roiPct.toFixed(2)}%` : "-"}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">P&amp;L (lifetime)</div>
          <div className="stat-cell-val">
            {totalPnl != null ? `${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}` : "-"}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Win rate (lifetime)</div>
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
              const first = new Date(pointDate(sliced[0])).getTime();
              const last = new Date(pointDate(sliced[sliced.length - 1])).getTime();
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
          <h2 className="panel-title">Snapshot ({rangeLabel.toLowerCase()})</h2>
          <span className="panel-meta">
            {overall.trades} {overall.trades === 1 ? "trade" : "trades"}
          </span>
        </div>
        {overall.trades === 0 ? (
          <div className="empty-state" style={{ padding: 32 }}>
            {loaded
              ? "No settled trades in this window yet."
              : "Loading..."}
          </div>
        ) : (
          <div className="stat-row">
            <div className="stat-cell">
              <div className="stat-cell-label">P&amp;L</div>
              <div className={`stat-cell-val ${overall.totalPnl >= 0 ? "cell-up" : "cell-down"}`}>
                {formatSigned(overall.totalPnl)}
              </div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Wagered</div>
              <div className="stat-cell-val">{formatMoney(overall.totalWagered)}</div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Avg stake</div>
              <div className="stat-cell-val">{formatMoney(overall.avgStake)}</div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Wins / losses</div>
              <div className="stat-cell-val">
                <span className="cell-up">{overall.wins}</span>
                <span style={{ color: "rgba(255,255,255,0.35)", padding: "0 6px" }}>/</span>
                <span className="cell-down">{overall.losses}</span>
              </div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Best win</div>
              <div className="stat-cell-val cell-up">
                {overall.bestWin > 0 ? formatSigned(overall.bestWin) : "-"}
              </div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Worst loss</div>
              <div className="stat-cell-val cell-down">
                {overall.worstLoss < 0 ? formatSigned(overall.worstLoss) : "-"}
              </div>
            </div>
            <div className="stat-cell">
              <div className="stat-cell-label">Current streak</div>
              <div
                className={`stat-cell-val ${
                  overall.currentStreak.kind === "win"
                    ? "cell-up"
                    : overall.currentStreak.kind === "loss"
                      ? "cell-down"
                      : ""
                }`}
              >
                {overall.currentStreak.kind === "none"
                  ? "-"
                  : `${overall.currentStreak.count} ${overall.currentStreak.kind === "win" ? "W" : "L"}`}
              </div>
            </div>
          </div>
        )}
      </div>

      <CategoryPanel
        categoryStats={categoryStats}
        rows={rangedSettled}
        loaded={loaded}
      />

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Does higher confidence mean higher ROI?</h2>
          <span className="panel-meta">{overall.trades} settled</span>
        </div>
        <p className="panel-body" style={{ marginBottom: 20 }}>
          Delfi's sizer softens the stake on low-confidence calls. If confidence
          is meaningful, ROI should step up as the confidence band rises. If it
          doesn't, the softener isn't earning its keep.
        </p>
        {cohortStats.every((c) => c.n === 0) ? (
          <div className="empty-state" style={{ padding: 32 }}>
            {loaded
              ? "No settled trades with confidence scores yet."
              : "Loading..."}
          </div>
        ) : (
          <table className="table-simple">
            <thead>
              <tr>
                <th>Confidence</th>
                <th>Range</th>
                <th>Trades</th>
                <th>Win rate</th>
                <th>P&amp;L</th>
                <th>ROI</th>
              </tr>
            </thead>
            <tbody>
              {cohortStats.map((c) => {
                const winRate = c.n > 0 ? (c.wins / c.n) * 100 : 0;
                const roi = c.totalCost > 0 ? (c.totalPnl / c.totalCost) * 100 : 0;
                const rowStyle = c.n === 0
                  ? { color: "rgba(232, 228, 216, 0.35)" }
                  : undefined;
                return (
                  <tr key={c.label} style={rowStyle}>
                    <td>{c.label}</td>
                    <td className="mono">{c.range}</td>
                    <td className="mono">{c.n}</td>
                    <td className="mono">{c.n > 0 ? `${winRate.toFixed(0)}%` : "-"}</td>
                    <td className={`mono ${c.n > 0 ? (c.totalPnl >= 0 ? "cell-up" : "cell-down") : ""}`}>
                      {c.n > 0 ? formatSigned(c.totalPnl) : "-"}
                    </td>
                    <td className={`mono ${c.n > 0 && c.totalCost > 0 ? (roi >= 0 ? "cell-up" : "cell-down") : ""}`}>
                      {c.n > 0 && c.totalCost > 0 ? formatPct(roi, 1) : "-"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
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
              const binRangeLabel = `${Math.round(b.lo * 100)}-${Math.round(b.hi * 100)}%`;
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
                      <strong style={{ color: "var(--gold)" }}>{binRangeLabel}</strong>{" "}
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

      <ClosedTradesPanel
        rows={rangedSettled}
        loaded={loaded}
        rangeLabel={rangeLabel}
      />
    </div>
  );
}

/**
 * Performance by category, with per-row drill-down. Click a category
 * to expand a sub-table of every settled trade in that bucket so you
 * can see EXACTLY which markets won and lost - not just the aggregate.
 */
function CategoryPanel({
  categoryStats,
  rows,
  loaded,
}: {
  categoryStats: CategoryStats[];
  rows: SettledPosition[];
  loaded: boolean;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);

  // Pre-bucket trades by category once so the drill-down render is O(1).
  const byCategory = useMemo(() => {
    const m = new Map<string, SettledPosition[]>();
    for (const r of rows) {
      const w = isWin(r);
      if (w == null) continue;
      const key = r.category ?? "Uncategorised";
      const arr = m.get(key) ?? [];
      arr.push(r);
      m.set(key, arr);
    }
    // Sort each bucket: biggest wins first so the user can scan top
    // contributors and worst hits quickly.
    for (const [k, arr] of m) {
      arr.sort((a, b) => (b.realized_pnl_usd ?? 0) - (a.realized_pnl_usd ?? 0));
      m.set(k, arr);
    }
    return m;
  }, [rows]);

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Performance by category</h2>
        <span className="panel-meta">
          {categoryStats.length} {categoryStats.length === 1 ? "category" : "categories"}
          {categoryStats.length > 0 ? " · click to drill down" : ""}
        </span>
      </div>
      {categoryStats.length === 0 ? (
        <div className="empty-state" style={{ padding: 32 }}>
          {loaded ? "No settled trades in this window yet." : "Loading..."}
        </div>
      ) : (
        <table className="table-simple">
          <thead>
            <tr>
              <th style={{ width: 24 }}></th>
              <th>Category</th>
              <th>Trades</th>
              <th>Win rate</th>
              <th>P&amp;L</th>
              <th>ROI</th>
              <th>Avg stake</th>
              <th>Avg conf</th>
            </tr>
          </thead>
          <tbody>
            {categoryStats.map((c) => {
              const winRate = c.n > 0 ? (c.wins / c.n) * 100 : 0;
              const roi = c.totalCost > 0 ? (c.totalPnl / c.totalCost) * 100 : 0;
              const isOpen = expanded === c.category;
              const trades = byCategory.get(c.category) ?? [];
              return (
                <React.Fragment key={c.category}>
                  <tr
                    onClick={() => setExpanded(isOpen ? null : c.category)}
                    style={{ cursor: "pointer" }}
                    aria-expanded={isOpen}
                  >
                    <td
                      className="mono"
                      style={{
                        color: "rgba(255,255,255,0.55)",
                        userSelect: "none",
                        textAlign: "center",
                      }}
                    >
                      {isOpen ? "▾" : "▸"}
                    </td>
                    <td>{c.category}</td>
                    <td className="mono">{c.n}</td>
                    <td className="mono">{winRate.toFixed(0)}%</td>
                    <td className={`mono ${c.totalPnl >= 0 ? "cell-up" : "cell-down"}`}>
                      {formatSigned(c.totalPnl)}
                    </td>
                    <td className={`mono ${roi >= 0 ? "cell-up" : "cell-down"}`}>
                      {c.totalCost > 0 ? formatPct(roi, 1) : "-"}
                    </td>
                    <td className="mono">
                      {c.n > 0 ? formatMoney(c.totalCost / c.n) : "-"}
                    </td>
                    <td className="mono">
                      {c.avgConfidence != null ? c.avgConfidence.toFixed(2) : "-"}
                    </td>
                  </tr>
                  {isOpen ? (
                    <tr>
                      <td
                        colSpan={8}
                        style={{
                          padding: 0,
                          background: "rgba(255,255,255,0.02)",
                          borderTop: "1px solid rgba(255,255,255,0.06)",
                        }}
                      >
                        <CategoryDrilldown trades={trades} />
                      </td>
                    </tr>
                  ) : null}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

/**
 * Per-trade table shown inline under an expanded category row. Displays
 * every settled trade in that category sorted by P&L descending so the
 * top contributors and worst hits are visible at a glance.
 */
function CategoryDrilldown({ trades }: { trades: SettledPosition[] }) {
  if (trades.length === 0) {
    return (
      <div style={{ padding: 16, color: "rgba(255,255,255,0.55)", fontSize: 13 }}>
        No settled trades in this bucket.
      </div>
    );
  }
  // Quick header summary: total $ won, total $ lost, biggest hit.
  let won = 0;
  let lost = 0;
  for (const t of trades) {
    const pnl = t.realized_pnl_usd ?? 0;
    if (pnl >= 0) won += pnl; else lost += pnl;
  }
  const wins = trades.filter((t) => isWin(t) === true).length;
  const losses = trades.length - wins;

  return (
    <div style={{ padding: "12px 16px 16px" }}>
      <div
        style={{
          display: "flex",
          gap: 24,
          flexWrap: "wrap",
          fontSize: 12,
          color: "rgba(255,255,255,0.7)",
          marginBottom: 12,
          fontFamily: "ui-monospace, SFMono-Regular, monospace",
        }}
      >
        <span>{wins} W / {losses} L</span>
        <span className="cell-up">Won: {formatSigned(won)}</span>
        <span className="cell-down">Lost: {formatSigned(lost)}</span>
        <span>Net: <span className={won + lost >= 0 ? "cell-up" : "cell-down"}>
          {formatSigned(won + lost)}
        </span></span>
      </div>
      <table className="table-simple" style={{ marginTop: 0 }}>
        <thead>
          <tr>
            <th>Market</th>
            <th>Side</th>
            <th>Entry</th>
            <th>Stake</th>
            <th>Outcome</th>
            <th>P&amp;L</th>
            <th>ROI</th>
            <th>Conf</th>
            <th>Settled</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => {
            const pnl = t.realized_pnl_usd ?? 0;
            const outcomeLabel = t.settlement_outcome == null
              ? null
              : t.side === t.settlement_outcome ? "WIN" : "LOSS";
            const outcomeClass = outcomeLabel === "WIN"
              ? "cell-up"
              : outcomeLabel === "LOSS" ? "cell-down" : "";
            const roi = t.cost_usd > 0 ? (pnl / t.cost_usd) * 100 : null;
            return (
              <tr key={t.id}>
                <td>{t.question}</td>
                <td>
                  <span className={t.side === "YES" ? "pill pill-yes" : "pill pill-no"}>
                    {t.side}
                  </span>
                </td>
                <td className="mono">{t.entry_price.toFixed(2)}</td>
                <td className="mono">${t.cost_usd.toFixed(0)}</td>
                <td className={`mono ${outcomeClass}`}>{outcomeLabel ?? "-"}</td>
                <td className={`mono ${pnl >= 0 ? "cell-up" : "cell-down"}`}>
                  {formatSigned(pnl)}
                </td>
                <td className={`mono ${roi != null && roi >= 0 ? "cell-up" : roi != null ? "cell-down" : ""}`}>
                  {roi != null ? formatPct(roi, 0) : "-"}
                </td>
                <td className="mono">
                  {t.confidence != null ? t.confidence.toFixed(2) : "-"}
                </td>
                <td className="mono">{formatDate(t.settled_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ClosedTradesPanel({
  rows,
  loaded,
  rangeLabel,
}: {
  rows: SettledPosition[];
  loaded: boolean;
  rangeLabel: string;
}) {
  const [pageSize, setPageSize] = useState(25);
  const visible = rows.slice(0, pageSize);
  const canLoadMore = rows.length > pageSize;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">All closed trades</h2>
        <span className="panel-meta">
          {rows.length} {rows.length === 1 ? "trade" : "trades"} · {rangeLabel.toLowerCase()}
        </span>
      </div>
      {rows.length === 0 ? (
        <div className="empty-state" style={{ padding: 32 }}>
          {loaded
            ? "No settled trades in this window yet."
            : "Loading..."}
        </div>
      ) : (
        <>
          <table className="table-simple">
            <thead>
              <tr>
                <th>Market</th>
                <th>Category</th>
                <th>Side</th>
                <th>Outcome</th>
                <th>Stake</th>
                <th>P&amp;L</th>
                <th>ROI</th>
                <th>Conf</th>
                <th>Settled</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((s) => {
                const pnl = s.realized_pnl_usd ?? 0;
                const outcomeLabel = s.settlement_outcome == null
                  ? null
                  : s.side === s.settlement_outcome ? "WIN" : "LOSS";
                const outcomeClass = outcomeLabel === "WIN"
                  ? "cell-up"
                  : outcomeLabel === "LOSS" ? "cell-down" : "";
                const roi = s.cost_usd > 0 ? (pnl / s.cost_usd) * 100 : null;
                return (
                  <tr key={s.id}>
                    <td>{s.question}</td>
                    <td className="mono" style={{ color: "rgba(232, 228, 216, 0.65)" }}>
                      {s.category ?? "-"}
                    </td>
                    <td>
                      <span className={s.side === "YES" ? "pill pill-yes" : "pill pill-no"}>
                        {s.side}
                      </span>
                    </td>
                    <td className={`mono ${outcomeClass}`}>{outcomeLabel ?? "-"}</td>
                    <td className="mono">${s.cost_usd.toFixed(0)}</td>
                    <td className={`mono ${pnl >= 0 ? "cell-up" : "cell-down"}`}>
                      {formatSigned(pnl)}
                    </td>
                    <td className={`mono ${roi != null && roi >= 0 ? "cell-up" : roi != null ? "cell-down" : ""}`}>
                      {roi != null ? formatPct(roi, 0) : "-"}
                    </td>
                    <td className="mono">
                      {s.confidence != null ? s.confidence.toFixed(2) : "-"}
                    </td>
                    <td className="mono">{formatDate(s.settled_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {canLoadMore ? (
            <div style={{ padding: 16, textAlign: "center" }}>
              <button
                className="chip"
                onClick={() => setPageSize((n) => n + 25)}
              >
                Show more ({rows.length - pageSize} left)
              </button>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

/**
 * Pick "nice" round-number ticks for the Y-axis. Given a (yMin, yMax)
 * span, return roughly `count` evenly-spaced values that the eye reads
 * cleanly (multiples of 1, 2, 5, 10, ...). Tick set always brackets the
 * data range so the line never escapes the grid.
 */
function niceTicks(yMin: number, yMax: number, count: number): number[] {
  const span = yMax - yMin;
  if (!Number.isFinite(span) || span <= 0) return [yMin];
  const rawStep = span / Math.max(1, count - 1);
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  const step = (norm < 1.5 ? 1 : norm < 3.5 ? 2 : norm < 7.5 ? 5 : 10) * mag;
  const start = Math.floor(yMin / step) * step;
  const end   = Math.ceil(yMax  / step) * step;
  const out: number[] = [];
  for (let v = start; v <= end + 1e-9; v += step) {
    out.push(Number(v.toFixed(10)));
  }
  return out;
}

/**
 * Pick `count` evenly-spaced timestamps across [tMin, tMax] for X-axis
 * tick labels. Always includes both endpoints.
 */
function evenlySpacedTimes(tMin: number, tMax: number, count: number): number[] {
  if (count <= 1 || tMax <= tMin) return [tMin];
  const step = (tMax - tMin) / (count - 1);
  return Array.from({ length: count }, (_, i) => tMin + i * step);
}

function EquityChart({ points, startingCash }: { points: BankrollPoint[]; startingCash: number }) {
  const w = 1000;
  const h = 320;
  const padL = 88;
  const padR = 24;
  const padT = 28;
  const padB = 56;

  // The incoming series is cumulative P&L (starts at 0). Equity is
  // starting cash plus cumulative P&L, so we render the actual dollar
  // equity the user holds at each point in time.
  const equity = points.map((p) => startingCash + p.bankroll);

  // Always include the starting-cash line in the visible range, plus a
  // small pad above and below the data so the line never touches the
  // top or bottom edge.
  const dataMin = Math.min(...equity);
  const dataMax = Math.max(...equity);
  const baseMin = Math.min(startingCash, dataMin);
  const baseMax = Math.max(startingCash, dataMax);
  const span = Math.max(1, baseMax - baseMin);
  const yMinRaw = baseMin - span * 0.10;
  const yMaxRaw = baseMax + span * 0.10;
  const yTickValues = niceTicks(yMinRaw, yMaxRaw, 5);
  const yMin = yTickValues[0];
  const yMax = yTickValues[yTickValues.length - 1];
  const yRange = yMax - yMin || 1;

  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  // X-axis: time-based so equal days produce equal horizontal spacing.
  const xTimes = points.map((p) => {
    const t = new Date(pointDate(p)).getTime();
    return Number.isFinite(t) ? t : 0;
  });
  const tMin = Math.min(...xTimes);
  const tMax = Math.max(...xTimes);
  const tSpan = tMax - tMin || 1;

  const xFor = (t: number): number => padL + plotW * ((t - tMin) / tSpan);
  const yFor = (v: number): number => padT + plotH * (1 - (v - yMin) / yRange);

  const coords = points.map((_, i) => [xFor(xTimes[i]), yFor(equity[i])] as const);
  const line = coords
    .map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1))
    .join(" ");

  // Filled area under the line, anchored to the starting-cash baseline
  // so the visual mass below the start line reads as "below baseline"
  // (loss territory) and above as gain.
  const baseY = yFor(startingCash);
  const area = `${line} L ${coords[coords.length - 1][0].toFixed(1)} ${baseY.toFixed(1)} L ${coords[0][0].toFixed(1)} ${baseY.toFixed(1)} Z`;

  // X-axis: 6 evenly-spaced labels across the time range (count adapts
  // down for very narrow ranges).
  const xTickCount = Math.min(6, Math.max(2, points.length));
  const xTickTimes = evenlySpacedTimes(tMin, tMax, xTickCount);

  const lastEquity = equity[equity.length - 1];
  const isUp       = lastEquity >= startingCash;
  const lineColor  = isUp ? "var(--teal, #4bd0c4)" : "var(--red, #e56b6f)";

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      style={{ width: "100%", display: "block" }}
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <linearGradient id="equity-area-fill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%"   stopColor={lineColor} stopOpacity="0.18" />
          <stop offset="100%" stopColor={lineColor} stopOpacity="0.00" />
        </linearGradient>
      </defs>

      {/* Horizontal gridlines + Y-axis tick labels. */}
      {yTickValues.map((v, i) => {
        const y       = yFor(v);
        const isStart = Math.abs(v - startingCash) < 1e-6;
        return (
          <g key={`y-${i}`}>
            <line
              x1={padL}
              x2={w - padR}
              y1={y}
              y2={y}
              stroke={isStart ? "rgba(255,255,255,0.28)" : "rgba(255,255,255,0.07)"}
              strokeDasharray={isStart ? "5 4" : undefined}
            />
            <text
              x={padL - 10}
              y={y + 4}
              fill={isStart ? "rgba(255,255,255,0.85)" : "rgba(255,255,255,0.55)"}
              fontSize="11"
              textAnchor="end"
              fontFamily="ui-monospace, SFMono-Regular, monospace"
            >
              {formatMoney(v)}
            </text>
          </g>
        );
      })}

      {/* X-axis baseline. */}
      <line
        x1={padL}
        x2={w - padR}
        y1={padT + plotH}
        y2={padT + plotH}
        stroke="rgba(255,255,255,0.18)"
      />

      {/* X-axis tick labels (evenly spaced across the time range). */}
      {xTickTimes.map((t, i) => {
        const x      = xFor(t);
        const label  = formatShortDate(new Date(t).toISOString());
        const anchor: "start" | "middle" | "end" =
          i === 0 ? "start" : i === xTickTimes.length - 1 ? "end" : "middle";
        return (
          <g key={`x-${i}`}>
            <line
              x1={x}
              x2={x}
              y1={padT + plotH}
              y2={padT + plotH + 5}
              stroke="rgba(255,255,255,0.25)"
            />
            <text
              x={x}
              y={padT + plotH + 20}
              fill="rgba(255,255,255,0.65)"
              fontSize="11"
              textAnchor={anchor}
              fontFamily="ui-monospace, SFMono-Regular, monospace"
            >
              {label}
            </text>
          </g>
        );
      })}

      {/* Filled area under the curve, then the line itself on top. */}
      <path d={area} fill="url(#equity-area-fill)" stroke="none" />
      <path
        d={line}
        fill="none"
        stroke={lineColor}
        strokeWidth="2"
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {/* Endpoints + last-equity label. */}
      <circle cx={coords[0][0]}                cy={coords[0][1]}                r="3.5" fill={lineColor} opacity="0.55" />
      <circle cx={coords[coords.length - 1][0]} cy={coords[coords.length - 1][1]} r="4"   fill={lineColor} />
      <text
        x={coords[coords.length - 1][0] - 8}
        y={coords[coords.length - 1][1] - 10}
        fill={lineColor}
        fontSize="12"
        textAnchor="end"
        fontFamily="ui-monospace, SFMono-Regular, monospace"
        fontWeight="600"
      >
        {formatMoney(lastEquity)}
      </text>

      {/* Axis title for clarity. */}
      <text
        x={padL}
        y={16}
        fill="rgba(255,255,255,0.55)"
        fontSize="10"
        fontFamily="ui-monospace, SFMono-Regular, monospace"
        letterSpacing="0.08em"
      >
        EQUITY (USD)
      </text>
    </svg>
  );
}
