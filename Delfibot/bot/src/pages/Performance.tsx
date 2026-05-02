import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  BrierTrendPoint,
  CalibrationReport,
  isConnectionError,
  PerformanceSummary,
  PMPosition,
} from "../api";
import { EquityChart } from "../components/EquityChart";
import { SortableTh, SortKey, useSort } from "../components/SortableTh";

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
function fmtSignedPnl(v: number): string {
  const sign = v >= 0 ? "+" : "-";
  const abs = Math.abs(v);
  if (abs >= 1000) return `${sign}$${abs.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  return `${sign}$${abs.toFixed(2)}`;
}

/**
 * Friendly label per archetype id. Mirrors `_ARCHETYPE_INFO` in
 * `bot/local_api.py` so the Performance page can render the same name
 * the user sees in Settings -> Risk and sizing without a second API
 * round-trip. If the id isn't in the map (e.g. a freshly added
 * archetype that hasn't been documented here yet), we humanize the id
 * itself by replacing underscores and title-casing.
 */
const ARCHETYPE_LABELS: Record<string, string> = {
  tennis:              "Tennis",
  basketball:          "Basketball",
  baseball:            "Baseball",
  football:            "Football",
  hockey:              "Hockey",
  cricket:             "Cricket",
  esports:             "Esports",
  soccer:              "Soccer",
  sports_other:        "Other sports",
  price_threshold:     "Price threshold",
  activity_count:      "Activity count",
  geopolitical_event:  "Geopolitical event",
  binary_event:        "Other event",
};
function archetypeLabel(id: string | null): string {
  if (!id) return "Unknown";
  if (ARCHETYPE_LABELS[id]) return ARCHETYPE_LABELS[id];
  return id
    .split("_")
    .map((w) => (w.length === 0 ? w : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
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
      const [s, t, c, p] = await Promise.all([
        api.summary(),
        api.brierTrend().then((x) => x.points),
        api.calibration({ source: "polymarket" }),
        api.positions(500).then((r) =>
          r.positions
            // Include 'invalid' alongside 'settled' - both are
            // closed trades the bot actually entered. Excluding
            // invalid (markets that resolved ambiguously, settled at
            // 0.50) made the Performance page disagree with the
            // dashboard's summary numbers.
            .filter((x) => x.status === "settled" || x.status === "invalid")
            .sort((a, b) => ((a.settled_at ?? "") < (b.settled_at ?? "") ? -1 : 1)),
        ),
      ]);
      setSummary(s);
      setTrend(t);
      setCalibration(c);
      setClosed(p);
      setLoaded(true);
      // Clear error only on confirmed success (anti-flash pattern,
      // see App.tsx::refresh).
      setError(null);
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
      // Wins/losses count clear YES/NO outcomes. Invalid markets
      // (outcome neither matches nor mirrors `side` because they
      // settled at 0.50) are trades but neither wins nor losses.
      if (outcome === r.side) wins++;
      else if (outcome === "YES" || outcome === "NO") losses++;
      totalPnl += pnl;
      totalCost += r.cost_usd ?? 0;
    }
    const winRate = trades > 0 ? (wins / trades) * 100 : 0;
    // ROI on BANKROLL, not on cost - matches CLAUDE.md doctrine
    // ("Maximize ROI on bankroll across all trades") and the
    // dashboard's `pnl/starting * 100` calculation. The cost-based
    // ROI used to display +21% while the dashboard showed +2.24%
    // for the same trades, which was confusing.
    const starting = summary?.starting_cash ?? 0;
    const roi = starting > 0 ? (totalPnl / starting) * 100 : 0;
    return { trades, wins, losses, totalPnl, totalCost, winRate, roi };
  }, [filtered, summary]);

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
            <ExportCsvButton />
          </div>
        </div>
      </div>

      {error && !isConnectionError(error) && (
        <div className="error">{error}</div>
      )}

      <div className="stat-row">
        <div className="stat-cell">
          <div className="stat-cell-label">Capital</div>
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

      <ArchetypeTable calibration={calibration} />
      <CategoryTable calibration={calibration} />
      <HorizonTable calibration={calibration} />
    </div>
  );
}

// ── By archetype ────────────────────────────────────────────────────────

type ArchetypeSk = "archetype" | "trades" | "win_rate" | "pnl" | "roi" | "brier";

function ArchetypeTable({ calibration }: { calibration: CalibrationReport | null }) {
  const sort = useSort<ArchetypeSk>("trades", "desc");
  const rows = useMemo(() => {
    const raw = calibration?.by_archetype ?? [];
    return sort.apply(raw, (a, f): SortKey => {
      const wins = a.wins ?? 0;
      const cost = a.cost_usd ?? 0;
      const pnl  = a.pnl_usd ?? 0;
      switch (f) {
        case "archetype": return archetypeLabel(a.archetype);
        case "trades":    return a.n;
        case "win_rate":  return a.n > 0 ? wins / a.n : null;
        case "pnl":       return a.n > 0 ? pnl : null;
        case "roi":       return cost > 0 ? pnl / cost : null;
        case "brier":     return a.brier;
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calibration?.by_archetype, sort.field, sort.dir]);

  if (!calibration) return null;
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">By archetype</h2>
        <span className="panel-meta">{calibration.by_archetype?.length ?? 0} archetypes</span>
      </div>
      {rows.length > 0 ? (
        <table className="table-simple">
          <thead>
            <tr>
              <SortableTh field="archetype" sort={sort}>Archetype</SortableTh>
              <SortableTh field="trades"    sort={sort}>Trades</SortableTh>
              <SortableTh field="win_rate"  sort={sort}>Win rate</SortableTh>
              <SortableTh field="pnl"       sort={sort}>P&amp;L</SortableTh>
              <SortableTh field="roi"       sort={sort}>ROI</SortableTh>
              <SortableTh field="brier"     sort={sort}>Brier</SortableTh>
            </tr>
          </thead>
          <tbody>
            {rows.map((a, i) => {
              const pnl = a.pnl_usd ?? 0;
              const cost = a.cost_usd ?? 0;
              const wins = a.wins ?? 0;
              const winRate = a.n > 0 ? (wins / a.n) * 100 : null;
              const roi = cost > 0 ? (pnl / cost) * 100 : null;
              const pnlClass = pnl > 0 ? "cell-up" : pnl < 0 ? "cell-down" : "";
              const roiClass = roi != null && roi > 0 ? "cell-up" : roi != null && roi < 0 ? "cell-down" : "";
              return (
                <tr key={i}>
                  <td>{archetypeLabel(a.archetype)}</td>
                  <td className="mono">{a.n}</td>
                  <td className="mono">{winRate != null ? `${winRate.toFixed(0)}%` : "-"}</td>
                  <td className={`mono ${pnlClass}`}>{a.n > 0 ? fmtSignedPnl(pnl) : "-"}</td>
                  <td className={`mono ${roiClass}`}>{roi != null ? fmtPct(roi) : "-"}</td>
                  <td className="mono">{a.brier?.toFixed(3) ?? "-"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <div className="empty-state">
          No settled trades yet. Per-archetype P&amp;L, ROI, and win rate
          appear here once Delfi opens and resolves positions.
        </div>
      )}
    </div>
  );
}

// ── By category ─────────────────────────────────────────────────────────

type CategorySk = "category" | "trades" | "win_rate" | "pnl" | "roi" | "brier";

function CategoryTable({ calibration }: { calibration: CalibrationReport | null }) {
  const sort = useSort<CategorySk>("trades", "desc");
  const rows = useMemo(() => {
    const raw = calibration?.by_category ?? [];
    return sort.apply(raw, (c, f): SortKey => {
      const wins = c.wins ?? 0;
      const cost = c.cost_usd ?? 0;
      const pnl  = c.pnl_usd ?? 0;
      switch (f) {
        case "category": return c.category ?? "";
        case "trades":   return c.n;
        case "win_rate": return c.n > 0 ? wins / c.n : (c.win_rate ?? null);
        case "pnl":      return c.n > 0 ? pnl : null;
        case "roi":      return cost > 0 ? pnl / cost : null;
        case "brier":    return c.brier;
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calibration?.by_category, sort.field, sort.dir]);

  if (!calibration) return null;
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">By category</h2>
        <span className="panel-meta">{calibration.by_category.length} categories</span>
      </div>
      {rows.length > 0 ? (
        <table className="table-simple">
          <thead>
            <tr>
              <SortableTh field="category" sort={sort}>Category</SortableTh>
              <SortableTh field="trades"   sort={sort}>Trades</SortableTh>
              <SortableTh field="win_rate" sort={sort}>Win rate</SortableTh>
              <SortableTh field="pnl"      sort={sort}>P&amp;L</SortableTh>
              <SortableTh field="roi"      sort={sort}>ROI</SortableTh>
              <SortableTh field="brier"    sort={sort}>Brier</SortableTh>
            </tr>
          </thead>
          <tbody>
            {rows.map((c, i) => {
              const pnl = c.pnl_usd ?? 0;
              const cost = c.cost_usd ?? 0;
              const wins = c.wins ?? 0;
              const winRate = wins > 0 || c.win_rate == null
                ? (c.n > 0 ? (wins / c.n) * 100 : null)
                : (c.win_rate ?? 0) * 100;
              const roi = cost > 0 ? (pnl / cost) * 100 : null;
              const pnlClass = pnl > 0 ? "cell-up" : pnl < 0 ? "cell-down" : "";
              const roiClass = roi != null && roi > 0 ? "cell-up" : roi != null && roi < 0 ? "cell-down" : "";
              return (
                <tr key={i}>
                  <td>{c.category ?? "Uncategorised"}</td>
                  <td className="mono">{c.n}</td>
                  <td className="mono">{winRate != null ? `${winRate.toFixed(0)}%` : "-"}</td>
                  <td className={`mono ${pnlClass}`}>{c.n > 0 ? fmtSignedPnl(pnl) : "-"}</td>
                  <td className={`mono ${roiClass}`}>{roi != null ? fmtPct(roi) : "-"}</td>
                  <td className="mono">{c.brier?.toFixed(3) ?? "-"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <div className="empty-state">
          No settled trades yet. Per-category breakdown appears here once
          Delfi opens and resolves positions.
        </div>
      )}
    </div>
  );
}

// ── By horizon ──────────────────────────────────────────────────────────

type HorizonSk = "bucket" | "trades" | "win_rate" | "pnl" | "roi" | "brier";

function HorizonTable({ calibration }: { calibration: CalibrationReport | null }) {
  const sort = useSort<HorizonSk>("trades", "desc");
  const rows = useMemo(() => {
    const raw = calibration?.by_horizon ?? [];
    return sort.apply(raw, (h, f): SortKey => {
      const wins = h.wins ?? 0;
      const cost = h.cost_usd ?? 0;
      const pnl  = h.pnl_usd ?? 0;
      switch (f) {
        case "bucket":   return h.bucket;
        case "trades":   return h.n;
        case "win_rate": return h.n > 0 ? wins / h.n : null;
        case "pnl":      return h.n > 0 ? pnl : null;
        case "roi":      return cost > 0 ? pnl / cost : null;
        case "brier":    return h.brier;
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calibration?.by_horizon, sort.field, sort.dir]);

  if (!calibration || calibration.by_horizon.length === 0) return null;
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">By horizon</h2>
        <span className="panel-meta">Resolution time bucket</span>
      </div>
      <table className="table-simple">
        <thead>
          <tr>
            <SortableTh field="bucket"   sort={sort}>Bucket</SortableTh>
            <SortableTh field="trades"   sort={sort}>Trades</SortableTh>
            <SortableTh field="win_rate" sort={sort}>Win rate</SortableTh>
            <SortableTh field="pnl"      sort={sort}>P&amp;L</SortableTh>
            <SortableTh field="roi"      sort={sort}>ROI</SortableTh>
            <SortableTh field="brier"    sort={sort}>Brier</SortableTh>
          </tr>
        </thead>
        <tbody>
          {rows.map((h, i) => {
            const pnl = h.pnl_usd ?? 0;
            const cost = h.cost_usd ?? 0;
            const wins = h.wins ?? 0;
            const winRate = h.n > 0 ? (wins / h.n) * 100 : null;
            const roi = cost > 0 ? (pnl / cost) * 100 : null;
            const pnlClass = pnl > 0 ? "cell-up" : pnl < 0 ? "cell-down" : "";
            const roiClass = roi != null && roi > 0 ? "cell-up" : roi != null && roi < 0 ? "cell-down" : "";
            return (
              <tr key={i}>
                <td>{h.bucket}</td>
                <td className="mono">{h.n}</td>
                <td className="mono">{winRate != null ? `${winRate.toFixed(0)}%` : "-"}</td>
                <td className={`mono ${pnlClass}`}>{h.n > 0 ? fmtSignedPnl(pnl) : "-"}</td>
                <td className={`mono ${roiClass}`}>{roi != null ? fmtPct(roi) : "-"}</td>
                <td className="mono">{h.brier?.toFixed(3) ?? "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// (Equity chart now lives in src/components/EquityChart.tsx and is
//  shared with the Dashboard so both views render identical hover
//  behaviour and tick math.)

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

// ── Export CSV ───────────────────────────────────────────────────────────

/**
 * Tiny button that downloads every position the bot has ever opened
 * as a CSV via the daemon's /api/positions/csv endpoint.
 *
 * We fetch through the Tauri webview rather than via a raw <a href>
 * so the request goes through the api.ts auto-retry-on-stale-port
 * path. A bare anchor would silently fail if the cached daemon port
 * was stale (every daemon respawn picks a fresh random port and a
 * page that's been open across a respawn would otherwise click into
 * a connection-refused).
 */
function ExportCsvButton() {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const onClick = async () => {
    setBusy(true);
    setErr(null);
    try {
      const blob = await api.positionsCsvBlob();
      const url = URL.createObjectURL(blob);
      try {
        const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
        const a = document.createElement("a");
        a.href = url;
        a.download = `delfi-trades-${ts}.csv`;
        a.style.display = "none";
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        // Free the Blob URL after a tick so the click had time to
        // start the download.
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button
        type="button"
        className="chip"
        onClick={onClick}
        disabled={busy}
        title="Export every position to CSV"
      >
        {busy ? "Exporting..." : "Export CSV"}
      </button>
      {err && (
        <span className="form-error" style={{ marginLeft: 8 }}>
          Export failed: {err}
        </span>
      )}
    </>
  );
}
