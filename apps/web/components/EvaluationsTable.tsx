"use client";

import type { EvaluationRow } from "@/hooks/use-dashboard-data";
import {
  polymarketUrl,
  prob,
  recommendationColorClass,
  timeAgo,
  formatTimestamp,
  formatRecommendation,
} from "@/lib/format";
import { Detail, Th } from "@/components/ui/Table";
import { Fragment, useState } from "react";

function buildEvaluationSummary(row: EvaluationRow): string {
  const bot = prob(row.claude_probability);
  const crowd = prob(row.market_price_yes);

  if (row.recommendation === "BUY_YES") {
    return `The bot would buy YES because its estimate for YES (${bot}) is above the crowd price (${crowd}).`;
  }

  if (row.recommendation === "BUY_NO") {
    return `The bot would buy NO because its estimate for YES (${bot}) is below the crowd price (${crowd}).`;
  }

  // Build an informative skip reason from the sizer's actual decision.
  if (row.skip_reason) {
    return `Skipped: ${row.skip_reason}.`;
  }

  // Fallback: infer the likely reason from the available numeric data.
  const ev = row.ev_bps;
  const conf = row.confidence;
  if (ev != null && ev < 500) {
    return `Skipped: expected value (${ev.toFixed(0)} bps) was too small after costs. Bot estimate: ${bot}, crowd: ${crowd}.`;
  }
  if (conf != null && conf < 0.55) {
    return `Skipped: the bot's confidence (${conf.toFixed(2)}) was below the minimum threshold. Bot estimate: ${bot}, crowd: ${crowd}.`;
  }
  return `Skipped: expected value was not large enough to justify a trade. Bot estimate: ${bot}, crowd: ${crowd}.`;
}

function buildEvaluationExplanation(row: EvaluationRow): string {
  const summary = buildEvaluationSummary(row);
  if (!row.reasoning) return summary;
  return `${summary}\n\nBecause ${row.reasoning}`;
}

export function EvaluationsTable({ evaluations }: { evaluations: EvaluationRow[] | undefined }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const toggle = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (!evaluations || evaluations.length === 0) {
    return (
      <section className="border border-[#1a1a1a] bg-[#050505]">
        <header className="px-3 py-2 border-b border-[#1a1a1a]">
          <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0]">evaluations</h2>
        </header>
        <div className="px-3 py-6 text-center text-xs text-[#666]">
          no evaluations yet — run a scan
        </div>
      </section>
    );
  }

  return (
    <section className="border border-[#1a1a1a] bg-[#050505]">
      <header className="flex items-center justify-between px-3 py-2 border-b border-[#1a1a1a]">
        <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0]">evaluations</h2>
        <span className="text-[10px] text-[#666]">last {evaluations.length} · click row for reasoning</span>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-[#666] text-[10px] uppercase tracking-widest">
            <tr className="border-b border-[#0a0a0a]">
              <Th>market</Th>
              <Th>decision</Th>
              <Th right className="hidden sm:table-cell">bot estimate</Th>
              <Th right className="hidden md:table-cell">crowd price</Th>
              <Th right className="hidden sm:table-cell">mispricing</Th>
              <Th right className="hidden lg:table-cell">confidence</Th>
              <Th right>when</Th>
            </tr>
          </thead>
          <tbody>
            {evaluations.map((r) => {
              const isOpen = expanded.has(r.id);
              return (
                <Fragment key={r.id}>
                  <tr
                    onClick={() => toggle(r.id)}
                    className="border-b border-[#0a0a0a] hover:bg-[#0a0a0a]/50 cursor-pointer"
                  >
                    <td className="px-3 py-2">
                      <div className="flex items-start gap-1.5">
                        <span className="text-[#444] text-[10px] shrink-0 mt-0.5">{isOpen ? "▼" : "▶"}</span>
                        <span className={`text-white ${isOpen ? "whitespace-normal" : "line-clamp-1"}`} title={r.question}>
                          {r.question}
                        </span>
                      </div>
                      <div className="text-[10px] text-[#444] ml-4">
                        #{r.id} · {r.category ?? "other"}
                        {r.pm_position_id ? ` · pos #${r.pm_position_id}` : ""}
                      </div>
                    </td>
                    <td className={`px-3 py-2 font-body whitespace-nowrap ${recommendationColorClass(r.recommendation)}`}>
                      {formatRecommendation(r.recommendation)}
                    </td>
                    <td className="px-3 py-2 text-right font-body text-white hidden sm:table-cell">{prob(r.claude_probability)}</td>
                    <td className="px-3 py-2 text-right font-body text-[#666] hidden md:table-cell">{prob(r.market_price_yes)}</td>
                    <td className="px-3 py-2 text-right font-body text-white hidden sm:table-cell">
                      {r.ev_bps != null ? r.ev_bps.toFixed(0) : "—"}
                    </td>
                    <td className="px-3 py-2 text-right font-body text-white hidden lg:table-cell">
                      {r.confidence != null ? r.confidence.toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2 text-right font-body text-[#a0a0a0] whitespace-nowrap">{timeAgo(r.evaluated_at)}</td>
                  </tr>
                  {isOpen && (
                    <tr className="border-b border-[#0a0a0a] bg-[#0a0a0a]/30">
                      <td colSpan={7} className="px-4 py-3">
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] mb-3">
                          <Detail label="bot estimate" value={prob(r.claude_probability)} />
                          <Detail label="crowd price" value={prob(r.market_price_yes)} />
                          <Detail label="EV" value={r.ev_bps != null ? `${r.ev_bps.toFixed(0)} bps` : "—"} />
                          <Detail label="confidence" value={r.confidence?.toFixed(2) ?? "—"} />
                          <Detail label="evaluated" value={formatTimestamp(r.evaluated_at)} />
                          <Detail label="sources" value={r.research_sources?.length ? `${r.research_sources.length} sources` : "none"} />
                        </div>
                        {r.research_sources && r.research_sources.length > 0 && (
                          <div className="mb-3">
                            <div className="text-[10px] text-[#666] mb-1">research sources</div>
                            <div className="flex flex-wrap gap-1">
                              {r.research_sources.map((src, i) => (
                                <span key={i} className="text-[10px] px-1.5 py-0.5 bg-[#0a0a0a] text-[#a0a0a0]">
                                  {src}
                                </span>
                              ))}
                            </div>
                          </div>
                        )}
                        <div className="text-[10px] text-[#666] mb-1">why this decision</div>
                        <div className="text-xs text-[#ccc] whitespace-pre-wrap leading-relaxed">
                          {buildEvaluationExplanation(r)}
                        </div>
                        <div className="text-[10px] text-[#444] mt-3">
                          market ID: {r.market_id} ·{" "}
                          <a
                            href={polymarketUrl(r.slug, r.market_id, r.event_slug)}
                            target="_blank" rel="noreferrer"
                            className="text-[#00ffff] hover:text-[#00ffff]/70"
                          >
                            open on Polymarket
                          </a>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
