import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  BrierTrendPoint,
  CalibrationReport,
  PerformanceSummary,
  PMPosition,
} from "../api";

/**
 * Performance - SaaS-parity layout.
 *
 * page-wrap with title + range chips, then:
 *   - stat-row: Bankroll, Realized P&L, Win rate, Brier (4 tiles)
 *   - Equity chart (SVG, reconstructed client-side from settled positions)
 *   - Calibration bins (predicted vs actual)
 *   - Brier trend sparkline
 *   - By category / by horizon tables
 */

type Range = "all" | "30d" | "7d";

const RANGES: { id: Range; label: string }[] = [
  { id: "all", label: "All time" },
  { id: "30d", label: "30 days" },
  { id: "7d",  label: "7 days" },
];

const RANGE_DAYS: Record<Exclude<Range, "all">, number> = { "30d": 30, "7d": 7 };

function fmtMoney(v: number): string {
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs >= 1000) return `${sign}$${abs.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  return `${sign}$${abs.toFixed(2)}`;
}
function fmtPct(v: number, digits = 1): string {
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}%`;
}

export default function Performance() {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [trend, setTrend] = useState<BrierTrendPoint[]>([]);
  const [calibration, setCalibration] = useState<CalibrationReport | null>(null);
  const [closed, setClosed] = useState<PMPosition[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState<Range>("all");
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [s, t, c, p] = await Promise.all([
        api.summary(),
        api.brierTrend().then((x) => x.points),
        api.calibration({ source: "polymarket" }),
        api.positions(500).then((r) =>
          r.positions
            .filter((x) => x.status === "settled")
            .sort((a, b) => ((a.settled_at ?? "") < (b.settled_at ?? "") ? -1 : 1)),
        ),
      ]);
      setSummary(s);
      setTrend(t);
      setCalibration(c);
      setClosed(p);
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 60_000);
    return () => clearInterval(id);
  }, [refresh]);

  const filtered = useMemo(() => {
    if (range === "all") return closed;
    const cutoff = Date.now() - RANGE_DAYS[range] * 86_400_000;
    return closed.filter((p) => {
      if (!p.settled_at) return false;
      const t = new Date(p.settled_at).getTime();
      return Number.isFinite(t) && t >= cutoff;
    });
  }, [closed, range]);

  const filteredStats = useMemo(() => {
    let trades = 0, wins = 0, losses = 0, totalPnl = 0, totalCost = 0;
    for (const r of filtered) {
      const pnl = (r.realized_pnl_usd as number | null | undefined) ?? 0;
      const outcome = r.settlement_outcome as string | null | undefined;
      if (outcome == null) continue;
      trades++;
      if (outcome === r.side) wins++; else losses++;
      totalPnl += pnl;
      totalCost += r.cost_usd ?? 0;
    }
    const winRate = trades > 0 ? (wins / trades) * 100 : 0;
    const roi = totalCost > 0 ? (totalPnl / totalCost) * 100 : 0;
    return { trades, wins, losses, totalPnl, totalCost, winRate, roi };
  }, [filtered]);

  const equitySeries = useMemo(() => {
    const start = summary?.starting_cash ?? 0;
    let cum = start;
    return [{ ts: "", v: start }, ...filtered.map((r) => {
      cum += (r.realized_pnl_usd as number | null | undefined) ?? 0;
      return { ts: r.settled_at ?? "", v: cum };
    })];
  }, [summary, filtered]);

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Performance</h1>
            <p className="page-sub">
              ROI, calibration, and category-level breakdowns. Numbers count only positions Delfi actually entered.
            </p>
          </div>
          <div className="page-head-right">
            {RANGES.map((r) => (
              <button
                key={r.id}
                className={`chip ${range === r.id ? "on" : ""}`}
                onClick={() => setRange(r.id)}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="stat-row">
        <div className="stat-cell">
          <div className="stat-cell-label">Bankroll</div>
          <div className="stat-cell-val t-num">
            {summary ? fmtMoney(summary.bankroll ?? summary.starting_cash ?? 0) : "-"}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Realized P&amp;L</div>
          <div className={`stat-cell-val t-num ${filteredStats.totalPnl > 0 ? "profit" : filteredStats.totalPnl < 0 ? "ember" : ""}`}>
            {loaded ? fmtMoney(filteredStats.totalPnl) : "-"}
          </div>
          <div className={`stat-cell-delta ${filteredStats.roi < 0 ? "down" : ""}`}>
            {loaded && filteredStats.totalCost > 0 ? `${fmtPct(filteredStats.roi)} ROI` : ""}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Win rate</div>
          <div className="stat-cell-val t-num">
            {filteredStats.trades > 0 ? `${filteredStats.winRate.toFixed(0)}%` : "-"}
          </div>
          <div className="stat-cell-delta">
            {filteredStats.trades > 0 ? `${filteredStats.wins}W / ${filteredStats.losses}L` : ""}
          </div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Brier score</div>
          <div className="stat-cell-val t-num gold">
            {summary?.brier != null ? summary.brier.toFixed(3) : "-"}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Equity history</h2>
          <span className="panel-meta">{filteredStats.trades} settled trades</span>
        </div>
        {filteredStats.trades === 0 ? (
          <div className="empty-state">
            {loaded ? "No equity history yet, take a trade to see this curve." : "Loading..."}
          </div>
        ) : (
          <EquityChart series={equitySeries} />
        )}
      </div>

      {calibration && calibration.bins.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Calibration</h2>
            <span className="panel-meta">
              {calibration.resolved} resolved · Brier {calibration.brier?.toFixed(3) ?? "-"}
            </span>
          </div>
          <div>
            {calibration.bins.map((b, i) => {
              const predPct = (b.mean_pred ?? 0) * 100;
              const actualPct = (b.mean_actual ?? 0) * 100;
              return (
                <div className="calib-bin" key={i}>
                  <div>{(b.lo * 100).toFixed(0)}-{(b.hi * 100).toFixed(0)}%</div>
                  <div>{b.n} trades</div>
                  <div className="calib-bar">
                    <div className="pred"   style={{ width: `${predPct}%` }} />
                    <div className="actual" style={{ width: `${actualPct}%` }} />
                  </div>
                  <div>{actualPct.toFixed(0)}%</div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {trend.length > 1 && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Brier trend</h2>
            <span className="panel-meta">Lower is better, 0 is perfect</span>
          </div>
          <BrierSpark points={trend} />
        </div>
      )}

      {calibration && calibration.by_category.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">By category</h2>
            <span className="panel-meta">{calibration.by_category.length} archetypes</span>
          </div>
          <table className="table-simple">
            <thead>
              <tr>
                <th>Category</th>
                <th>Trades</th>
                <th>Brier</th>
                <th>Win rate</th>
              </tr>
            </thead>
            <tbody>
              {calibration.by_category.map((c, i) => (
                <tr key={i}>
                  <td>{c.category ?? "Uncategorised"}</td>
                  <td className="mono">{c.n}</td>
                  <td className="mono">{c.brier?.toFixed(3) ?? "-"}</td>
                  <td className="mono">{c.win_rate != null ? `${(c.win_rate * 100).toFixed(0)}%` : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {calibration && calibration.by_horizon.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">By horizon</h2>
            <span className="panel-meta">Resolution time bucket</span>
          </div>
          <table className="table-simple">
            <thead>
              <tr>
                <th>Bucket</th>
                <th>Trades</th>
                <th>Brier</th>
                <th>Mean predicted</th>
                <th>Mean actual</th>
              </tr>
            </thead>
            <tbody>
              {calibration.by_horizon.map((h, i) => (
                <tr key={i}>
                  <td>{h.bucket}</td>
                  <td className="mono">{h.n}</td>
                  <td className="mono">{h.brier?.toFixed(3) ?? "-"}</td>
                  <td className="mono">{h.mean_pred != null ? `${(h.mean_pred * 100).toFixed(0)}%` : "-"}</td>
                  <td className="mono">{h.mean_actual != null ? `${(h.mean_actual * 100).toFixed(0)}%` : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function EquityChart({ series }: { series: { ts: string; v: number }[] }) {
  if (series.length < 2) return null;
  const W = 800, H = 180, PAD = 8;
  const xs = series.map((_, i) => i);
  const ys = series.map((p) => p.v);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const sx = (i: number) => PAD + ((W - PAD * 2) * i) / Math.max(1, xs.length - 1);
  const sy = (v: number) => H - PAD - ((H - PAD * 2) * (v - minY)) / Math.max(1, maxY - minY);
  const d = series.map((p, i) => `${i === 0 ? "M" : "L"}${sx(i)},${sy(p.v)}`).join(" ");
  const lastV = series[series.length - 1].v;
  const firstV = series[0].v;
  const positive = lastV >= firstV;
  const color = positive ? "var(--profit)" : "var(--ember)";
  const fill = positive ? "rgba(75,255,161,0.08)" : "rgba(255,77,61,0.08)";
  const area = `${d} L${sx(series.length - 1)},${H - PAD} L${sx(0)},${H - PAD} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="eq-svg" preserveAspectRatio="none">
      <path d={area} fill={fill} />
      <path d={d} fill="none" stroke={color} strokeWidth="1.6" />
      <line x1={sx(0)} y1={sy(firstV)} x2={sx(series.length - 1)} y2={sy(firstV)}
            stroke="var(--vellum-10)" strokeDasharray="2 4" />
    </svg>
  );
}

function BrierSpark({ points }: { points: BrierTrendPoint[] }) {
  if (points.length < 2) return null;
  const W = 800, H = 60, PAD = 6;
  const ys = points.map((p) => p.brier);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const sx = (i: number) => PAD + ((W - PAD * 2) * i) / (points.length - 1);
  const sy = (v: number) => H - PAD - ((H - PAD * 2) * (v - minY)) / Math.max(1e-9, maxY - minY);
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${sx(i)},${sy(p.brier)}`).join(" ");
  return (
    <div className="brier-spark">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <path d={d} fill="none" stroke="var(--gold)" strokeWidth="1.4" />
      </svg>
      <p className="brier-foot">
        {points[0].brier.toFixed(3)} → {points[points.length - 1].brier.toFixed(3)} ·
        {" "}{points.length} samples
      </p>
    </div>
  );
}
