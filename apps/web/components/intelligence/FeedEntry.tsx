"use client";

/**
 * FeedEntry - single intelligence feed line.
 *
 * Renders like a telemetry terminal line: timestamp, category tag,
 * market question, probability comparison, and recommendation.
 * Animates in with a slide-up on mount.
 */

import type { EvaluationRow } from "@/hooks/use-dashboard-data";
import { prob, recommendationColorClass, formatRecommendation, timeAgo } from "@/lib/format";
import type { Recommendation } from "@/lib/format";

export function FeedEntry({
  entry,
  index,
}: {
  entry: EvaluationRow;
  index: number;
}) {
  const isTraded = entry.recommendation === "BUY_YES" || entry.recommendation === "BUY_NO";

  return (
    <div
      className="flex items-start gap-3 px-3 py-2.5 hover:bg-surface-3/30 transition-colors animate-feed-in"
      style={{ animationDelay: `${index * 60}ms` }}
    >
      {/* Timestamp */}
      <span className="text-[10px] font-body text-[#444] whitespace-nowrap mt-0.5 w-8 shrink-0">
        {timeAgo(entry.evaluated_at)}
      </span>

      {/* Activity indicator */}
      <span className={`w-1 h-1 rounded-full mt-1.5 shrink-0 ${
        isTraded ? "bg-accent" : "bg-[#444]"
      }`} />

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          {/* Category tag */}
          {entry.category && (
            <span className="text-[9px] px-1.5 py-0.5 bg-surface-3 text-[#666] uppercase tracking-wider shrink-0">
              {entry.category}
            </span>
          )}
          <span className="text-xs text-[#ccc] line-clamp-1">{entry.question}</span>
        </div>

        {/* Telemetry line */}
        <div className="flex items-center gap-2 text-[10px] font-body">
          <span className="text-[#444]">est:</span>
          <span className="text-[#a0a0a0]">{prob(entry.claude_probability)}</span>
          <span className="text-[#444]">|</span>
          <span className="text-[#444]">mkt:</span>
          <span className="text-[#666]">{prob(entry.market_price_yes)}</span>
          {entry.ev_bps != null && (
            <>
              <span className="text-[#444]">|</span>
              <span className="text-[#444]">ev:</span>
              <span className={isTraded ? "text-accent" : "text-[#666]"}>
                {entry.ev_bps.toFixed(0)}bps
              </span>
            </>
          )}
          <span className="text-[#444]">→</span>
          <span className={recommendationColorClass(entry.recommendation)}>
            {formatRecommendation(entry.recommendation)}
          </span>
        </div>
      </div>
    </div>
  );
}
