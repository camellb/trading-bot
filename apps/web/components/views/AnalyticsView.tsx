"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { pnlColorClass, usd } from "@/lib/format";

/* ── Types ─────────────────────────────────────────────────────────── */

type Period = "7" | "30" | "90" | "all";

type SummaryData = {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  expectancy: number | null;
  sharpe: number | null;
  sortino: number | null;
  max_drawdown: number | null;
  profit_factor: number | null;
  total_pnl: number | null;
  win_loss_ratio: number | null;
};

type AttributionRow = {
  category: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  total_pnl: number | null;
  avg_pnl: number | null;
};

type AttributionData = {
  by_archetype: AttributionRow[];
  by_side: AttributionRow[];
  by_ev: AttributionRow[];
  by_confidence: AttributionRow[];
};

type RollingDay = {
  date: string;
  daily_pnl: number | null;
  cumulative_pnl: number | null;
  trades: number;
  win_rate: number | null;
};

type BenchmarkData = {
  bot_pnl: number | null;
  always_yes_pnl: number | null;
  always_no_pnl: number | null;
  random_pnl: number | null;
  alpha: number | null;
};

type TradeRow = {
  question: string;
  side: string;
  entry_price: number | null;
  pnl: number | null;
  ev_bps: number | null;
  confidence: number | null;
};


/* ── Fetch helper ──────────────────────────────────────────────────── */

