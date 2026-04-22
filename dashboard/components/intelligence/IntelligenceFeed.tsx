"use client";

/**
 * IntelligenceFeed — live telemetry-style reasoning stream.
 *
 * Shows the bot's recent evaluations as a scrolling terminal feed.
 * Each line shows the analysis flow: market → probability → decision.
 * Makes the automation visible so users perceive the complexity.
 */

import type { EvaluationRow } from "@/hooks/use-dashboard-data";
import { FeedEntry } from "./FeedEntry";
import { LivePulse } from "../kinetic/LivePulse";

export function IntelligenceFeed({
  evaluations,
  maxEntries = 12,
}: {
  evaluations: EvaluationRow[] | undefined;
  maxEntries?: number;
}) {
  const entries = (evaluations ?? []).slice(0, maxEntries);
  const tradedCount = entries.filter(
    (e) => e.recommendation === "BUY_YES" || e.recommendation === "BUY_NO",
  ).length;

  return (
    <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#1a1a1a]">
        <div className="flex items-center gap-2">
          <LivePulse active size="xs" />
          <h3 className="text-xs uppercase tracking-widest text-[#a0a0a0]">Intelligence Feed</h3>
        </div>
        <div className="flex items-center gap-3 text-[10px]">
          <span className="text-[#444]">
            <span className="text-accent font-body">{tradedCount}</span> traded
          </span>
          <span className="text-[#444]">
            <span className="text-[#a0a0a0] font-body">{entries.length}</span> scanned
          </span>
        </div>
      </div>

      {/* Feed */}
      <div className="max-h-[420px] overflow-y-auto divide-y divide-[#1a1a1a]/50">
        {entries.length === 0 ? (
          <div className="px-4 py-8 text-center text-xs text-[#666]">
            <div className="font-body text-[#444] mb-1">
              awaiting signal...
            </div>
            No evaluations yet. Run a scan to populate the feed.
          </div>
        ) : (
          entries.map((entry, i) => (
            <FeedEntry key={entry.id} entry={entry} index={i} />
          ))
        )}
      </div>

      {/* Footer */}
      {entries.length > 0 && (
        <div className="px-4 py-2 border-t border-[#1a1a1a] bg-surface-3/20">
          <div className="flex items-center gap-1.5 text-[10px] text-[#444] font-body">
            <span className="w-1 h-1 rounded-full bg-accent/50 animate-breathe" />
            <span>stream.active</span>
            <span className="text-[#333]">|</span>
            <span>refresh: 30s</span>
          </div>
        </div>
      )}
    </div>
  );
}
