"use client";

import { useEffect, useState } from "react";
import type { CalibrationData, SummaryData } from "@/hooks/use-dashboard-data";
import { usd } from "@/lib/format";

type Gate = {
  label: string;
  current: number | null;
  target: number;
  progress: number;
  pass: boolean;
  display: string;
  mode: "below" | "above";
};

function clamp01(value: number) {
  return Math.min(1, Math.max(0, value));
}

function progressForBelowGate(current: number | null, target: number) {
  if (current == null || target <= 0) return 0;
  if (current <= target) return 1;
  return clamp01(1 - ((current - target) / target));
}

function progressForAboveGate(
  current: number | null,
  target: number,
  zeroTargetScale = 100,
) {
  if (current == null) return 0;
  if (target === 0) {
    if (current >= 0) return 1;
    return clamp01(1 - (Math.abs(current) / zeroTargetScale));
  }
  return clamp01(current / target);
}

function buildGates(
  summary: SummaryData | null,
  calibration: CalibrationData | null,
): Gate[] {
  const brier = calibration?.brier ?? summary?.brier ?? null;
  const resolved = calibration?.resolved ?? summary?.resolved_predictions ?? 0;
  const pnl = summary?.realized_pnl ?? null;
  const pnlScale = Math.max((summary?.starting_cash ?? 0) * 0.1, 25);

  return [
    {
      label: "Brier score",
      current: brier,
      target: 0.22,
      progress: progressForBelowGate(brier, 0.22),
      pass: brier != null && brier < 0.22,
      display: brier != null ? brier.toFixed(3) : "---",
      mode: "below",
    },
    {
      label: "Resolved predictions",
      current: resolved,
      target: 150,
      progress: progressForAboveGate(resolved, 150),
      pass: resolved >= 150,
      display: `${resolved}/150`,
      mode: "above",
    },
    {
      label: "Simulation P&L",
      current: pnl,
      target: 0,
      progress: progressForAboveGate(pnl, 0, pnlScale),
      pass: pnl != null && pnl > 0,
      display: pnl != null ? usd(pnl, { sign: true }) : "---",
      mode: "above",
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

export function GoLiveGate({
  summary,
  calibration,
  botMode,
}: {
  summary: SummaryData | null;
  calibration: CalibrationData | null;
  botMode: string | null;
}) {
  if (botMode === "live") return null;

  const gates = buildGates(summary, calibration);
  const allPass = gates.every((g) => g.pass);
  const countdown = useCountdown(summary?.test_end ?? null);
  const testStillRunning = countdown != null && countdown !== "test complete";
  const showReady = allPass && !testStillRunning;

  return (
    <section className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#1a1a1a]">
        <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0]">go-live gate</h2>
        <span className={`text-[10px] font-body ${showReady ? "text-accent" : "text-[#666]"}`}>
          {showReady ? "READY" : `${gates.filter((g) => g.pass).length}/${gates.length} passing`}
        </span>
      </header>
      <div className="p-4 space-y-3">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {gates.map((g) => (
            <GateRow key={g.label} gate={g} />
          ))}
        </div>
        {countdown && (
          <div className="text-[10px] text-amber-400/80 border-t border-[#1a1a1a] pt-3 mt-3 uppercase tracking-widest">
            simulation test - {countdown}
          </div>
        )}
        {showReady && (
          <div className="text-[10px] text-accent/70 border-t border-[#1a1a1a] pt-3 mt-3">
            All gates passing and test period complete. Switch the bot to live mode in config and restart when you are ready.
          </div>
        )}
      </div>
    </section>
  );
}

function GateRow({ gate }: { gate: Gate }) {
  const widthPct = gate.pass ? 100 : Math.max(gate.progress * 100, 2);

  return (
    <div className="border border-[#1a1a1a] bg-surface-3/40 p-3">
      <div className="flex justify-between items-baseline mb-1.5">
        <span className="text-[10px] uppercase tracking-widest text-[#666]">
          {gate.label}
        </span>
        <span className={`font-body text-xs ${gate.pass ? "text-accent" : "text-[#ccc]"}`}>
          {gate.display}
          <span className="text-[#444] ml-1">
            ({gate.mode === "below" ? "<" : ">"} {gate.target})
          </span>
        </span>
      </div>
      <div className="h-1.5 bg-surface-0/50 overflow-hidden">
        <div
          className={`h-full transition-all ${gate.pass ? "bg-accent w-full" : "bg-amber-500"}`}
          style={gate.pass ? undefined : { width: `${widthPct}%` }}
        />
      </div>
    </div>
  );
}
