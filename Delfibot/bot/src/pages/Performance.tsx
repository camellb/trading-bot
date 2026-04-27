import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  BrierTrendPoint,
  CalibrationReport,
  PerformanceSummary,
  PMPosition,
} from "../api";

/**
 * Performance page (desktop equivalent of /dashboard/performance).
 *
 * Sections:
 *   1. Snapshot - bankroll, ROI, win rate, Brier.
 *   2. Equity history - SVG sparkline reconstructed from settled
 *      positions cumulating realized P&L from starting_cash.
 *   3. Calibration bins - probability bucket vs predicted vs actual.
 *   4. By category - per-archetype N, Brier, win rate.
 *   5. By horizon - short/medium/long bucket performance.
 *   6. Brier trend - sparkline of brier over time.
 *
 * The equity SVG is reconstructed client-side because the desktop
 * sidecar doesn't take daily snapshots; we walk settled positions and
 * accumulate realized_pnl_usd starting from starting_cash. This matches
 * what the user sees in summary.bankroll at any point in time.
 */

const RANGES: Array<{ id: "all" | "30d" | "7d"; label: string }> = [
  { id: "all", label: "All time" },
  { id: "30d", label: "Last 30 days" },
  { id: "7d",  label: "Last 7 days" },
];

export default function Performance() {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [trend, setTrend] = useState<BrierTrendPoint[]>([]);
  const [calibration, setCalibration] = useState<CalibrationReport | null>(null);
  const [closed, setClosed] = useState<PMPosition[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState<"all" | "30d" | "7d">("all");

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [s, t, c, p] = await Promise.all([
        api.summary(),
        api.brierTrend().then((x) => x.points),
        api.calibration({ source: "polymarket" }),
        api
          .positions(500)
          .then((r) =>
            r.positions
              .filter((x) => x.status === "settled")
              .sort((a, b) =>
                (a.settled_at ?? "") < (b.settled_at ?? "") ? -1 : 1,
              ),
          ),
      ]);
      setSummary(s);
      setTrend(t);
      setCalibration(c);
      setClosed(p);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, [refresh]);

  const equity = useMemo(
    () => buildEquitySeries(closed, summary?.starting_cash ?? 0, range),
    [closed, summary?.starting_cash, range],
  );

  return (
    <>
      <header className="page-header">
        <h1>Performance</h1>
        <div className="page-actions">
          {RANGES.map((r) => (
            <button
              key={r.id}
              className={`btn small ${range === r.id ? "" : "ghost"}`}
              onClick={() => setRange(r.id)}
              type="button"
            >
              {r.label}
            </button>
          ))}
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      {/* Snapshot row */}
      <div className="grid-4" style={{ marginBottom: 20 }}>
        <Tile
          label="Bankroll"
          value={fmtUsd(summary?.bankroll)}
          sub={`from $${(summary?.starting_cash ?? 0).toFixed(0)} starting`}
        />
        <Tile
          label="ROI"
          value={fmtPct(summary?.roi)}
          tone={(summary?.roi ?? 0) >= 0 ? "profit" : "ember"}
          sub={fmtPnL(summary?.realized_pnl)}
        />
        <Tile
          label="Win rate"
          value={
            summary?.win_rate != null
              ? `${(summary.win_rate * 100).toFixed(0)}%`
              : "-"
          }
          sub={`${summary?.settled_wins ?? 0} of ${summary?.settled_total ?? 0} wins`}
        />
        <Tile
          label="Brier"
          value={summary?.brier != null ? summary.brier.toFixed(3) : "-"}
          sub="lower is better"
        />
      </div>

      {/* Equity history */}
      <section className="equity-frame" style={{ marginBottom: 20 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            marginBottom: 12,
          }}
        >
          <h2 className="card-title">Equity history</h2>
          <span className="hero-label">
            {equity.points.length > 0
              ? `${equity.points.length} settled trades`
              : "no settled trades yet"}
          </span>
        </div>
        {equity.points.length === 0 ? (
          <p className="empty">
            Equity will populate as Delfi settles trades. The first settled
            position becomes the first datapoint here.
          </p>
        ) : (
          <EquitySvg points={equity.points} starting={equity.starting} />
        )}
      </section>

      {/* Two-column body: calibration + brier */}
      <div className="grid-2">
        <section className="card">
          <h2 className="card-title">Calibration</h2>
          {!calibration || calibration.resolved === 0 ? (
            <p className="empty">No resolved predictions yet.</p>
          ) : (
            <div>
              {calibration.bins.map((b, idx) => (
                <CalibBin key={idx} bin={b} />
              ))}
              <p
                className="hint"
                style={{ marginTop: 12, fontSize: 12 }}
              >
                Teal = predicted, gold = actual. Closer is better calibration.
              </p>
            </div>
          )}
        </section>

        <section className="card">
          <h2 className="card-title">Brier trend</h2>
          {trend.length === 0 ? (
            <p className="empty">No settled positions yet.</p>
          ) : (
            <BrierSpark points={trend} />
          )}
        </section>

        {/* By category drill-down */}
        <section className="card">
          <h2 className="card-title">By category</h2>
          {!calibration || calibration.by_category.length === 0 ? (
            <p className="empty">Categories appear after the first trades settle.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Category</th>
                  <th className="num">N</th>
                  <th className="num">Brier</th>
                  <th className="num">Win rate</th>
                </tr>
              </thead>
              <tbody>
                {calibration.by_category
                  .filter((c) => c.n > 0)
                  .sort((a, b) => b.n - a.n)
                  .map((c, i) => (
                    <tr key={i}>
                      <td>{c.category || "uncategorized"}</td>
                      <td className="num">{c.n}</td>
                      <td className="num">
                        {c.brier != null ? c.brier.toFixed(3) : "-"}
                      </td>
                      <td className="num">
                        {c.win_rate != null
                          ? `${(c.win_rate * 100).toFixed(0)}%`
                          : "-"}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </section>

        {/* By horizon drill-down */}
        <section className="card">
          <h2 className="card-title">By horizon</h2>
          {!calibration || calibration.by_horizon.length === 0 ? (
            <p className="empty">Horizons appear after the first trades settle.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Horizon</th>
                  <th className="num">N</th>
                  <th className="num">Brier</th>
                  <th className="num">Pred / Actual</th>
                </tr>
              </thead>
              <tbody>
                {calibration.by_horizon
                  .filter((h) => h.n > 0)
                  .map((h, i) => (
                    <tr key={i}>
                      <td>{h.bucket}</td>
                      <td className="num">{h.n}</td>
                      <td className="num">
                        {h.brier != null ? h.brier.toFixed(3) : "-"}
                      </td>
                      <td className="num">
                        {h.mean_pred != null && h.mean_actual != null
                          ? `${h.mean_pred.toFixed(2)} / ${h.mean_actual.toFixed(2)}`
                          : "-"}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </>
  );
}

// ── Equity series reconstructed from settled positions ─────────────────

type EquityPoint = { ts: string; equity: number };

function buildEquitySeries(
  settled: PMPosition[],
  starting: number,
  range: "all" | "30d" | "7d",
): { points: EquityPoint[]; starting: number } {
  const cutoff =
    range === "all"
      ? null
      : Date.now() - (range === "30d" ? 30 : 7) * 86_400_000;

  let equity = starting;
  const series: EquityPoint[] = [];
  for (const p of settled) {
    if (!p.settled_at) continue;
    equity += p.realized_pnl_usd ?? 0;
    series.push({ ts: p.settled_at, equity });
  }
  if (cutoff != null) {
    return {
      points: series.filter((s) => new Date(s.ts).getTime() >= cutoff),
      starting,
    };
  }
  return { points: series, starting };
}

function EquitySvg({
  points,
  starting,
}: {
  points: EquityPoint[];
  starting: number;
}) {
  if (points.length === 0) return null;

  const w = 920;
  const h = 200;
  const padX = 24;
  const padY = 16;

  const xs = points.map((_, i) => {
    const denom = Math.max(1, points.length - 1);
    return padX + (i / denom) * (w - padX * 2);
  });
  const ys = points.map((p) => p.equity);
  const yMin = Math.min(starting, ...ys);
  const yMax = Math.max(starting, ...ys);
  const ySpan = Math.max(1, yMax - yMin);

  const pathD = points
    .map((p, i) => {
      const y = h - padY - ((p.equity - yMin) / ySpan) * (h - padY * 2);
      return `${i === 0 ? "M" : "L"} ${xs[i].toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");

  const fillD = `${pathD} L ${xs[xs.length - 1].toFixed(1)} ${h - padY} L ${xs[0].toFixed(1)} ${h - padY} Z`;

  const yBase =
    h - padY - ((starting - yMin) / ySpan) * (h - padY * 2);
  const last = points[points.length - 1].equity;
  const lastTone = last >= starting ? "profit" : "ember";

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="equity-svg" preserveAspectRatio="none">
      <defs>
        <linearGradient id="eq-fill" x1="0" y1="0" x2="0" y2="1">
          <stop
            offset="0%"
            stopColor={
              last >= starting ? "var(--profit)" : "var(--ember)"
            }
            stopOpacity="0.18"
          />
          <stop offset="100%" stopColor="transparent" stopOpacity="0" />
        </linearGradient>
      </defs>
      <line
        x1={padX}
        x2={w - padX}
        y1={yBase}
        y2={yBase}
        stroke="var(--vellum-20)"
        strokeWidth={1}
        strokeDasharray="3 4"
      />
      <path d={fillD} fill="url(#eq-fill)" />
      <path
        d={pathD}
        fill="none"
        stroke={`var(--${lastTone})`}
        strokeWidth={1.5}
      />
    </svg>
  );
}

// ── Calibration bin row ────────────────────────────────────────────────

function CalibBin({
  bin,
}: {
  bin: {
    lo: number;
    hi: number;
    n: number;
    mean_pred: number | null;
    mean_actual: number | null;
  };
}) {
  const pred = bin.mean_pred ?? 0;
  const actual = bin.mean_actual ?? 0;
  return (
    <div className="calib-bin">
      <span style={{ color: "var(--vellum)" }}>
        {bin.lo.toFixed(1)} - {bin.hi.toFixed(1)}
      </span>
      <span style={{ color: "var(--vellum-60)" }}>n = {bin.n}</span>
      <div className="calib-bar">
        <span
          className="pred"
          style={{ width: `${Math.min(100, pred * 100)}%` }}
        />
        <span
          className="actual"
          style={{ width: `${Math.min(100, actual * 100)}%` }}
        />
      </div>
      <span style={{ color: "var(--vellum-60)" }}>
        {pred.toFixed(2)} / {actual.toFixed(2)}
      </span>
    </div>
  );
}

// ── Brier sparkline ────────────────────────────────────────────────────

function BrierSpark({ points }: { points: BrierTrendPoint[] }) {
  if (points.length < 2) {
    const last = points[0];
    return (
      <p style={{ margin: 0 }}>
        Brier <strong>{last.brier.toFixed(3)}</strong> on {last.n} trade
        {last.n === 1 ? "" : "s"}.
      </p>
    );
  }
  const w = 360;
  const h = 80;
  const xs = points.map((_, i) => (i / (points.length - 1)) * w);
  const ys = points.map((p) => p.brier);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const span = Math.max(0.001, yMax - yMin);
  const path = points
    .map((p, i) => {
      const y = h - ((p.brier - yMin) / span) * h;
      return `${i === 0 ? "M" : "L"} ${xs[i].toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
  const last = points[points.length - 1];
  return (
    <div className="brier-spark">
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h}>
        <path d={path} fill="none" stroke="var(--gold)" strokeWidth="1.5" />
      </svg>
      <p className="brier-foot">
        Latest <strong style={{ color: "var(--gold)" }}>
          {last.brier.toFixed(3)}
        </strong> on {last.n} trades. Lower is better; perfect calibration is 0.
      </p>
    </div>
  );
}

// ── Tiles ──────────────────────────────────────────────────────────────

function Tile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "profit" | "ember" | "gold";
}) {
  return (
    <div className="kpi-tile">
      <span className="kpi-label">{label}</span>
      <span className={`kpi-value ${tone ?? ""}`.trim()}>{value}</span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  );
}

// ── Format helpers ─────────────────────────────────────────────────────

function fmtUsd(v: number | null | undefined): string {
  if (v == null) return "-";
  return `$${v.toFixed(2)}`;
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "-";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtPnL(v: number | null | undefined): string {
  if (v == null) return "-";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}
