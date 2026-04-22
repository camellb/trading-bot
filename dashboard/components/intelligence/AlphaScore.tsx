"use client";

/**
 * ProfitScore — radial gauge hero metric.
 *
 * Weights ROI (60) > Brier (25) > win rate (15) — the bot's job is
 * to make money, not maximize accuracy. Displayed as a circular SVG
 * gauge; this is the north-star metric users check first.
 */

import { useMemo } from "react";
import { computeProfitScore, profitLabel, profitStrokeColor } from "@/lib/alpha-score";
import type { CalibrationData, SummaryData } from "@/hooks/use-dashboard-data";

const RADIUS = 40;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

export function ProfitScore({
  summary,
  calibration,
}: {
  summary: SummaryData | null;
  calibration: CalibrationData | null;
}) {
  const brier = calibration?.brier ?? summary?.brier ?? null;
  const winRate = summary?.win_rate ?? null;
  const roi = summary != null && summary.starting_cash > 0
    ? summary.realized_pnl / summary.starting_cash
    : null;

  const score = useMemo(
    () => computeProfitScore(brier, winRate, roi),
    [brier, winRate, roi],
  );

  const dashOffset = CIRCUMFERENCE * (1 - score / 100);
  const strokeColor = profitStrokeColor(score);
  const label = profitLabel(score);

  return (
    <div className="bg-surface-2 border border-[#1a1a1a] p-4 h-full">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[11px] uppercase tracking-widest text-[#666]">Profit Score</span>
        <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full`}
          style={{ color: strokeColor, backgroundColor: `${strokeColor}18` }}
        >
          {label}
        </span>
      </div>

      <div className="flex items-center gap-5">
        {/* Gauge */}
        <div className="relative w-[100px] h-[100px] shrink-0">
          <svg viewBox="0 0 90 90" className="w-full h-full -rotate-90">
            {/* Background track */}
            <circle
              cx="45" cy="45" r={RADIUS}
              fill="none"
              stroke="var(--color-surface-3)"
              strokeWidth="5"
            />
            {/* Progress arc */}
            <circle
              cx="45" cy="45" r={RADIUS}
              fill="none"
              stroke={strokeColor}
              strokeWidth="5"
              strokeLinecap="round"
              strokeDasharray={CIRCUMFERENCE}
              strokeDashoffset={dashOffset}
              className="animate-gauge-fill transition-all duration-1000"
              style={{ filter: `drop-shadow(0 0 4px ${strokeColor}40)` }}
            />
          </svg>
          {/* Center value */}
          <div className="absolute inset-0 flex items-center justify-center">
            <span
              className="text-2xl font-bold font-body"
              style={{ color: strokeColor }}
            >
              {score}
            </span>
          </div>
        </div>

        {/* Breakdown */}
        <div className="flex-1 space-y-2">
          <BreakdownRow
            label="Calibration"
            value={brier != null ? brier.toFixed(3) : "—"}
            desc="Brier score"
            good={brier != null && brier < 0.22}
          />
          <BreakdownRow
            label="Win Rate"
            value={winRate != null ? `${(winRate * 100).toFixed(0)}%` : "—"}
            desc="Settled positions"
            good={winRate != null && winRate > 0.5}
          />
          <BreakdownRow
            label="ROI"
            value={roi != null ? `${(roi * 100).toFixed(1)}%` : "—"}
            desc="Return on capital"
            good={roi != null && roi > 0}
          />
        </div>
      </div>
    </div>
  );
}

function BreakdownRow({
  label, value, desc, good,
}: {
  label: string;
  value: string;
  desc: string;
  good: boolean;
}) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <div className="text-[10px] text-[#666]">{label}</div>
        <div className="text-[10px] text-[#444]">{desc}</div>
      </div>
      <span className={`text-xs font-body ${good ? "text-accent" : "text-[#a0a0a0]"}`}>
        {value}
      </span>
    </div>
  );
}
