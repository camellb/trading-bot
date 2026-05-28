import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  BrierTrendPoint,
  CalibrationReport,
  EquitySnapshot,
  isConnectionError,
  PerformanceSummary,
  PMPosition,
} from "../api";
import { EquityChart } from "../components/EquityChart";
import { SortableTh, SortKey, useSort } from "../components/SortableTh";
import { archetypeLabel } from "../lib/archetypes";

/**
 * Performance - SaaS-parity layout.
 *
 * page-wrap with title + range chips, then:
 *   - stat-row: Bankroll, Realized P&L, Win rate, Brier (4 tiles)
 *   - Equity chart (SVG, reconstructed client-side from settled positions)
 *   - Brier trend sparkline
 *   - By category / by horizon / by archetype / by price-band tables
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

// archetypeLabel + ARCHETYPE_LABELS now live in `lib/archetypes` so the
// same map drives Performance, Intelligence, and (via /api/archetypes)
// Risk Control. See lib/archetypes.ts for the canonical source.

export default function Performance() {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [trend, setTrend] = useState<BrierTrendPoint[]>([]);
  const [calibration, setCalibration] = useState<CalibrationReport | null>(null);
  const [closed, setClosed] = useState<PMPosition[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState<Range>("all");
  const [loaded, setLoaded] = useState(false);
  // Periodic (bankroll, open_cost, equity) snapshots recorded by the
  // daemon every ~10 min. Same source the Dashboard's chart uses.
  const [equitySnapshots, setEquitySnapshots] = useState<EquitySnapshot[]>([]);

  const refresh = useCallback(async () => {
    try {
      const [s, t, c, p, eh] = await Promise.all([
        api.summary(),
        api.brierTrend().then((x) => x.points),
        api.calibration({ source: "polymarket" }),
        api.positions(500).then((r) =>
          r.positions
            // Include every trade the bot entered that has resolved:
            //   settled       -> market reached natural YES/NO
            //   closed_early  -> exit-policy sold before resolution
            //   invalid       -> market voided, Polymarket refunded
            // closed_early was previously missing here, which made
            // Performance count fewer losses than the server's
            // settled_total. Adding it is what finally makes the
            // Dashboard win-rate (server side) and the Performance
            // win-rate (this page) agree on a single number.
            .filter((x) =>
              x.status === "settled" ||
              x.status === "closed_early" ||
              x.status === "invalid",
            )
            .sort((a, b) => ((a.settled_at ?? "") < (b.settled_at ?? "") ? -1 : 1)),
        ),
        // Best-effort: an empty / failed equity_history just means
        // the chart falls back to the legacy back-step reconstruction
        // for one tick.
        api.equityHistory().then((r) => r.history).catch(() => []),
      ]);
      setSummary(s);
      setTrend(t);
      setCalibration(c);
      setClosed(p);
      setEquitySnapshots(eh);
      // Same data_ready gating as Dashboard: hide values until the
      // server says the wallet probe has warmed up. See Dashboard.tsx
      // for the long-form rationale.
      setLoaded(s?.data_ready !== false);
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
      // Win/loss determined by realized P&L sign - matches the
      // server's pm_executor.get_portfolio_stats() so the Dashboard
      // and Performance page agree on a single win-rate number.
      // Break-even trades (pnl === 0) and invalid/voided markets
      // (refunded at the entry price, so pnl ~= 0) count toward
      // `trades` for the closed-trade total but are excluded from
      // both numerator and denominator of the win rate.
      if (pnl > 0) wins++;
      else if (pnl < 0) losses++;
      totalPnl += pnl;
      totalCost += r.cost_usd ?? 0;
    }
    const settled = wins + losses;
    const winRate = settled > 0 ? (wins / settled) * 100 : 0;
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
    // Same strategy as Dashboard.tsx: full historical back-step
    // from the first settled trade up to the first snapshot, then
    // real snapshot points afterward. Joined smoothly at the first
    // snapshot's ts. See Dashboard.tsx for the long-form rationale.
    //
    // For 30d / 7d windowed ranges we restrict BOTH the back-step
    // input (already pre-filtered into `filtered`) AND the snapshot
    // pool to the same time window so the chart stays scoped.
    if (filtered.length === 0 && equitySnapshots.length === 0) {
      return [] as { ts: string; v: number }[];
    }

    let snapshotPool = equitySnapshots;
    if (range !== "all") {
      const days = RANGE_DAYS[range];
      const cutoffMs = Date.now() - days * 86_400_000;
      snapshotPool = snapshotPool.filter((s) => {
        const t = Date.parse(s.ts);
        return Number.isFinite(t) && t >= cutoffMs;
      });
    }

    // Anchor: first snapshot in the window, else current equity
    // (all-time) or starting_cash (windowed).
    let anchorTs: string;
    let anchorEquity: number;
    let beforeAnchor: typeof filtered;
    if (snapshotPool.length > 0) {
      const firstSnap = snapshotPool[0];
      anchorTs = firstSnap.ts;
      anchorEquity = firstSnap.equity;
      beforeAnchor = filtered.filter((r) =>
        (r.settled_at ?? "") < anchorTs,
      );
    } else {
      anchorTs = "";
      anchorEquity = range === "all"
        ? (summary?.equity ?? summary?.starting_cash ?? 0)
        : (summary?.starting_cash ?? 0);
      beforeAnchor = filtered;
    }

    const realizedBefore = beforeAnchor.reduce(
      (s, r) => s + ((r.realized_pnl_usd as number | null | undefined) ?? 0),
      0,
    );
    const startEquity = anchorEquity - realizedBefore;
    const historical: { ts: string; v: number }[] = [];
    if (beforeAnchor.length > 0) {
      const firstTs = beforeAnchor[0].settled_at as string | null | undefined;
      const startTs = firstTs
        ? new Date(new Date(firstTs).getTime() - 60_000).toISOString()
        : "";
      historical.push({ ts: startTs, v: startEquity });
      let cum = startEquity;
      for (const r of beforeAnchor) {
        cum += (r.realized_pnl_usd as number | null | undefined) ?? 0;
        historical.push({ ts: r.settled_at ?? "", v: cum });
      }
    }

    const snapshotPoints = snapshotPool.map((s) => ({
      ts: s.ts, v: s.equity,
    }));

    return [...historical, ...snapshotPoints];
  }, [summary, filtered, range, equitySnapshots]);

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
          {/* Mirrors the Overview dashboard's "Total equity" tile so
              both pages show the same headline number for the same
              metric. Source: summary.equity from /api/summary
              (Polymarket-truth: wallet cash + currentValue of all
              open positions, refreshed every 60s by the
              pm_balance_refresh scheduler). */}
          <div className="stat-cell-label">Total equity</div>
          <div className="stat-cell-val t-num">
            {summary && summary.equity != null
              ? fmtMoney(summary.equity)
              : "-"}
          </div>
        </div>
        <div className="stat-cell">
          {/* "Total P&L" mirrors the Overview dashboard's P&L tile
              EXACTLY: same metric (realized + unrealized open MTM),
              same source (summary.total_pnl), same ROI denominator
              (summary.starting_cash). Previously this tile was
              labeled "Realized P&L" with just summary.realized_pnl,
              which showed $12.96 here vs $17.09 on Dashboard for
              the same wallet - same-ish label, different number,
              and the user (2026-05-24) called it inconsistent.
              For 30/7-day ranges we fall back to the DB-filtered
              sum since Polymarket doesn't expose a time-windowed
              total. */}
          <div className="stat-cell-label">Total P&amp;L</div>
          {(() => {
            const useApi = range === "all"
              && summary
              && summary.total_pnl != null;
            const value = useApi
              ? (summary!.total_pnl as number)
              : filteredStats.totalPnl;
            const denom = summary?.starting_cash ?? 0;
            const roi = denom > 0 ? (value / denom) * 100 : 0;
            return (
              <>
                <div className={`stat-cell-val t-num ${value > 0 ? "profit" : value < 0 ? "ember" : ""}`}>
                  {loaded ? fmtMoney(value) : "-"}
                </div>
                <div className={`stat-cell-delta ${roi < 0 ? "down" : ""}`}>
                  {loaded && denom > 0 ? `${fmtPct(roi)} ROI` : ""}
                </div>
              </>
            );
          })()}
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
        {/* YES-bias tile was here (v1.5.20-v1.5.23). Removed 2026-05-28
            per user: not a metric the user cares about. Backend kept
            live (calibration.get_yes_bias_report + /api/summary
            yes_bias field) so it can be re-surfaced later if needed. */}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Equity history</h2>
        </div>
        {equitySeries.length < 2 ? (
          <div className="empty-state">
            {loaded ? "No equity history yet. The curve fills in as the daemon records snapshots (every ~10 min) and Delfi settles its first trades." : "Loading..."}
          </div>
        ) : (
          <EquityChart series={equitySeries} />
        )}
      </div>

      {trend.length > 1 && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Brier trend</h2>
          </div>
          {/* Same interactive chart primitive as the equity history,
              just configured for Brier semantics: gold stroke,
              3-decimal precision in the hover tooltip, integer-
              %-style ticks on the axis, lower = better so green/red
              colour logic is inverted, and a tiny minSpan since
              Brier values live inside [0, 1]. */}
          <EquityChart
            series={trend
              .filter((p) => p.date)
              .map((p) => ({ ts: p.date as string, v: p.brier }))}
            strokeColor="var(--gold)"
            fillColor="rgba(212,175,55,0.08)"
            lowerIsBetter
            minSpan={0.05}
            formatValue={(n) => n.toFixed(3)}
            formatValueAxis={(n) => n.toFixed(2)}
          />
        </div>
      )}

      <ArchetypeTable calibration={calibration} />
      <CategoryTable calibration={calibration} />
      <HorizonTable calibration={calibration} />
      <PriceBandTable calibration={calibration} />
    </div>
  );
}

