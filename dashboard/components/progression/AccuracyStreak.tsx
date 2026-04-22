"use client";

/**
 * AccuracyStreak — win streak counter card.
 *
 * Shows the current consecutive-win streak with escalating
 * visual intensity (fire, bolt) and the all-time best streak.
 */

import type { SettledPosition } from "@/hooks/use-dashboard-data";
import { computeCurrentStreak, computeBestStreak, streakIcon } from "@/lib/streaks";
import { useMemo } from "react";

export function AccuracyStreak({
  settled,
}: {
  settled: SettledPosition[];
}) {
  const current = useMemo(() => computeCurrentStreak(settled), [settled]);
  const best = useMemo(() => computeBestStreak(settled), [settled]);
  const icon = streakIcon(current);
  const isHot = current >= 3;

  return (
    <div className="bg-surface-2 border border-[#1a1a1a] p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[11px] uppercase tracking-widest text-[#666]">Win Streak</span>
        {isHot && (
          <span className="text-lg animate-streak-fire">{icon}</span>
        )}
      </div>
      <div className="flex items-baseline gap-2">
        <span className={`text-3xl font-bold font-body ${
          isHot ? "text-accent" : "text-[#ccc]"
        }`}>
          {current}
        </span>
        <span className="text-xs text-[#444]">consecutive wins</span>
      </div>
      <div className="flex items-center gap-3 mt-2 text-[10px] text-[#666]">
        <span>Best: <span className="text-[#a0a0a0] font-body">{best}</span></span>
        <span className="text-[#444]">·</span>
        <span>Total settled: <span className="text-[#a0a0a0] font-body">{settled.length}</span></span>
      </div>
      {isHot && (
        <div className="mt-3 pt-3 border-t border-[#1a1a1a]">
          <div className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-accent animate-breathe" />
            <span className="text-[10px] text-accent">
              {current >= 10 ? "Legendary streak!" : current >= 5 ? "On fire! Keep it going" : "Building momentum"}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
