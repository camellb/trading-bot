"use client";

/**
 * PostMortemList - collapsible container for loss post-mortems.
 *
 * Shows recent losses framed as "Recalibration Events" with
 * expandable analysis. Keeps users engaged through loss periods
 * by framing them as learning opportunities.
 */

import { useState } from "react";
import type { SettledPosition } from "@/hooks/use-dashboard-data";
import { getLosses } from "@/lib/post-mortem";
import { PostMortemCard } from "./PostMortemCard";

export function PostMortemList({
  settled,
  maxVisible = 5,
}: {
  settled: SettledPosition[];
  maxVisible?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const losses = getLosses(settled);

  if (losses.length === 0) return null;

  const visible = expanded ? losses : losses.slice(0, maxVisible);
  const hasMore = losses.length > maxVisible;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs uppercase tracking-widest text-[#a0a0a0]">Recalibration Events</span>
          <span className="text-[10px] px-1.5 py-0.5 bg-amber-500/10 text-amber-400 font-body">
            {losses.length}
          </span>
        </div>
        {hasMore && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[10px] text-[#666] hover:text-[#ccc] transition-colors"
          >
            {expanded ? "Show less" : `Show all ${losses.length}`}
          </button>
        )}
      </div>

      <div className="space-y-3">
        {visible.map((loss, i) => (
          <PostMortemCard key={loss.id} position={loss} index={i} />
        ))}
      </div>
    </div>
  );
}
