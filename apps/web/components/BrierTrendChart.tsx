"use client";

import type { BrierTrendPoint } from "@/hooks/use-dashboard-data";
import { useContainerSize } from "@/hooks/use-container-size";
import { useMemo, useRef } from "react";
import {
  CartesianGrid, Line, LineChart,
  ReferenceLine, Tooltip, XAxis, YAxis,
} from "recharts";

function TrendBody({
  points,
  heightClass,
}: {
  points: BrierTrendPoint[] | null;
  heightClass: string;
}) {
  const chartRef = useRef<HTMLDivElement>(null);
  const dims = useContainerSize(chartRef);

  const data = useMemo(() => {
    const pts = points ?? [];
    const dates = pts
      .map((p) => (p.date ? new Date(p.date) : null))
      .filter((d): d is Date => d != null && !Number.isNaN(d.getTime()));
    const singleDay = dates.length > 0
      ? dates.every((d) => d.toDateString() === dates[0].toDateString())
      : false;

    return pts.map((p) => ({
      ...p,
      label: p.date
        ? new Date(p.date).toLocaleString(undefined, singleDay
          ? { hour: "2-digit", minute: "2-digit" }
          : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
        : `#${p.n}`,
    }));
  }, [points]);

  return (
    <div ref={chartRef} className={`${heightClass} min-w-0`}>
      {data.length >= 2 && dims ? (
        <LineChart
          width={dims.w} height={dims.h}
          data={data}
          margin={{ top: 5, right: 10, bottom: 5, left: 0 }}
        >
          <CartesianGrid strokeDasharray="2 4" stroke="#1a1a1a" />
          <XAxis
            dataKey="label" stroke="#444" fontSize={10}
            interval="preserveStartEnd"
          />
          <YAxis
            domain={[0, 0.5]} stroke="#444" fontSize={10}
            tickFormatter={(v: number) => v.toFixed(2)}
          />
          <ReferenceLine
            y={0.22}
            stroke="#f59e0b"
            strokeDasharray="4 4"
            label={{ value: "gate 0.22", position: "right", fill: "#f59e0b", fontSize: 9 }}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#050505", border: "1px solid #1a1a1a",
              fontSize: 11, fontFamily: "var(--font-body)",
              borderRadius: "0px",
            }}
            formatter={(v) =>
              [typeof v === "number" ? v.toFixed(4) : String(v ?? ""), "brier"]
            }
            labelFormatter={(l) => String(l ?? "")}
          />
          <Line
            type="monotone" dataKey="brier" stroke="#00ffff"
            dot={false} strokeWidth={2}
          />
        </LineChart>
      ) : (
        <div className="h-full flex items-center justify-center text-xs text-[#666]">
          {data.length < 2
            ? "need at least 2 resolved predictions for trend"
            : "measuring…"}
        </div>
      )}
    </div>
  );
}

export function BrierTrendChart({
  points,
  embedded = false,
}: {
  points: BrierTrendPoint[] | null;
  embedded?: boolean;
}) {
  if (embedded) {
    return (
      <div>
        <div className="flex items-center justify-between gap-2 mb-3">
          <div>
            <h3 className="text-[10px] uppercase tracking-widest text-[#666]">brier trend</h3>
            <div className="text-[11px] text-[#444]">lower is better</div>
          </div>
          <span className="text-[10px] text-[#444]">resolved predictions only</span>
        </div>
        <TrendBody points={points} heightClass="h-40" />
      </div>
    );
  }

  return (
    <section className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#1a1a1a]">
        <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0]">brier trend</h2>
        <span className="text-[10px] text-[#666]">
          snapshot history when available, otherwise cumulative fallback
        </span>
      </header>
      <div className="p-4">
        <TrendBody points={points} heightClass="h-48" />
      </div>
    </section>
  );
}
