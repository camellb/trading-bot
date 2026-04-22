"use client";

/**
 * QuestMilestone — single milestone within the Go-Live Quest.
 *
 * Shows a circular progress arc, label, current/target values,
 * and a celebration burst when the gate is unlocked.
 */

import { CelebrationBurst } from "./CelebrationBurst";

export type MilestoneData = {
  label: string;
  current: string;
  target: string;
  progress: number;   // 0–1
  pass: boolean;
  icon: React.ReactNode;
  sublabel: string;
};

const CIRCUMFERENCE = 2 * Math.PI * 36; // r=36

export function QuestMilestone({ data }: { data: MilestoneData }) {
  const dashOffset = CIRCUMFERENCE * (1 - data.progress);

  return (
    <div className="relative flex flex-col items-center gap-3 p-4">
      <CelebrationBurst active={data.pass} />

      {/* Circular progress */}
      <div className="relative w-[88px] h-[88px]">
        <svg viewBox="0 0 80 80" className="w-full h-full -rotate-90">
          {/* Track */}
          <circle
            cx="40" cy="40" r="36"
            fill="none"
            stroke="var(--color-surface-3)"
            strokeWidth="4"
          />
          {/* Progress arc */}
          <circle
            cx="40" cy="40" r="36"
            fill="none"
            stroke={data.pass ? "var(--color-accent)" : "#f59e0b"}
            strokeWidth="4"
            strokeLinecap="round"
            strokeDasharray={CIRCUMFERENCE}
            strokeDashoffset={dashOffset}
            className="transition-all duration-1000 ease-out"
          />
        </svg>

        {/* Center content */}
        <div className="absolute inset-0 flex items-center justify-center">
          {data.pass ? (
            <div className="w-8 h-8 bg-accent/20 flex items-center justify-center">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#00ffff" strokeWidth="2.5">
                <path d="M20 6L9 17l-5-5" />
              </svg>
            </div>
          ) : (
            <span className="text-[#666]">{data.icon}</span>
          )}
        </div>
      </div>

      {/* Label */}
      <div className="text-center">
        <div className="text-xs font-medium text-white">{data.label}</div>
        <div className={`text-lg font-semibold font-body ${data.pass ? "text-accent" : "text-[#ccc]"}`}>
          {data.current}
        </div>
        <div className="text-[10px] text-[#444]">
          {data.pass ? "UNLOCKED" : `target: ${data.target}`}
        </div>
        <div className="text-[10px] text-[#444] mt-0.5">{data.sublabel}</div>
      </div>
    </div>
  );
}
