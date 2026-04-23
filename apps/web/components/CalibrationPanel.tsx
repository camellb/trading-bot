"use client";

import type {
  BrierTrendPoint, CalibrationBin, CalibrationData,
} from "@/hooks/use-dashboard-data";
import { useContainerSize } from "@/hooks/use-container-size";
import { BrierTrendChart } from "./BrierTrendChart";
import { useRef } from "react";
import {
  CartesianGrid, ReferenceLine,
  Scatter, ScatterChart, Tooltip, XAxis, YAxis,
} from "recharts";

const EMPTY_BINS: CalibrationBin[] = [];

export function CalibrationPanel({
  data,
  brierTrend,
  settledCount,
}: {
  data: CalibrationData | null;
  brierTrend: BrierTrendPoint[] | null;
  settledCount: number | null;
}) {
  const bins = data?.bins ?? EMPTY_BINS;
  const chartRef = useRef<HTMLDivElement>(null);
  const dims = useContainerSize(chartRef);

  return (
    <section className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#1a1a1a]">
        <h3 className="text-xs uppercase tracking-widest text-[#a0a0a0]">calibration</h3>
        <div className="text-[10px] text-right leading-relaxed text-[#666]">
          <div>{data?.resolved ?? 0}/{data?.total ?? 0} resolved predictions · last 30d</div>
          <div className="text-[#444]">{settledCount ?? 0} settled bets · includes skipped calls</div>
        </div>
      </header>
      <div className="grid grid-cols-1 md:grid-cols-[1fr_280px] gap-4 p-4">
        <div ref={chartRef} className="h-56 min-w-0">
          {bins.length > 0 && dims ? (
            <ScatterChart
              width={dims.w} height={dims.h}
              margin={{ top: 5, right: 10, bottom: 5, left: 0 }}
            >
              <CartesianGrid strokeDasharray="2 4" stroke="#1a1a1a" />
              <XAxis
                type="number" dataKey="mean_pred" domain={[0, 1]}
                tickFormatter={(v) => v.toFixed(1)}
                stroke="#444" fontSize={10}
                label={{
                  value: "bot estimate (YES)", position: "insideBottom",
                  offset: -2, fill: "#666", fontSize: 10,
                }}
              />
              <YAxis
                type="number" dataKey="mean_actual" domain={[0, 1]}
                tickFormatter={(v) => v.toFixed(1)}
                stroke="#444" fontSize={10}
                label={{
                  value: "actual YES rate", angle: -90,
                  position: "insideLeft", offset: 10,
                  fill: "#666", fontSize: 10,
                }}
              />
              <ReferenceLine
                segment={[{ x: 0, y: 0 }, { x: 1, y: 1 }]}
                stroke="#444" strokeDasharray="4 4"
              />
              <Tooltip
                cursor={false}
                contentStyle={{
                  backgroundColor: "#050505", border: "1px solid #1a1a1a",
                  fontSize: 11, fontFamily: "var(--font-body)",
                  borderRadius: "0px",
                }}
                formatter={(v) =>
                  typeof v === "number" ? v.toFixed(3) : String(v ?? "")
                }
              />
              <Scatter data={bins} fill="#00ffff" />
            </ScatterChart>
          ) : (
            <div className="h-full flex items-center justify-center text-xs text-[#666]">
              {bins.length === 0
                ? "no resolved predictions yet - diagonal = perfect calibration"
                : "measuring…"}
            </div>
          )}
        </div>
        <div className="flex flex-col gap-3 text-xs">
          <Metric label="brier score" value={data?.brier != null ? data.brier.toFixed(3) : "-"} />
          <Metric label="avg bot estimate" value={data?.mean_prob != null ? data.mean_prob.toFixed(3) : "-"} />
          <Metric label="actual YES rate" value={data?.mean_outcome != null ? data.mean_outcome.toFixed(3) : "-"} />
          <Metric label="still open" value={data?.unresolved?.toString() ?? "-"} />
          {data?.by_category && data.by_category.length > 0 && (
            <div className="pt-2 border-t border-[#1a1a1a]">
              <div className="text-[10px] uppercase tracking-widest text-[#666] mb-1">
                by category (predictions)
              </div>
              {data.by_category.map((c) => (
                <div key={c.category} className="flex justify-between font-body text-[11px] text-[#ccc] py-0.5">
                  <span>{c.category}</span>
                  <span className="text-[#666]">
                    {c.n} · B={c.brier != null ? c.brier.toFixed(3) : "-"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="border-t border-[#1a1a1a] px-4 py-3">
        <BrierTrendChart points={brierTrend} embedded />
      </div>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between items-baseline">
      <span className="text-[10px] uppercase tracking-widest text-[#666]">{label}</span>
      <span className="font-body text-white">{value}</span>
    </div>
  );
}
