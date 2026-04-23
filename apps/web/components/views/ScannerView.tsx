"use client";

import type { EvaluationRow } from "@/hooks/use-dashboard-data";
import type { ToastFn } from "@/lib/format";
import {
  polymarketUrl, prob, recommendationColorClass,
  timeAgo, formatTimestamp, formatRecommendation,
} from "@/lib/format";
import { DetailField } from "@/components/ui/DetailField";
import { ScanReveal } from "@/components/kinetic/ScanReveal";
import { LivePulse } from "@/components/kinetic/LivePulse";
import { useScanReveal } from "@/hooks/use-scan-reveal";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

type FilterTab = "all" | "traded" | "skipped";

function buildEvaluationSummary(row: EvaluationRow): string {
  const bot = prob(row.claude_probability);
  const crowd = prob(row.market_price_yes);

  if (row.recommendation === "BUY_YES") {
    return `The bot would buy YES because its estimate for YES (${bot}) is above the crowd price (${crowd}).`;
  }
  if (row.recommendation === "BUY_NO") {
    return `The bot would buy NO because its estimate for YES (${bot}) is below the crowd price (${crowd}).`;
  }
  if (row.skip_reason) {
    return `Skipped: ${row.skip_reason}.`;
  }
  const ev = row.ev_bps;
  const conf = row.confidence;
  if (ev != null && ev < 500) {
    return `Skipped: expected value (${ev.toFixed(0)} bps) was too small after costs. Bot: ${bot}, crowd: ${crowd}.`;
  }
  if (conf != null && conf < 0.55) {
    return `Skipped: confidence (${conf.toFixed(2)}) below threshold. Bot: ${bot}, crowd: ${crowd}.`;
  }
  return `Skipped: insufficient expected value. Bot: ${bot}, crowd: ${crowd}.`;
}

