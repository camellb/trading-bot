"use client";

import type { HealthData, SummaryData } from "@/hooks/use-dashboard-data";
import { formatUptime } from "@/lib/format";

export function HeaderBar({
  health, summary, lastUpdated,
}: {
  health:       HealthData | null;
  summary:      SummaryData | null;
  lastUpdated:  Date | null;
}) {
  const mode = summary?.mode ?? health?.mode ?? "—";
  const modeColor = mode === "live" ? "text-red-400" : "text-amber-400";

  return (
    <header className="border-b border-neutral-800 bg-neutral-950/90 backdrop-blur px-4 py-4">
      <div className="flex flex-col sm:flex-row items-center gap-3 sm:gap-6">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold tracking-tight text-neutral-100">
            Polymarket Bot
          </h1>
          <span className={`text-[10px] uppercase tracking-widest font-semibold px-2 py-0.5 rounded ${
            mode === "live"
              ? "bg-red-500/15 text-red-400 border border-red-500/30"
              : "bg-amber-500/15 text-amber-400 border border-amber-500/30"
          }`}>
            {mode}
          </span>
        </div>

        <div className="flex items-center gap-5 sm:gap-6 sm:ml-auto">
          <Stat label="uptime" value={formatUptime(health?.started_at ?? null)} />
          <div className="text-[10px] text-neutral-600">
            updated {lastUpdated ? lastUpdated.toLocaleTimeString() : "—"}
          </div>
        </div>
      </div>
    </header>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="text-center sm:text-left leading-tight">
      <div className="text-[10px] uppercase tracking-widest text-neutral-500">{label}</div>
      <div className="font-mono text-sm text-neutral-100">{value}</div>
    </div>
  );
}