// ── By price band ───────────────────────────────────────────────────────

type PriceBandSk = "bucket" | "trades" | "win_rate" | "pnl" | "roi" | "brier";

function PriceBandTable({ calibration }: { calibration: CalibrationReport | null }) {
  const sort = useSort<PriceBandSk>("trades", "desc");
  const rows = useMemo(() => {
    const raw = calibration?.by_price_band ?? [];
    return sort.apply(raw, (b, f): SortKey => {
      const wins = b.wins ?? 0;
      const cost = b.cost_usd ?? 0;
      const pnl  = b.pnl_usd ?? 0;
      switch (f) {
        case "bucket":   return b.bucket;
        case "trades":   return b.n;
        case "win_rate": return b.n > 0 ? wins / b.n : null;
        case "pnl":      return b.n > 0 ? pnl : null;
        case "roi":      return cost > 0 ? pnl / cost : null;
        case "brier":    return b.brier;
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calibration?.by_price_band, sort.field, sort.dir]);

  if (!calibration || !calibration.by_price_band || calibration.by_price_band.length === 0) return null;
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">By price band</h2>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        How each market-price band has actually performed. 0-50 means
        the market favoured NO; 50-100 means YES. Bands with weak win
        rate or negative ROI are candidates for skipping in the Risk
        page Price band filter.
      </p>
      <table className="table-simple">
        <thead>
          <tr>
            <SortableTh field="bucket"   sort={sort}>Band</SortableTh>
            <SortableTh field="trades"   sort={sort}>Trades</SortableTh>
            <SortableTh field="win_rate" sort={sort}>Win rate</SortableTh>
            <SortableTh field="pnl"      sort={sort}>P&amp;L</SortableTh>
            <SortableTh field="roi"      sort={sort}>ROI</SortableTh>
            <SortableTh field="brier"    sort={sort}>Brier</SortableTh>
          </tr>
        </thead>
        <tbody>
          {rows.map((b, i) => {
            const pnl = b.pnl_usd ?? 0;
            const cost = b.cost_usd ?? 0;
            const wins = b.wins ?? 0;
            const winRate = b.n > 0 ? (wins / b.n) * 100 : null;
            const roi = cost > 0 ? (pnl / cost) * 100 : null;
            const pnlClass = pnl > 0 ? "cell-up" : pnl < 0 ? "cell-down" : "";
            const roiClass = roi != null && roi > 0 ? "cell-up" : roi != null && roi < 0 ? "cell-down" : "";
            return (
              <tr key={i}>
                <td>{b.bucket}</td>
                <td className="mono">{b.n}</td>
                <td className="mono">{winRate != null ? `${winRate.toFixed(0)}%` : "-"}</td>
                <td className={`mono ${pnlClass}`}>{b.n > 0 ? fmtSignedPnl(pnl) : "-"}</td>
                <td className={`mono ${roiClass}`}>{roi != null ? fmtPct(roi) : "-"}</td>
                <td className="mono">{b.brier?.toFixed(3) ?? "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
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
          No settled trades yet.
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
          No settled trades yet.
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

// BrierSpark removed 2026-05-25: replaced by the shared EquityChart
// (now generalised with formatter + colour props) so Brier trend is
// fully interactive with the same hover-tooltip behaviour as equity.

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