export function ScannerView({
  evaluations,
  toast,
  refresh,
}: {
  evaluations: EvaluationRow[] | undefined;
  toast: ToastFn;
  refresh: () => void;
}) {
  const [filter, setFilter] = useState<FilterTab>("all");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [scanning, setScanning] = useState(false);
  const {
    phase, phaseLabel, isRevealing, startReveal, markDataReady,
    total, processed, opened, currentMarket,
  } = useScanReveal();
  const prevEvalCount = useRef(evaluations?.length ?? 0);

  // Detect when new evaluation data arrives after a scan
  useEffect(() => {
    const currentCount = evaluations?.length ?? 0;
    if (scanning && currentCount > prevEvalCount.current) {
      markDataReady();
    }
    prevEvalCount.current = currentCount;
  }, [evaluations?.length, scanning, markDataReady]);

  const tradedCount = useMemo(() => (evaluations ?? []).filter(e => e.recommendation === "BUY_YES" || e.recommendation === "BUY_NO").length, [evaluations]);
  const skippedCount = useMemo(() => (evaluations ?? []).filter(e => e.recommendation === "SKIP").length, [evaluations]);

  const toggle = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const rows = (evaluations ?? []).filter((r) => {
    if (filter === "traded") return r.recommendation === "BUY_YES" || r.recommendation === "BUY_NO";
    if (filter === "skipped") return r.recommendation === "SKIP";
    return true;
  });

  const handleScan = async () => {
    setScanning(true);
    startReveal();
    try {
      const res = await fetch("/api/scan-now", { method: "POST" });
      if (res.ok) {
        toast("Market scan triggered");
        setTimeout(() => { refresh(); markDataReady(); }, 1500);
      } else {
        toast("Scan failed", "error");
        markDataReady();
      }
    } catch {
      toast("Request failed", "error");
      markDataReady();
    } finally {
      setScanning(false);
    }
  };

  return (
    <div className="space-y-6 max-w-[1400px]">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-headline text-white">Market Scanner</h1>
          <p className="text-xs text-[#666] mt-1">
            Scan, filter, and review automated evaluations across active markets.
          </p>
        </div>
        <button
          onClick={handleScan}
          disabled={scanning || isRevealing}
          className="flex items-center gap-2 px-4 py-2 text-xs font-medium bg-accent text-surface-0
                     hover:bg-accent-bright disabled:opacity-50 transition-colors"
        >
          {(scanning || isRevealing) && <LivePulse active size="xs" color="accent" />}
          {scanning || isRevealing ? "Scanning..." : "Scan Now"}
        </button>
      </div>

      {/* Scan Reveal Animation */}
      <ScanReveal
        phase={phase}
        phaseLabel={phaseLabel}
        total={total}
        processed={processed}
        opened={opened}
        currentMarket={currentMarket}
      />

      {/* Filter tabs */}
      <div className="flex items-center gap-3">
        <div className="flex bg-surface-2 border border-[#1a1a1a] p-0.5">
          {(["all", "traded", "skipped"] as FilterTab[]).map((t) => (
            <button
              key={t}
              onClick={() => setFilter(t)}
              className={`px-4 py-1.5 text-xs font-medium transition-colors capitalize ${
                filter === t
                  ? "bg-accent text-surface-0"
                  : "text-[#a0a0a0] hover:text-white"
              }`}
            >
              {t === "all" ? `All (${evaluations?.length ?? 0})` :
               t === "traded" ? `Traded (${tradedCount})` :
               `Skipped (${skippedCount})`}
            </button>
          ))}
        </div>
      </div>

      {/* Evaluations Table */}
      <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-[#1a1a1a] text-[10px] uppercase tracking-widest text-[#444]">
                <th className="text-left px-4 py-2.5 font-medium">Market</th>
                <th className="text-center px-3 py-2.5 font-medium">Decision</th>
                <th className="text-right px-3 py-2.5 font-medium hidden sm:table-cell">Bot Est.</th>
                <th className="text-right px-3 py-2.5 font-medium hidden md:table-cell">Crowd</th>
                <th className="text-right px-3 py-2.5 font-medium hidden sm:table-cell">Δ</th>
                <th className="text-right px-3 py-2.5 font-medium hidden lg:table-cell">Conf.</th>
                <th className="text-right px-3 py-2.5 font-medium">When</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1a1a1a]">
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-12 text-center text-sm text-[#444]">
                    No evaluations yet - run a scan
                  </td>
                </tr>
              ) : (
                rows.map((r) => {
                  const isOpen = expanded.has(r.id);
                  return (
                    <Fragment key={r.id}>
                      <tr
                        onClick={() => toggle(r.id)}
                        className="hover:bg-surface-3/50 cursor-pointer transition-colors"
                      >
                        <td className="px-4 py-3">
                          <div className="flex items-start gap-2">
                            <span className="text-[#444] text-[10px] mt-0.5 shrink-0">
                              {isOpen ? "▼" : "▶"}
                            </span>
                            <div>
                              <span className={`text-white ${isOpen ? "whitespace-normal" : "line-clamp-1"}`}>
                                {r.question}
                              </span>
                              <div className="text-[10px] text-[#444]">
                                #{r.id} · {r.category ?? "other"}
                                {r.pm_position_id ? ` · pos #${r.pm_position_id}` : ""}
                              </div>
                            </div>
                          </div>
                        </td>
                        <td className="px-3 py-3 text-center">
                          <span className={`inline-block px-2 py-0.5 text-[10px] font-semibold ${
                            r.recommendation === "BUY_YES" ? "bg-accent-dim text-accent" :
                            r.recommendation === "BUY_NO" ? "bg-red-500/10 text-red-400" :
                            "bg-[#1a1a1a] text-[#666]"
                          }`}>
                            {formatRecommendation(r.recommendation)}
                          </span>
                        </td>
                        <td className="px-3 py-3 text-right font-body text-[#ccc] hidden sm:table-cell">
                          {prob(r.claude_probability)}
                        </td>
                        <td className="px-3 py-3 text-right font-body text-[#666] hidden md:table-cell">
                          {prob(r.market_price_yes)}
                        </td>
                        <td className="px-3 py-3 text-right font-body text-[#ccc] hidden sm:table-cell">
                          {r.ev_bps != null ? r.ev_bps.toFixed(0) : "-"}
                        </td>
                        <td className="px-3 py-3 text-right font-body text-[#ccc] hidden lg:table-cell">
                          {r.confidence != null ? r.confidence.toFixed(2) : "-"}
                        </td>
                        <td className="px-3 py-3 text-right text-[#666] whitespace-nowrap">
                          {timeAgo(r.evaluated_at)}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-surface-3/30">
                          <td colSpan={7} className="px-4 py-4">
                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-[11px] mb-3">
                              <DetailField label="Bot estimate" value={prob(r.claude_probability)} />
                              <DetailField label="Crowd price" value={prob(r.market_price_yes)} />
                              <DetailField label="EV" value={r.ev_bps != null ? `${r.ev_bps.toFixed(0)} bps` : "-"} />
                              <DetailField label="Confidence" value={r.confidence?.toFixed(2) ?? "-"} />
                              <DetailField label="Evaluated" value={formatTimestamp(r.evaluated_at)} />
                              <DetailField label="Sources" value={r.research_sources?.length ? `${r.research_sources.length} sources` : "none"} />
                            </div>
                            {r.research_sources && r.research_sources.length > 0 && (
                              <div className="mb-3">
                                <div className="text-[10px] uppercase tracking-widest text-[#444] mb-1">Research Sources</div>
                                <div className="flex flex-wrap gap-1">
                                  {r.research_sources.map((src, i) => (
                                    <span key={i} className="text-[10px] px-1.5 py-0.5 bg-surface-0/60 text-[#a0a0a0]">
                                      {src}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}
                            <div className="text-[10px] uppercase tracking-widest text-[#444] mb-1">Why this decision</div>
                            <div className="text-xs text-[#a0a0a0] whitespace-pre-wrap leading-relaxed mb-3">
                              {buildEvaluationSummary(r)}
                              {r.reasoning ? `\n\nBecause ${r.reasoning}` : ""}
                            </div>
                            <div className="text-[10px] text-[#444]">
                              market ID: {r.market_id} ·{" "}
                              <a
                                href={polymarketUrl(r.slug, r.market_id, r.event_slug)}
                                target="_blank" rel="noreferrer"
                                className="text-accent hover:text-accent-bright"
                              >
                                Open on Polymarket &rarr;
                              </a>
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