async function fetchJson<T>(url: string, signal: AbortSignal): Promise<T | null> {
  try {
    const res = await fetch(url, { signal, cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

/* ── API → component transformers ─────────────────────────────────── */

/**
 * The analytics API returns different field names per attribution tab
 * (archetype/side/bucket/zone/day/hour for the "category" column,
 * `count` instead of `trades`, `pnl` instead of `total_pnl`, and no
 * wins/losses/avg_pnl). This function normalises any raw row into
 * the uniform AttributionRow shape the table component expects.
 */
function toAttributionRows(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  raw: any[],
  categoryKey: string,
): AttributionRow[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((r) => {
    const count = Number(r.count ?? 0);
    const winRate = r.win_rate != null ? Number(r.win_rate) : null;
    const wins = winRate != null ? Math.round(count * winRate) : 0;
    const pnl = r.pnl != null ? Number(r.pnl) : null;
    return {
      category: String(r[categoryKey] ?? "unknown"),
      trades: count,
      wins,
      losses: count - wins,
      win_rate: winRate,
      total_pnl: pnl,
      avg_pnl: count > 0 && pnl != null ? pnl / count : null,
    };
  });
}

/**
 * Best/worst trades API returns `realized_pnl` - map to the `pnl`
 * field the TradeRow type (and table component) expects.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toTradeRow(r: any): TradeRow {
  return {
    question: r.question ?? "",
    side: r.side ?? "",
    entry_price: r.entry_price ?? null,
    pnl: r.realized_pnl ?? r.pnl ?? null,
    ev_bps: r.ev_bps ?? r.edge_bps ?? null,
    confidence: r.confidence ?? null,
  };
}

/* ── Component ─────────────────────────────────────────────────────── */

export function AnalyticsView() {
  const [period, setPeriod] = useState<Period>("30");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [summary, setSummary] = useState<SummaryData | null>(null);
  const [attribution, setAttribution] = useState<AttributionData | null>(null);
  const [rolling, setRolling] = useState<RollingDay[] | null>(null);
  const [benchmark, setBenchmark] = useState<BenchmarkData | null>(null);
  const [bestTrades, setBestTrades] = useState<TradeRow[] | null>(null);
  const [worstTrades, setWorstTrades] = useState<TradeRow[] | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  const fetchData = useCallback(async (p: Period) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);

    const daysParam = p === "all" ? "" : `?days=${p}`;

    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const [sum, rawAttr, roll, bench, rawBest, rawWorst] = await Promise.all([
        fetchJson<SummaryData>(`/api/analytics/summary${daysParam}`, controller.signal),
        fetchJson<Record<string, any>>(`/api/analytics/attribution${daysParam}`, controller.signal),
        fetchJson<RollingDay[]>(`/api/analytics/rolling${daysParam}`, controller.signal),
        fetchJson<BenchmarkData>("/api/analytics/benchmark", controller.signal),
        fetchJson<any[]>("/api/analytics/best-trades?limit=5", controller.signal),
        fetchJson<any[]>("/api/analytics/worst-trades?limit=5", controller.signal),
      ]);

      if (controller.signal.aborted) return;

      setSummary(sum);

      // Transform raw attribution rows - API field names differ per tab
      setAttribution(rawAttr ? {
        by_archetype: toAttributionRows(rawAttr.by_archetype ?? [], "archetype"),
        by_side: toAttributionRows(rawAttr.by_side ?? [], "side"),
        by_ev: toAttributionRows(rawAttr.by_ev ?? rawAttr.by_edge ?? [], "bucket"),
        by_confidence: toAttributionRows(rawAttr.by_confidence ?? [], "bucket"),
      } : null);

      setRolling(roll);
      setBenchmark(bench);

      // Trade rows: API uses realized_pnl, component uses pnl
      setBestTrades(rawBest ? rawBest.map(toTradeRow) : null);
      setWorstTrades(rawWorst ? rawWorst.map(toTradeRow) : null);
    } catch {
      if (!controller.signal.aborted) setError("Failed to load analytics data");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData(period);
    return () => abortRef.current?.abort();
  }, [period, fetchData]);

  const [attrTab, setAttrTab] = useState<"archetype" | "side" | "ev" | "confidence">("archetype");

  const attrData: AttributionRow[] | undefined = attribution
    ? {
        archetype: attribution.by_archetype,
        side: attribution.by_side,
        ev: attribution.by_ev,
        confidence: attribution.by_confidence,
      }[attrTab]
    : undefined;

  return (
    <div className="space-y-6 max-w-[1400px]">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-headline text-white">Performance Analytics</h1>
          <p className="text-xs text-[#666] mt-1">
            Trading metrics and P&L attribution
          </p>
        </div>
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>

      {error && (
        <div className="bg-danger-dim border border-red-500/20 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* KPI Row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          label="Sharpe Ratio"
          value={summary?.sharpe != null ? summary.sharpe.toFixed(2) : "--"}
          valueClass={sharpeColor(summary?.sharpe)}
          loading={loading}
        />
        <KpiCard
          label="Sortino Ratio"
          value={summary?.sortino != null ? summary.sortino.toFixed(2) : "--"}
          valueClass={summary?.sortino != null && summary.sortino > 0 ? "text-accent" : "text-white"}
          loading={loading}
        />
        <KpiCard
          label="Max Drawdown"
          value={summary?.max_drawdown != null ? `${(summary.max_drawdown * 100).toFixed(1)}%` : "--"}
          valueClass="text-red-400"
          loading={loading}
        />
        <KpiCard
          label="Profit Factor"
          value={summary?.profit_factor != null ? summary.profit_factor.toFixed(2) : "--"}
          valueClass={summary?.profit_factor != null && summary.profit_factor > 1 ? "text-accent" : "text-red-400"}
          loading={loading}
        />
      </div>

      {/* Performance Summary Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Win/Loss Stats */}
        <div className="bg-surface-2 border border-[#1a1a1a] p-5">
          <h3 className="text-[11px] uppercase tracking-widest text-[#666] mb-4 font-headline">Win / Loss Stats</h3>
          {loading ? (
            <LoadingSkeleton rows={6} />
          ) : summary ? (
            <div className="space-y-3">
              <StatRow label="Total Trades" value={String(summary.total_trades)} />
              <StatRow label="Wins / Losses" value={`${summary.wins} / ${summary.losses}`} />
              <StatRow
                label="Win Rate"
                value={summary.win_rate != null ? `${(summary.win_rate * 100).toFixed(1)}%` : "--"}
                valueClass={summary.win_rate != null && summary.win_rate >= 0.5 ? "text-accent" : "text-red-400"}
              />
              <StatRow
                label="Avg Win"
                value={usd(summary.avg_win, { sign: true })}
                valueClass="text-accent"
              />
              <StatRow
                label="Avg Loss"
                value={usd(summary.avg_loss, { sign: true })}
                valueClass="text-red-400"
              />
              <StatRow
                label="Win/Loss Ratio"
                value={summary.win_loss_ratio != null ? summary.win_loss_ratio.toFixed(2) : "--"}
              />
              <StatRow
                label="Expectancy"
                value={usd(summary.expectancy, { sign: true })}
                valueClass={pnlColorClass(summary.expectancy)}
              />
            </div>
          ) : (
            <EmptyState message="No data available" />
          )}
        </div>

        {/* Benchmark Comparison */}
        <div className="bg-surface-2 border border-[#1a1a1a] p-5">
          <h3 className="text-[11px] uppercase tracking-widest text-[#666] mb-4 font-headline">Benchmark Comparison</h3>
          {loading ? (
            <LoadingSkeleton rows={5} />
          ) : benchmark ? (
            <div className="space-y-3">
              <StatRow
                label="Bot P&L"
                value={usd(benchmark.bot_pnl, { sign: true })}
                valueClass={pnlColorClass(benchmark.bot_pnl)}
              />
              <StatRow
                label="Always YES"
                value={usd(benchmark.always_yes_pnl, { sign: true })}
                valueClass={pnlColorClass(benchmark.always_yes_pnl)}
              />
              <StatRow
                label="Always NO"
                value={usd(benchmark.always_no_pnl, { sign: true })}
                valueClass={pnlColorClass(benchmark.always_no_pnl)}
              />
              <StatRow
                label="Random"
                value={usd(benchmark.random_pnl, { sign: true })}
                valueClass={pnlColorClass(benchmark.random_pnl)}
              />
              <div className="border-t border-[#1a1a1a] pt-3">
                <StatRow
                  label="Alpha (vs best benchmark)"
                  value={usd(benchmark.alpha, { sign: true })}
                  valueClass={pnlColorClass(benchmark.alpha)}
                  bold
                />
              </div>
            </div>
          ) : (
            <EmptyState message="No benchmark data" />
          )}
        </div>
      </div>

      {/* P&L Attribution */}
      <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
        <div className="px-5 py-3 border-b border-[#1a1a1a] flex items-center justify-between">
          <h3 className="text-[11px] uppercase tracking-widest text-[#666] font-headline">P&L Attribution</h3>
          <div className="flex bg-surface-3 p-0.5">
            {(["archetype", "side", "ev", "confidence"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setAttrTab(t)}
                className={`px-3 py-1 text-[10px] font-medium transition-colors capitalize ${
                  attrTab === t
                    ? "bg-accent text-surface-0"
                    : "text-[#a0a0a0] hover:text-white"
                }`}
              >
                {t === "archetype" ? "By Archetype" : t === "side" ? "By Side" : t === "ev" ? "By EV" : "By Confidence"}
              </button>
            ))}
          </div>
        </div>
        <div className="overflow-x-auto">
          {loading ? (
            <div className="p-5"><LoadingSkeleton rows={4} /></div>
          ) : attrData && attrData.length > 0 ? (
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-[#1a1a1a] text-[10px] uppercase tracking-widest text-[#444]">
                  <th className="px-4 py-2.5 text-left font-medium">Category</th>
                  <th className="px-3 py-2.5 text-right font-medium">Trades</th>
                  <th className="px-3 py-2.5 text-right font-medium">Wins</th>
                  <th className="px-3 py-2.5 text-right font-medium">Losses</th>
                  <th className="px-3 py-2.5 text-right font-medium">Win Rate</th>
                  <th className="px-3 py-2.5 text-right font-medium">Total P&L</th>
                  <th className="px-3 py-2.5 text-right font-medium">Avg P&L</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1a1a1a]">
                {[...attrData]
                  .sort((a, b) => (b.total_pnl ?? 0) - (a.total_pnl ?? 0))
                  .map((row) => (
                    <tr key={row.category} className="hover:bg-surface-3/50 transition-colors">
                      <td className="px-4 py-2.5 text-white capitalize">{row.category}</td>
                      <td className="px-3 py-2.5 text-right font-body text-[#ccc]">{row.trades}</td>
                      <td className="px-3 py-2.5 text-right font-body text-accent">{row.wins}</td>
                      <td className="px-3 py-2.5 text-right font-body text-red-400">{row.losses}</td>
                      <td className="px-3 py-2.5 text-right font-body text-[#ccc]">
                        {row.win_rate != null ? `${(row.win_rate * 100).toFixed(1)}%` : "--"}
                      </td>
                      <td className={`px-3 py-2.5 text-right font-body ${pnlColorClass(row.total_pnl)}`}>
                        {usd(row.total_pnl, { sign: true })}
                      </td>
                      <td className={`px-3 py-2.5 text-right font-body ${pnlColorClass(row.avg_pnl)}`}>
                        {usd(row.avg_pnl, { sign: true })}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          ) : (
            <div className="p-5"><EmptyState message="No attribution data" /></div>
          )}
        </div>
      </div>

      {/* P&L Time Series */}
      <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
        <div className="px-5 py-3 border-b border-[#1a1a1a]">
          <h3 className="text-[11px] uppercase tracking-widest text-[#666] font-headline">Daily P&L Series</h3>
        </div>
        <div className="overflow-x-auto max-h-[24rem]">
          {loading ? (
            <div className="p-5"><LoadingSkeleton rows={5} /></div>
          ) : rolling && rolling.length > 0 ? (
            <table className="w-full text-xs">
              <thead className="sticky top-0 z-10 bg-surface-2">
                <tr className="border-b border-[#1a1a1a] text-[10px] uppercase tracking-widest text-[#444]">
                  <th className="px-4 py-2.5 text-left font-medium">Date</th>
                  <th className="px-3 py-2.5 text-right font-medium">Daily P&L</th>
                  <th className="px-3 py-2.5 text-right font-medium">Cumulative P&L</th>
                  <th className="px-3 py-2.5 text-right font-medium">Trades</th>
                  <th className="px-3 py-2.5 text-right font-medium">Win Rate</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1a1a1a]">
                {rolling.map((day) => (
                  <tr key={day.date} className="hover:bg-surface-3/50 transition-colors">
                    <td className="px-4 py-2.5 text-[#ccc] font-body">{day.date}</td>
                    <td className={`px-3 py-2.5 text-right font-body ${pnlColorClass(day.daily_pnl)}`}>
                      {usd(day.daily_pnl, { sign: true })}
                    </td>
                    <td className={`px-3 py-2.5 text-right font-body ${pnlColorClass(day.cumulative_pnl)}`}>
                      {usd(day.cumulative_pnl, { sign: true })}
                    </td>
                    <td className="px-3 py-2.5 text-right font-body text-[#ccc]">{day.trades}</td>
                    <td className="px-3 py-2.5 text-right font-body text-[#a0a0a0]">
                      {day.win_rate != null ? `${(day.win_rate * 100).toFixed(0)}%` : "--"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="p-5"><EmptyState message="No rolling data" /></div>
          )}
        </div>
      </div>

      {/* Best & Worst Trades */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TradesTable title="Best Trades" trades={bestTrades} loading={loading} accent />
        <TradesTable title="Worst Trades" trades={worstTrades} loading={loading} />
      </div>
    </div>
  );
}

/* ── Sub-components ─────────────────────────────────────────────────── */

function PeriodSelector({ value, onChange }: { value: Period; onChange: (p: Period) => void }) {
  const options: { id: Period; label: string }[] = [
    { id: "7", label: "7d" },
    { id: "30", label: "30d" },
    { id: "90", label: "90d" },
    { id: "all", label: "All" },
  ];
  return (
    <div className="flex bg-surface-2 border border-[#1a1a1a] p-0.5">
      {options.map((o) => (
        <button
          key={o.id}
          onClick={() => onChange(o.id)}
          className={`px-4 py-1.5 text-xs font-medium transition-colors ${
            value === o.id
              ? "bg-accent text-surface-0"
              : "text-[#a0a0a0] hover:text-white"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function KpiCard({
  label,
  value,
  valueClass,
  loading,
}: {
  label: string;
  value: string;
  valueClass?: string;
  loading?: boolean;
}) {
  return (
    <div className="bg-surface-2 border border-[#1a1a1a] p-4">
      <div className="text-[11px] uppercase tracking-widest text-[#666] mb-3">{label}</div>
      {loading ? (
        <div className="h-8 w-20 bg-surface-3 animate-pulse" />
      ) : (
        <div className={`text-2xl font-semibold font-body ${valueClass ?? "text-white"}`}>
          {value}
        </div>
      )}
    </div>
  );
}

function StatRow({
  label,
  value,
  valueClass,
  bold,
}: {
  label: string;
  value: string;
  valueClass?: string;
  bold?: boolean;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className={`text-xs ${bold ? "text-white font-medium" : "text-[#a0a0a0]"}`}>{label}</span>
      <span className={`text-xs font-body ${bold ? "font-semibold" : ""} ${valueClass ?? "text-white"}`}>
        {value}
      </span>
    </div>
  );
}

function TradesTable({
  title,
  trades,
  loading,
  accent,
}: {
  title: string;
  trades: TradeRow[] | null;
  loading: boolean;
  accent?: boolean;
}) {
  return (
    <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
      <div className="px-4 py-3 border-b border-[#1a1a1a]">
        <h3 className="text-[11px] uppercase tracking-widest text-[#666] font-headline">{title}</h3>
      </div>
      {loading ? (
        <div className="p-4"><LoadingSkeleton rows={5} /></div>
      ) : trades && trades.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-[#1a1a1a] text-[10px] uppercase tracking-widest text-[#444]">
                <th className="px-4 py-2 text-left font-medium">Market</th>
                <th className="px-3 py-2 text-center font-medium">Side</th>
                <th className="px-3 py-2 text-right font-medium">Entry</th>
                <th className="px-3 py-2 text-right font-medium">P&L</th>
                <th className="px-3 py-2 text-right font-medium hidden sm:table-cell">Δ</th>
                <th className="px-3 py-2 text-right font-medium hidden sm:table-cell">Conf.</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1a1a1a]">
              {trades.map((t, i) => (
                <tr key={i} className="hover:bg-surface-3/50 transition-colors">
                  <td className="px-4 py-2.5 max-w-[200px]">
                    <span className="text-[#ccc] line-clamp-1">{t.question}</span>
                  </td>
                  <td className="px-3 py-2.5 text-center">
                    <span className={`inline-block px-2 py-0.5 text-[10px] font-semibold ${
                      t.side === "YES" ? "bg-accent-dim text-accent" : "bg-red-500/10 text-red-400"
                    }`}>{t.side}</span>
                  </td>
                  <td className="px-3 py-2.5 text-right font-body text-[#ccc]">
                    {t.entry_price != null ? `$${t.entry_price.toFixed(2)}` : "--"}
                  </td>
                  <td className={`px-3 py-2.5 text-right font-body ${accent ? "text-accent" : "text-red-400"}`}>
                    {usd(t.pnl, { sign: true })}
                  </td>
                  <td className="px-3 py-2.5 text-right font-body text-[#a0a0a0] hidden sm:table-cell">
                    {t.ev_bps != null ? `${t.ev_bps.toFixed(0)}` : "--"}
                  </td>
                  <td className="px-3 py-2.5 text-right font-body text-[#a0a0a0] hidden sm:table-cell">
                    {t.confidence != null ? t.confidence.toFixed(2) : "--"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="p-4"><EmptyState message="No trades" /></div>
      )}
    </div>
  );
}

function LoadingSkeleton({ rows }: { rows: number }) {
  return (
    <div className="space-y-3">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-4 bg-surface-3 animate-pulse" style={{ width: `${70 + Math.random() * 30}%` }} />
      ))}
    </div>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="text-center text-sm text-[#444] py-6">
      {message}
    </div>
  );
}

function sharpeColor(v: number | null | undefined): string {
  if (v == null) return "text-white";
  if (v > 1) return "text-accent";
  if (v >= 0) return "text-yellow-400";
  return "text-red-400";
}
