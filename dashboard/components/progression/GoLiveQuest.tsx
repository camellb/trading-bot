"use client";

/**
 * GoLiveQuest — gamified progression system for the Go-Live gates.
 *
 * Transforms the 3 gates (Brier, resolved count, P&L) into
 * a quest/leveling UI with circular progress milestones.
 */

import { useEffect, useState } from "react";
import type { CalibrationData, SummaryData } from "@/hooks/use-dashboard-data";
import { usd } from "@/lib/format";
import { QuestMilestone, type MilestoneData } from "./QuestMilestone";

function clamp01(v: number) {
  return Math.min(1, Math.max(0, v));
}

function buildMilestones(
  summary: SummaryData | null,
  calibration: CalibrationData | null,
): MilestoneData[] {
  const brier = calibration?.brier ?? summary?.brier ?? null;
  const resolved = calibration?.resolved ?? summary?.resolved_predictions ?? 0;
  const pnl = summary?.realized_pnl ?? null;
  const pnlScale = Math.max((summary?.starting_cash ?? 0) * 0.1, 25);

  return [
    {
      label: "Calibration",
      current: brier != null ? brier.toFixed(3) : "—",
      target: "< 0.220",
      progress: brier != null ? clamp01(1 - ((brier - 0.22) / 0.22)) : 0,
      pass: brier != null && brier < 0.22,
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
          <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" />
        </svg>
      ),
      sublabel: "Brier score",
    },
    {
      label: "Sample Size",
      current: `${resolved}`,
      target: "150",
      progress: clamp01(resolved / 150),
      pass: resolved >= 150,
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
          <path d="M12 20V10M18 20V4M6 20v-4" />
        </svg>
      ),
      sublabel: "Resolved predictions",
    },
    {
      label: "Profitability",
      current: pnl != null ? usd(pnl, { sign: true }) : "—",
      target: "> $0.00",
      progress: pnl != null ? (pnl >= 0 ? 1 : clamp01(1 - (Math.abs(pnl) / pnlScale))) : 0,
      pass: pnl != null && pnl > 0,
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
          <path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
        </svg>
      ),
      sublabel: "Simulation P&L",
    },
  ];
}

function useCountdown(testEnd: string | null) {
  const [remaining, setRemaining] = useState<string | null>(null);

  useEffect(() => {
    if (!testEnd) { setRemaining(null); return; }
    const end = new Date(testEnd).getTime();
    if (Number.isNaN(end)) { setRemaining(null); return; }

    function tick() {
      const diff = end - Date.now();
      if (diff <= 0) { setRemaining("test complete"); return; }
      const d = Math.floor(diff / 86_400_000);
      const h = Math.floor((diff % 86_400_000) / 3_600_000);
      const m = Math.floor((diff % 3_600_000) / 60_000);
      setRemaining(d > 0 ? `${d}d ${h}h ${m}m left` : `${h}h ${m}m left`);
    }

    tick();
    const id = setInterval(tick, 60_000);
    return () => clearInterval(id);
  }, [testEnd]);

  return remaining;
}

export function GoLiveQuest({
  summary,
  calibration,
  botMode,
}: {
  summary: SummaryData | null;
  calibration: CalibrationData | null;
  botMode: string | null;
}) {
  if (botMode === "live") return null;

  const milestones = buildMilestones(summary, calibration);
  const passCount = milestones.filter((m) => m.pass).length;
  const allPass = passCount === milestones.length;
  const countdown = useCountdown(summary?.test_end ?? null);
  const testStillRunning = countdown != null && countdown !== "test complete";
  const showReady = allPass && !testStillRunning;

  const level = passCount + 1;
  const levelLabel = allPass ? "Ready" : `Level ${level}`;

  return (
    <section className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
      {/* Header */}
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#1a1a1a]">
        <div className="flex items-center gap-3">
          <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0]">Go-Live Quest</h2>
          <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium font-body ${
            showReady
              ? "bg-accent/20 text-accent"
              : "bg-surface-3 text-[#a0a0a0]"
          }`}>
            {levelLabel}
          </span>
        </div>
        <span className={`text-[10px] font-body ${showReady ? "text-accent" : "text-[#666]"}`}>
          {showReady ? "ALL GATES PASSED" : `${passCount}/${milestones.length} unlocked`}
        </span>
      </header>

      {/* Milestones */}
      <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-[#1a1a1a]">
        {milestones.map((m) => (
          <QuestMilestone key={m.label} data={m} />
        ))}
      </div>

      {/* Footer info */}
      {countdown && (
        <div className="px-4 py-2.5 border-t border-[#1a1a1a] bg-surface-3/30">
          <div className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-breathe" />
            <span className="text-[10px] text-amber-400/80 uppercase tracking-widest">
              Simulation test — {countdown}
            </span>
          </div>
          {!testStillRunning && countdown === "test complete" && !allPass && (
            <div className="text-[10px] text-[#666] mt-1">
              Test period complete but not all gates are passing. Continue in simulation until all milestones unlock.
            </div>
          )}
        </div>
      )}

      {showReady && (
        <div className="px-4 py-2.5 border-t border-[#1a1a1a] bg-accent/5">
          <div className="flex items-center gap-2">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#00ffff" strokeWidth="2">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><path d="m9 11 3 3L22 4" />
            </svg>
            <span className="text-xs text-accent">
              All gates passing. Switch to live mode in Settings when ready.
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
