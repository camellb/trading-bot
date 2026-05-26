import { useState } from "react";
import { formatDate, formatDateTime } from "../lib/format";

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

// Tooltip uses cent precision so a settlement of $13.57 reads as
// $13.57 instead of being rounded to $14. Axis tick labels stay at
// round-dollar precision so the y-axis stays readable.
const fmtUsd = (n: number) =>
  `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const fmtUsdAxis = (n: number) =>
  `$${n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;

// Tz-aware formatters live in src/lib/format.ts. The local consts
// here used to roll their own toLocaleDateString / toLocaleString
// without honouring the user's configured display timezone, which
// made the chart's tick labels and hover tooltip read in the OS
// clock even after the user switched the rest of the app to a
// different zone. The thin wrappers below preserve the original
// internal API shape (no-arg, returns string for empty input).
const fmtDate = (s: string): string => s ? formatDate(s) : "";
const fmtDateTime = (s: string): string => s ? formatDateTime(s) : "-";

export function EquityChart({
  series,
  height = H,
  formatValue,
  formatValueAxis,
  strokeColor,
  fillColor,
  // For series where DOWN is good (Brier: 0 is perfect, 1 is worst),
  // pass lowerIsBetter=true to flip the green/red logic. Ignored when
  // strokeColor is also passed.
  lowerIsBetter = false,
  // Pad the y-range minimum span. USD equity defaults to 1.0 ($1).
  // Brier values live in [0,1] so the natural span is small; pass a
  // smaller floor (e.g. 0.01) to keep movement visible.
  minSpan = 1,
}: {
  series: { ts: string; v: number }[];
  height?: number;
  formatValue?: (n: number) => string;
  formatValueAxis?: (n: number) => string;
  strokeColor?: string;
  fillColor?: string;
  lowerIsBetter?: boolean;
  minSpan?: number;
}) {
  const [hoverI, setHoverI] = useState<number | null>(null);

  if (series.length < 2) return null;

  // Pad y-range slightly so the line doesn't touch top/bottom edges.
  const ys = series.map((p) => p.v);
  const rawMin = Math.min(...ys);
  const rawMax = Math.max(...ys);
  const span = Math.max(rawMax - rawMin, minSpan);
  const minY = rawMin - span * 0.08;
  const maxY = rawMax + span * 0.08;

  // X positions are proportional to TIME, not array index. Before
  // this change, the chart placed each data point at an evenly-
  // spaced X regardless of how far apart its timestamp was from its
  // neighbours - so a series with 29 historical points spread over
  // 8 days and 5 snapshot points all on the same day rendered with
  // the same-day cluster taking up roughly a third of the width.
  // The X-tick logic (which picks 5 evenly-spaced indices) then
  // happened to land on two distinct indices that both formatted to
  // the same day, producing duplicate "May 26" labels at the right
  // edge. Time-based positioning fixes both: the cluster collapses
  // to its natural sliver of width, and the 5 tick labels are
  // selected at evenly-spaced TIMES so they can never duplicate
  // unless the entire chart spans less than 24h.
  const tsMs = series.map((p) => Date.parse(p.ts));
  const tsValid = tsMs.every((t) => Number.isFinite(t));
  const minTs = tsValid ? Math.min(...tsMs) : 0;
  const maxTs = tsValid ? Math.max(...tsMs) : 0;
  const timeSpan = maxTs - minTs;
  // Fall back to index-based positioning when timestamps are
  // missing or all collapse to the same instant. Same rendering as
  // before, applied only in the degenerate cases.
  const useTime = tsValid && timeSpan > 0;

  const sxByIndex = (i: number) =>
    PAD_L + (INNER_W * i) / Math.max(1, series.length - 1);
  const sxByTime = (i: number) =>
    PAD_L + (INNER_W * (tsMs[i] - minTs)) / timeSpan;
  const sx = useTime ? sxByTime : sxByIndex;

  const sy = (v: number) =>
    PAD_T + INNER_H - (INNER_H * (v - minY)) / (maxY - minY);

  const d = series.map((p, i) => `${i === 0 ? "M" : "L"}${sx(i)},${sy(p.v)}`).join(" ");
  const lastV = series[series.length - 1].v;
  const firstV = series[0].v;
  // "Positive" = series moved in the user-good direction. For equity
  // that's last >= first; for Brier (lower is better) it's last <= first.
  const positive = lowerIsBetter ? lastV <= firstV : lastV >= firstV;
  const stroke = strokeColor ?? (positive ? "var(--profit)" : "var(--ember)");
  const fill = fillColor ?? (positive ? "rgba(75,255,161,0.08)" : "rgba(255,77,61,0.08)");
  const area = `${d} L${sx(series.length - 1)},${PAD_T + INNER_H} L${sx(0)},${PAD_T + INNER_H} Z`;

  // Value formatters with USD defaults preserved for equity callers.
  const fmtValue = formatValue ?? fmtUsd;
  const fmtAxis  = formatValueAxis ?? fmtUsdAxis;

  // Gridlines: even spacing across the padded range.
  const yTickValues = Array.from({ length: Y_TICKS + 1 }, (_, i) =>
    minY + ((maxY - minY) * i) / Y_TICKS,
  );

  // Date ticks. Time-based mode picks 5 evenly-spaced timestamps
  // across [minTs, maxTs] so the tick gaps reflect calendar time,
  // not data density. Each tick's X position derives from its
  // timestamp via the same sxByTime math the curve uses, so labels
  // line up under the curve point closest in time. Index-based
  // fallback retains the old behaviour for degenerate series.
  const xTickEntries: { x: number; ts: string }[] = useTime
    ? Array.from({ length: X_TICKS + 1 }, (_, k) => {
        const t = minTs + (timeSpan * k) / X_TICKS;
        const x = PAD_L + (INNER_W * (t - minTs)) / timeSpan;
        return { x, ts: new Date(t).toISOString() };
      })
    : Array.from({ length: X_TICKS + 1 }, (_, k) => {
        const idx = Math.round(((series.length - 1) * k) / X_TICKS);
        return { x: sxByIndex(idx), ts: series[idx].ts };
      });

  // Mouse handler. SVG uses preserveAspectRatio="none", so the
  // viewBox stretches to the wrapper's CSS width. Convert client X
  // back to viewBox X by ratio, then to a series index. In time
  // mode we map the cursor's viewBox X back to a timestamp and pick
  // the series point closest in time; in index mode we use the
  // legacy round-to-nearest-index math.
  const handleMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const vbx = ((e.clientX - rect.left) / rect.width) * W;
    if (vbx < PAD_L || vbx > W - PAD_R) {
      setHoverI(null);
      return;
    }
    const ratio = (vbx - PAD_L) / INNER_W;
    let i: number;
    if (useTime) {
      const tHover = minTs + ratio * timeSpan;
      let bestI = 0;
      let bestDelta = Math.abs(tsMs[0] - tHover);
      for (let k = 1; k < tsMs.length; k++) {
        const dt = Math.abs(tsMs[k] - tHover);
        if (dt < bestDelta) {
          bestDelta = dt;
          bestI = k;
        }
      }
      i = bestI;
    } else {
      i = Math.max(0, Math.min(series.length - 1,
        Math.round(ratio * (series.length - 1))));
    }
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
              {fmtAxis(v)}
            </text>
          </g>
        ))}

        {/* Equity area + line. */}
        <path d={area} fill={fill} />
        <path d={d} fill="none" stroke={stroke} strokeWidth="1.6" />

        {/* X-axis date labels. */}
        {xTickEntries.map(({ x, ts }, k) => (
          <text
            key={`x-${k}`}
            x={x}
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
          <div className="eq-tip-val">{fmtValue(tipPoint.v)}</div>
        </div>
      )}
    </div>
  );
}
