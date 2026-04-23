"use client";

/**
 * ScanReveal - staged execution visualization.
 *
 * Shows a phased progress bar when a scan is running,
 * building anticipation before results appear.
 */

import type { ScanPhase } from "@/hooks/use-scan-reveal";

const PHASES: { key: ScanPhase; label: string; icon: string }[] = [
  { key: "fetching", label: "Fetching Markets", icon: "📡" },
  { key: "analyzing", label: "Analyzing Markets", icon: "🧠" },
];

function phaseIndex(phase: ScanPhase): number {
  const idx = PHASES.findIndex((p) => p.key === phase);
  if (idx !== -1) return idx;
  if (phase === "complete") return PHASES.length;
  return -1;
}

export function ScanReveal({
  phase,
  phaseLabel,
  processed,
  total,
  opened,
  currentMarket,
}: {
  phase: ScanPhase;
  phaseLabel: string;
  processed?: number | null;
  total?: number | null;
  opened?: number | null;
  currentMarket?: string | null;
}) {
  if (phase === "idle") return null;

  const activeIdx = phaseIndex(phase);
  const isComplete = phase === "complete";
  const isError = phase === "error";
  const pctDone =
    total != null && total > 0 && processed != null
      ? Math.min(100, Math.round((processed / total) * 100))
      : null;

  return (
    <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden mb-4">
      <div className="px-4 py-3 border-b border-[#1a1a1a]">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${
              isError ? "bg-red-500" :
              isComplete ? "bg-accent" :
              "bg-accent animate-scan-pulse"
            }`} />
            <span className={`text-xs font-medium ${isError ? "text-red-400" : "text-[#ccc]"}`}>
              {phaseLabel}
            </span>
          </div>
          <div className="flex items-center gap-3 text-[10px] text-[#666] font-body">
            {total != null && processed != null && total > 0 && (
              <span>
                {processed}/{total} markets
                {pctDone != null ? ` · ${pctDone}%` : ""}
              </span>
            )}
            {opened != null && opened > 0 && (
              <span className="text-accent">{opened} opened</span>
            )}
            {!isComplete && !isError && <span>live</span>}
          </div>
        </div>
        {currentMarket && !isComplete && !isError && (
          <div className="mt-1.5 text-[10px] text-[#888] font-body truncate">
            Evaluating: {currentMarket}
          </div>
        )}
        {pctDone != null && (
          <div className="mt-2 h-[3px] bg-surface-0/60 overflow-hidden">
            <div
              className={`h-full transition-all duration-500 ${
                isError ? "bg-red-500/70" : "bg-accent"
              }`}
              style={{ width: `${pctDone}%` }}
            />
          </div>
        )}
      </div>

      <div className="px-4 py-4">
        <div className="flex items-center gap-2">
          {PHASES.map((p, i) => {
            const done = activeIdx > i;
            const active = activeIdx === i;

            return (
              <div key={p.key} className="flex-1 flex items-center gap-2">
                {/* Phase node */}
                <div className="flex flex-col items-center gap-1.5 min-w-[80px]">
                  <div
                    className={`
                      w-8 h-8 flex items-center justify-center text-sm
                      transition-all duration-500
                      ${done
                        ? "bg-accent/20 ring-1 ring-accent"
                        : active
                          ? "bg-accent/10 ring-1 ring-accent/50 animate-scan-pulse"
                          : "bg-surface-3 ring-1 ring-[#1a1a1a]"
                      }
                    `}
                  >
                    {done ? (
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#00ffff" strokeWidth="2.5">
                        <path d="M20 6L9 17l-5-5" />
                      </svg>
                    ) : (
                      <span className={active ? "" : "opacity-40"}>{p.icon}</span>
                    )}
                  </div>
                  <span className={`text-[10px] tracking-wide ${done ? "text-accent" : active ? "text-[#ccc]" : "text-[#666]"}`}>
                    {p.label}
                  </span>
                </div>

                {/* Connector line */}
                {i < PHASES.length - 1 && (
                  <div className="flex-1 h-px bg-[#1a1a1a] relative">
                    <div
                      className="absolute inset-y-0 left-0 bg-accent transition-all duration-700"
                      style={{ width: done ? "100%" : active ? "50%" : "0%" }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
