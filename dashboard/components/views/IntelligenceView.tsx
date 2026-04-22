"use client";

/**
 * IntelligenceView — dedicated view for the bot's reasoning telemetry.
 *
 * Shows the full intelligence feed, Profit Score breakdown,
 * and accuracy streaks in one place.
 * This is the "magic" view — making the automation visible.
 */

import type { DashboardSnapshot } from "@/hooks/use-dashboard-data";
import type { ToastFn } from "@/lib/format";
import { ProfitScore } from "../intelligence/AlphaScore";
import { IntelligenceFeed } from "../intelligence/IntelligenceFeed";
import { PendingSuggestionsPanel } from "../intelligence/PendingSuggestionsPanel";
import { AccuracyStreak } from "../progression/AccuracyStreak";

import { LivePulse } from "../kinetic/LivePulse";

export function IntelligenceView({
  data,
  toast,
  refresh,
}: {
  data: DashboardSnapshot;
  toast: ToastFn;
  refresh: () => void;
}) {
  return (
    <div className="space-y-6 max-w-[1400px]">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white">Intelligence</h1>
          <div className="flex items-center gap-2 mt-1">
            <LivePulse active size="xs" />
            <span className="text-xs text-[#666]">
              Bot reasoning telemetry and performance analytics.
            </span>
          </div>
        </div>
      </div>

      {/* Hero Row: Profit Score + Streak */}
      <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-4">
        <ProfitScore summary={data.summary} calibration={data.calibration} />
        <AccuracyStreak settled={data.positions?.settled ?? []} />
      </div>

      {/* Intelligence Feed (full size) */}
      <IntelligenceFeed
        evaluations={data.evaluations?.evaluations}
        maxEntries={25}
      />

      {/* Learning-cadence proposals awaiting the user's decision. */}
      <PendingSuggestionsPanel />

    </div>
  );
}
