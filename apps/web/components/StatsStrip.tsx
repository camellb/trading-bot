"use client";

import type { SummaryData } from "@/hooks/use-dashboard-data";
import { pnlColorClass, usd } from "@/lib/format";

export function StatsStrip({ summary }: { summary: SummaryData | null }) {
  const totalPreds = summary?.total_predictions ?? 0;
  const openPos = summary?.open_positions ?? 0;
  const settledPos = summary?.settled_total ?? 0;
  const traded = openPos + settledPos;
  const skipped = totalPreds > traded ? totalPreds - traded : 0;

  return (
    <section className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-px bg-[#0a0a0a] border-b border-[#1a1a1a] text-white">
      <Cell label="balance" value={usd(summary?.bankroll)} />
      <Cell label="locked capital" value={usd(summary?.open_cost)} />
      <Cell
        label="realized P&L"
        value={usd(summary?.realized_pnl, { sign: true, clampZero: true })}
        valueClass={pnlColorClass(summary?.realized_pnl)}
      />
      <Cell label="open bets" value={openPos.toString()} />
      <Cell label="markets analyzed" value={totalPreds.toString()} />
      <Cell label="settled" value={settledPos.toString()} />
      <Cell label="skipped" value={skipped.toString()} />
    </section>
  );
}

function Cell({
  label, value, valueClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="bg-[#050505] px-4 py-3">
      <div className="text-[10px] uppercase tracking-widest text-[#666]">{label}</div>
      <div className={`font-body text-lg ${valueClass ?? "text-white"}`}>{value}</div>
    </div>
  );
}
