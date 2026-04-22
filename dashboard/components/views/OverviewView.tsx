"use client";

import type { DashboardSnapshot } from "@/hooks/use-dashboard-data";
import type { ToastFn, Recommendation } from "@/lib/format";
import { formatUptime, pnlColorClass, usd, prob, timeAgo, recommendationColorClass, formatRecommendation, polymarketUrl } from "@/lib/format";
import { GoLiveQuest } from "../progression/GoLiveQuest";
import { ProfitScore } from "../intelligence/AlphaScore";
import { AccuracyStreak } from "../progression/AccuracyStreak";
import { IntelligenceFeed } from "../intelligence/IntelligenceFeed";
import { CalibrationPanel } from "../CalibrationPanel";
import { ShimmerCard } from "../kinetic/ShimmerCard";
import { LivePulse } from "../kinetic/LivePulse";

export function OverviewView({
  data,
  refresh,
  toast,
  lastUpdated,
}: {
  data: DashboardSnapshot;
  refresh: () => void;
  toast: ToastFn;
  lastUpdated: Date | null;
}) {
  const summary = data.summary;
  const health = data.health;
  const mode = summary?.mode ?? health?.mode ?? "simulation";
  const isLoading = !summary;

  return (
    <div className="space-y-6 max-w-[1400px]">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white font-headline">Portfolio Overview</h1>
          <div className="flex items-center gap-2 mt-1">
            <LivePulse active size="xs" />
            <span className="text-xs text-[#666]">
              Live sync active. Last update: {lastUpdated ? timeAgo(lastUpdated.toISOString()) : "—"}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <ActionButton label="Scan" onClick={async () => {
            try {
              const res = await fetch("/api/scan-now", { method: "POST" });
              if (res.ok) { toast("Scan triggered"); setTimeout(refresh, 1500); }
              else toast("Scan failed", "error");
            } catch { toast("Request failed", "error"); }
          }} />
          <ActionButton label="Resolve" onClick={async () => {
            try {
              const res = await fetch("/api/resolve-now", { method: "POST" });
              if (res.ok) { toast("Resolution triggered"); setTimeout(refresh, 1500); }
              else toast("Resolve failed", "error");
            } catch { toast("Request failed", "error"); }
          }} />
          <ActionButton label="Refresh" onClick={refresh} accent />
        </div>
      </div>

      {/* Hero Row: Profit Score + KPI Cards */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_2fr] gap-4 items-stretch">
        <ShimmerCard loading={isLoading} className="h-full">
          <ProfitScore summary={summary} calibration={data.calibration} />
        </ShimmerCard>

        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <ShimmerCard loading={isLoading} className="h-full">
            <KpiCard
              label="Portfolio Value"
              value={usd(summary?.equity)}
              sub={summary ? (
                <span className="text-[#666]">
                  {usd(summary.bankroll)} free · {usd(summary.open_cost)} in bets
                </span>
              ) : null}
              icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>}
            />
          </ShimmerCard>
          <ShimmerCard loading={isLoading} className="h-full">
            <KpiCard
              label="Active Positions"
              value={String(summary?.open_positions ?? 0)}
              sub={summary?.settled_total ? (
                <span className="text-[#666]">{summary.settled_total} settled</span>
              ) : null}
              icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><path d="M12 20V10M18 20V4M6 20v-4"/></svg>}
            />
          </ShimmerCard>
          <ShimmerCard loading={isLoading} className="h-full">
            <KpiCard
              label="Realized P&L"
              value={usd(summary?.realized_pnl, { sign: true, clampZero: true })}
              valueClass={pnlColorClass(summary?.realized_pnl)}
              sub={summary?.win_rate != null ? (
                <span className="text-[#666]">
                  {(summary.win_rate * 100).toFixed(0)}% win rate · {summary.realized_pnl != null ? (
                    <span className={summary.realized_pnl >= 0 ? "text-accent" : "text-red-400"}>
                      {summary.realized_pnl >= 0 ? "+" : ""}{((summary.realized_pnl / (summary.starting_cash || 1)) * 100).toFixed(1)}%
                    </span>
                  ) : null}
                </span>
              ) : null}
              icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><path d="m22 12-4-4v3H3v2h15v3l4-4z"/></svg>}
            />
          </ShimmerCard>
          <ShimmerCard loading={isLoading} className="h-full">
            <KpiCard
              label="Bot Uptime"
              value={formatUptime(health?.started_at ?? null)}
              sub={<span className="text-accent">Engine stable</span>}
              icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>}
            />
          </ShimmerCard>
        </div>
      </div>

      {/* Go-Live Quest */}
      <GoLiveQuest
        summary={summary}
        calibration={data.calibration}
        botMode={mode}
      />

      {/* Streak + Calibration Row */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_2fr] gap-4">
        <AccuracyStreak settled={data.positions?.settled ?? []} />
        <CalibrationPanel
          data={data.calibration}
          brierTrend={data.brierTrend?.points ?? null}
          settledCount={summary?.settled_total ?? null}
        />
      </div>

      {/* Intelligence Feed */}
      <IntelligenceFeed evaluations={data.evaluations?.evaluations} />
    </div>
  );
}

/* ── Sub-components ─────────────────────────────────────────────────── */

function KpiCard({
  label, value, valueClass, sub, icon,
}: {
  label: string;
  value: string;
  valueClass?: string;
  sub?: React.ReactNode;
  icon: React.ReactNode;
}) {
  return (
    <div className="bg-surface-2 border border-[#1a1a1a] p-4 h-full flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[11px] uppercase tracking-widest text-[#666] font-body">{label}</span>
        <span className="text-[#444]">{icon}</span>
      </div>
      <div className="flex-1 flex flex-col justify-center">
        <div className={`text-2xl font-semibold font-body ${valueClass ?? "text-white"}`}>
          {value}
        </div>
        {sub && (
          <div className="text-xs mt-1">{sub}</div>
        )}
      </div>
    </div>
  );
}

function ActionButton({ label, onClick, accent }: { label: string; onClick: () => void; accent?: boolean }) {
  return (
    <button
      onClick={onClick}
      className={`
        px-4 py-2 text-xs font-medium transition-colors
        ${accent
          ? "bg-accent text-surface-0 hover:bg-accent-bright"
          : "bg-surface-2 border border-[#1a1a1a] text-[#ccc] hover:bg-surface-3 hover:text-white"
        }
      `}
    >
      {label}
    </button>
  );
}
