"use client";

/**
 * PostMortemCard — recalibration event display for losing trades.
 *
 * Frames each loss as a learning opportunity rather than a failure.
 * Shows what the bot predicted, what happened, and what was learned.
 */

import { usd, prob, timeAgo } from "@/lib/format";
import { buildPostMortem } from "@/lib/post-mortem";
import type { SettledPosition } from "@/hooks/use-dashboard-data";

export function PostMortemCard({
  position,
  index,
}: {
  position: SettledPosition;
  index: number;
}) {
  const pm = buildPostMortem(position);

  return (
    <div className="bg-surface-3/30 border border-[#1a1a1a] overflow-hidden animate-feed-in"
      style={{ animationDelay: `${index * 80}ms` }}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-[#1a1a1a] bg-surface-3/20">
        <div className="flex items-center gap-2">
          <div className="w-5 h-5 bg-amber-500/10 flex items-center justify-center">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2.5">
              <path d="M12 9v4M12 17h.01" />
            </svg>
          </div>
          <span className="text-[10px] uppercase tracking-widest text-amber-400/80 font-medium">
            Recalibration Event
          </span>
        </div>
        <span className="text-[10px] text-[#444]">{timeAgo(position.settled_at)}</span>
      </div>

      {/* Market info */}
      <div className="px-4 py-3">
        <div className="text-sm text-[#ccc] mb-3 leading-relaxed">{position.question}</div>

        {/* What happened */}
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mb-3">
          <MiniStat label="Position" value={position.side} valueClass={position.side === "YES" ? "text-accent" : "text-red-400"} />
          <MiniStat label="Bot P(YES)" value={prob(position.claude_probability)} />
          <MiniStat label="Confidence" value={position.confidence != null ? `${(position.confidence * 100).toFixed(0)}%` : "—"} />
          <MiniStat label="Outcome" value={position.settlement_outcome ?? "—"} valueClass="text-red-400" />
          <MiniStat label="Impact" value={usd(position.realized_pnl_usd, { sign: true })} valueClass="text-red-400" />
        </div>

        {pm.predictionError != null && (
          <div className="flex items-center gap-2 mb-3">
            <div className="flex-1 h-1.5 bg-surface-0/50 overflow-hidden">
              <div
                className="h-full bg-amber-500/70 transition-all duration-700"
                style={{ width: `${Math.min(pm.predictionError * 100, 100)}%` }}
              />
            </div>
            <span className="text-[10px] text-[#666] font-body whitespace-nowrap">
              {(pm.predictionError * 100).toFixed(0)}pp error
            </span>
          </div>
        )}

        {/* Narrative */}
        <div className="text-xs text-[#a0a0a0] leading-relaxed mb-2">{pm.narrative}</div>

        {/* Lesson — only shown when a concrete diagnostic fired. No
            catch-all reassurance: unexplained losses stay unexplained. */}
        {pm.lesson && (
          <div className="mt-3 pt-3 border-t border-[#1a1a1a]/50">
            <div className="flex items-start gap-2">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2" className="mt-0.5 shrink-0">
                <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
                <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
              </svg>
              <span className="text-[11px] text-[#666] leading-relaxed">{pm.lesson}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function MiniStat({
  label, value, valueClass = "text-[#ccc]",
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div>
      <div className="text-[10px] text-[#444] mb-0.5">{label}</div>
      <div className={`text-xs font-body ${valueClass}`}>{value}</div>
    </div>
  );
}
