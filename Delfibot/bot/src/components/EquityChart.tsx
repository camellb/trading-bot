import { useState } from "react";

/**
 * Equity-history chart used by both Dashboard and Performance.
 *
 * Renders the cumulative-equity curve as an SVG path with proper
 * tick gridlines on the y-axis (5 evenly spaced equity levels) and
 * date marks on the x-axis (5 evenly spaced timestamps from the
 * series). Hover anywhere on the chart to see the date + equity at
 * that point as a floating tooltip.
 *
 * Single source of truth for chart rendering - both pages import
 * this so the visual + the hover behaviour stay aligned.
 */

const W = 800;
const H = 220;
const PAD_T = 12;
const PAD_B = 30;
const PAD_L = 64;
const PAD_R = 12;
const INNER_W = W - PAD_L - PAD_R;
const INNER_H = H - PAD_T - PAD_B;

const Y_TICKS = 4;  // 4 intervals -> 5 gridlines including baseline
const X_TICKS = 4;  // 4 intervals -> 5 date labels

const fmtUsd = (n: number) =>
  `$${n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;

const fmtDate = (s: string): string => {
  if (!s) return "";
  const d = new Date(s);
  if (!Number.isFinite(d.getTime())) return s.slice(0, 10);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};

const fmtDateTime = (s: string): string => {
  if (!s) return "—";
  const d = new Date(s);
  if (!Number.isFinite(d.getTime())) return s.slice(0, 16);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
};

export function EquityChart({
  series,
  height = H,
}: {
  series: { ts: string; v: number }[];
  height?: number;
}) {
  const [hoverI, setHoverI] = useState<number | null>(null);

  if (series.length < 2) return null;

  // Pad y-range slightly so the line doesn't touch top/bottom edges.
  const ys = series.map((p) => p.v);
  const rawMin = Math.min(...ys);
  const rawMax = Math.max(...ys);
  const span = Math.max(rawMax - rawMin, 1);
  const minY = rawMin - span * 0.08;
  const maxY = rawMax + span * 0.08;

  const sx = (i: number) =>
    PAD_L + (INNER_W * i) / Math.max(1, series.length - 1);
  const sy = (v: number) =>
    PAD_T + INNER_H - (INNER_H * (v - minY)) / (maxY - minY);

  const d = series.map((p, i) => `${i === 0 ? "M" : "L"}${sx(i)},${sy(p.v)}`).join(" ");
  const lastV = series[series.length - 1].v;
  const firstV = series[0].v;
  const positive = lastV >= firstV;
  const stroke = positive ? "var(--profit)" : "var(--ember)";
  const fill = positive ? "rgba(75,255,161,0.08)" : "rgba(255,77,61,0.08)";
  const area = `${d} L${sx(series.length - 1)},${PAD_T + INNER_H} L${sx(0)},${PAD_T + INNER_H} Z`;

  // Gridlines: even spacing across the padded range.
  const yTickValues = Array.from({ length: Y_TICKS + 1 }, (_, i) =>
    minY + ((maxY - minY) * i) / Y_TICKS,
  );

  // Date ticks: pick indices evenly across the series.
  const xTickEntries = Array.from({ length: X_TICKS + 1 }, (_, k) => {
    const idx = Math.round(((series.length - 1) * k) / X_TICKS);
    return { i: idx, ts: series[idx].ts };
  });

  // Mouse handler. SVG uses preserveAspectRatio="none", so the
  // viewBox stretches to the wrapper's CSS width. Convert client X
  // back to viewBox X by ratio.
  const handleMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const vbx = ((e.clientX - rect.left) / rect.width) * W;
    if (vbx < PAD_L || vbx > W - PAD_R) {
      setHoverI(null);
      return;
    }
    const ratio = (vbx - PAD_L) / INNER_W;
    const i = Math.max(0, Math.min(series.length - 1,
      Math.round(ratio * (series.length - 1))));
    setHoverI(i);
  };

  const tipPoint = hoverI != null ? series[hoverI] : null;
  const tipLeftPct = hoverI != null ? (sx(hoverI) / W) * 100 : 0;

  return (
    <div className="eq-chart-wrap" style={{ position: "relative" }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="eq-svg"
        preserveAspectRatio="none"
        onMouseMove={handleMove}
        onMouseLeave={() => setHoverI(null)}
        style={{ height }}
      >
        {/* Y-axis gridlines + value labels. Drawn first so the area
            fills over the gridlines. */}
        {yTickValues.map((v, i) => (
          <g key={`y-${i}`}>
            <line
              x1={PAD_L}
              y1={sy(v)}
              x2={W - PAD_R}
              y2={sy(v)}
              stroke="var(--vellum-10)"
              strokeWidth="1"
              strokeDasharray="2 4"
            />
            <text
              x={PAD_L - 8}
              y={sy(v) + 4}
              fontSize="11"
              fill="var(--vellum-40)"
              textAnchor="end"
              fontFamily="var(--font-mono)"
            >
              {fmtUsd(v)}
            </text>
          </g>
        ))}

        {/* Equity area + line. */}
        <path d={area} fill={fill} />
        <path d={d} fill="none" stroke={stroke} strokeWidth="1.6" />

        {/* X-axis date labels. */}
        {xTickEntries.map(({ i, ts }, k) => (
          <text
            key={`x-${k}`}
            x={sx(i)}
            y={H - 10}
            fontSize="11"
            fill="var(--vellum-40)"
            textAnchor={k === 0 ? "start" : k === X_TICKS ? "end" : "middle"}
            fontFamily="var(--font-mono)"
          >
            {fmtDate(ts)}
          </text>
        ))}

        {/* Hover indicator. Drawn LAST so it sits on top. */}
        {hoverI != null && (
          <g>
            <line
              x1={sx(hoverI)}
              y1={PAD_T}
              x2={sx(hoverI)}
              y2={PAD_T + INNER_H}
              stroke="var(--gold)"
              strokeWidth="1"
              strokeOpacity="0.55"
            />
            <circle
              cx={sx(hoverI)}
              cy={sy(series[hoverI].v)}
              r="4"
              fill={stroke}
              stroke="var(--obsidian, #0a0a0a)"
              strokeWidth="2"
            />
          </g>
        )}
      </svg>

      {tipPoint && (
        <div
          className="eq-tip"
          style={{
            left: `${tipLeftPct}%`,
            // Bias the tooltip horizontally so it stays inside the
            // wrapper at the edges instead of clipping off-screen.
            transform:
              tipLeftPct < 12
                ? "translateX(0)"
                : tipLeftPct > 88
                ? "translateX(-100%)"
                : "translateX(-50%)",
          }}
        >
          <div className="eq-tip-date">{fmtDateTime(tipPoint.ts)}</div>
          <div className="eq-tip-val">{fmtUsd(tipPoint.v)}</div>
        </div>
      )}
    </div>
  );
}
