// v2
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { EquityChart } from "../components/EquityChart";
import {
  api,
  BotState,
  EquitySnapshot,
  isConnectionError,
  MarketEvaluation,
  PerformanceSummary,
  PMPosition,
} from "../api";
import { formatDateTime, getDisplayTz, timeAgo } from "../lib/format";
import type { Page, SettingsTab } from "../App";

/**
 * Dashboard - SaaS-parity layout.
 *
 * Hero (balance + chart placeholder), then a 12-col grid:
 *   - Open positions (8 cols): top 5, expandable
 *   - Recent activity (4 cols): buys + skips + resolves
 *   - Risk today (4 cols): daily loss / drawdown / exposure gauges
 *   - Resolving soon (4 cols)
 *   - This week (4 cols): brier, win rate, settled count
 */

type Risk = {
  daily_loss_limit_pct?: number;
  drawdown_halt_pct?: number;
  dry_powder_reserve_pct?: number;
};

interface Props {
  state: BotState | null;
  refresh: () => void;
  goto: (p: Page, tab?: SettingsTab) => void;
}

type Tone = "gold" | "muted" | "teal" | "profit" | "ember";
type ActivityItem = {
  t: string;
  sortKey: string;
  kind: "execute" | "pass" | "update" | "resolve" | "resolve-loss" | "scan";
  text: string;
  meta: string;
  tone: Tone;
};

type ResolutionItem = {
  q: string;
  in: string;
  you: string;
  conviction: number;
};

type RiskItem = { used: number; cap: number; label: string };

const ACT_ICON: Record<ActivityItem["kind"], string> = {
  execute: "◆",
  pass: "–",
  update: "~",
  resolve: "✓",
  "resolve-loss": "✕",
  scan: "·",
};

// Activity-feed time formatter. Uses the user's display-tz via the
// central formatter so feed timestamps line up with the rest of the
// dashboard rather than always reading in the OS clock.
function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const tz = getDisplayTz();
  const opts: Intl.DateTimeFormatOptions = {
    hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  };
  return new Intl.DateTimeFormat(undefined, tz ? { ...opts, timeZone: tz } : opts).format(d);
}
function daysFromNow(iso: string | null | undefined): string {
  if (!iso) return "-";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "-";
  const ms = t - Date.now();
  // After endDate: Polymarket is in UMA settlement window (proposer
  // submits resolution → 2-12h dispute window → market closes →
  // funds released → bot's resolver settles the row). The position
  // is just waiting on the oracle here, not actively trading.
  if (ms <= 0) return "settling";
  const days = Math.round(ms / 86_400_000);
  if (days === 0) {
    const hours = Math.max(1, Math.round(ms / 3_600_000));
    return `${hours}h`;
  }
  return `${days}d`;
}
function numberOr(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

export default function Dashboard({ state, goto }: Props) {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [positions, setPositions] = useState<PMPosition[]>([]);
  const [evaluations, setEvaluations] = useState<MarketEvaluation[]>([]);
  const [risk, setRisk] = useState<Risk>({});
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  // Periodic (bankroll, open_cost, equity) snapshots recorded by the
  // daemon every ~10 min. When the curve has >=2 snapshots we plot
  // them directly so deposits show as real step-ups; under that
  // threshold we fall back to the legacy back-step reconstruction
  // (which retcons the past on deposit but is the best we can do
  // before the snapshot table has populated).
  const [equitySnapshots, setEquitySnapshots] = useState<EquitySnapshot[]>([]);

  const refresh = useCallback(async () => {
    try {
      const [s, p, ev, cfg, eh] = await Promise.all([
        api.summary(),
        // Match Performance's limit so the equity chart on both
        // pages renders the SAME line from the SAME data. With 50
        // the chart was missing older settled trades and the curve
        // ended below the actual realized P&L.
        api.positions(500).then((r) => r.positions),
        api.evaluations(25).then((r) => r.evaluations),
        api.config(),
        // Equity history is best-effort. A cold cache (first 30s
        // after boot) or a transient query failure should not nuke
        // the rest of the dashboard - swallow to an empty list and
        // let the legacy back-step path render the chart.
        api.equityHistory().then((r) => r.history).catch(() => []),
      ]);
      setSummary(s);
      setPositions(p);
      setEvaluations(ev);
      setEquitySnapshots(eh);
      setRisk({
        daily_loss_limit_pct: numberOr(cfg.daily_loss_limit_pct, 0.10),
        drawdown_halt_pct: numberOr(cfg.drawdown_halt_pct, 0.40),
        dry_powder_reserve_pct: numberOr(cfg.dry_powder_reserve_pct, 0.20),
      });
      setLoaded(true);
      // Only clear error after a confirmed successful refresh - same
      // anti-flash pattern as App.tsx's poll loop.
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15_000);
    return () => clearInterval(id);
  }, [refresh]);

  const open = useMemo(
    () => positions.filter((p) => p.status === "open"),
    [positions],
  );
  const settled = useMemo(
    () => positions.filter((p) =>
      p.status === "settled"
      || p.status === "invalid"
      || p.status === "closed",
    ),
    [positions],
  );

  // Equity time-series for the dashboard chart.
  //
  // PRIMARY SOURCE: equity_snapshots written by the daemon every
  // ~10 min. Each row carries the actual (bankroll, open_cost,
  // equity) triple captured at `ts`. Plotting them directly means a
  // wallet deposit shows up as a natural step-up on the day it
  // happened - we never retcon past values.
  //
  // FALLBACK: when the snapshot table has <2 rows (fresh install,
  // first 20 min of uptime) we reconstruct a curve from settled
  // positions: start at (current_equity - total_realized), step by
  // each realized_pnl_usd, land at current_equity. This path
  // silently retcons history on deposit but is the best we can do
  // before real snapshots accumulate. The curve transitions to the
  // accurate snapshot-based view as soon as the 2nd snapshot lands.
  //
  // Same logic mirrored in Performance.tsx.
  const equitySeries = useMemo(() => {
    if (equitySnapshots.length >= 2) {
      return equitySnapshots.map((s) => ({ ts: s.ts, v: s.equity }));
    }
    if (settled.length === 0) return [] as { ts: string; v: number }[];
    const sorted = [...settled].sort((a, b) =>
      ((a.settled_at ?? "") < (b.settled_at ?? "") ? -1 : 1),
    );
    const totalRealized = sorted.reduce(
      (s, r) => s + ((r.realized_pnl_usd as number | null | undefined) ?? 0),
      0,
    );
    const currentEquity = summary?.equity ?? summary?.starting_cash ?? 0;
    const start = currentEquity - totalRealized;
    // Starting-anchor timestamp: derive from the first settlement so
    // the leftmost point on the chart has a real date/time in the
    // hover tooltip. Backdate by 60s so the anchor visibly precedes
    // the first settlement on the X axis.
    const firstSettlement = sorted[0].settled_at as string | null | undefined;
    const anchorTs = firstSettlement
      ? new Date(new Date(firstSettlement).getTime() - 60_000).toISOString()
      : "";
    let cum = start;
    return [
      { ts: anchorTs, v: start },
      ...sorted.map((r) => {
        cum += (r.realized_pnl_usd as number | null | undefined) ?? 0;
        return { ts: (r.settled_at as string | null | undefined) ?? "", v: cum };
      }),
    ];
  }, [summary, settled, equitySnapshots]);

  const activity = useMemo(
    () => buildActivity(evaluations, open, settled),
    [evaluations, open, settled],
  );
  const resolving = useMemo(() => buildResolving(open), [open]);
  const riskGauges = useMemo(
    () => buildRisk(summary, open, risk),
    [summary, open, risk],
  );

  const mode = (state?.mode as "simulation" | "live") ?? "simulation";
  const bankroll = summary?.bankroll ?? summary?.starting_cash ?? 0;
  const starting = summary?.starting_cash ?? 0;
  // Headline P&L = realized + unrealized. Matches the semantics of
  // Polymarket's "All-Time Profit/Loss" tile, which always includes
  // the MTM gain/loss on currently-open positions. The summary
  // endpoint computes this server-side from `open_cost - bot_open_cost`
  // (= data-api MTM minus DB cost basis) + realized_pnl. Older
  // sidecars without total_pnl fall back to realized-only.
  const realizedOnly = summary?.realized_pnl ?? 0;
  const totalPnl     = summary?.total_pnl ?? null;
  const pnl    = totalPnl != null ? Number(totalPnl) : realizedOnly;
  const pnlPct = starting > 0 ? (pnl / starting) * 100 : 0;
  const winRate = summary?.win_rate ?? null;
  const closed = summary?.settled_total ?? 0;
  const brier = summary?.brier ?? null;

  // Source the totals from /api/summary, NOT from the limit-50
  // positions array. The positions endpoint is paginated (capped at
  // 50 by default for the dashboard) so any user with more than 50
  // total positions would see locked capital + open trades silently
  // truncated. summary aggregates over the full pm_positions table
  // server-side and always reflects ground truth.
  //
  // totalEquity is the authoritative value from the API.
  // lockedCapital is ALWAYS derived as equity - bankroll so that
  //   balance + lockedCapital == totalEquity (by construction).
  // This avoids the open_cost vs position_value mismatch: when the
  // MTM fetch on the sidecar fails, equity falls back to
  // bankroll + open_cost, so locked capital auto-reflects that too.
  const totalEquity = numberOr(
    summary?.equity,
    bankroll + numberOr(summary?.position_value, numberOr(summary?.open_cost, 0)),
  );
  const lockedCapital = totalEquity - bankroll;
  const openTrades = Math.round(numberOr(summary?.open_positions, open.length));

  // Skipped count: prefer the server-side aggregate (summary.skipped_total)
  // so the Dashboard tile and the Positions chips reconcile. The local
  // /api/evaluations feed is capped at ~25 rows and was producing a
  // misleadingly low count here. Fall back to the local filter only if
  // the sidecar predates the skipped_total field.
  const skippedTrades = useMemo(() => {
    if (summary?.skipped_total != null) return summary.skipped_total;
    return evaluations.filter((e) => {
      const r = (e.recommendation ?? "").toUpperCase();
      return r !== "BUY_YES" && r !== "YES" && r !== "BUY_NO" && r !== "NO";
    }).length;
  }, [summary?.skipped_total, evaluations]);

  return (
    <div className="dash">
      {error && !isConnectionError(error) && (
        <div className="error">{error}</div>
      )}

      {state?.idle_reason === "insufficient_bankroll" && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "10px 14px",
            // No bottom margin: `.dash-hero` already supplies its own
            // 32px top margin. Adding 14px here on top of that made
            // the banner → hero gap (46px) visibly larger than the
            // hero-balance → hero-chart gap (32px) on stacked
            // viewports. Letting dash-hero own the spacing keeps
            // both gaps at a consistent 32px.
            margin: 0,
            borderRadius: 8,
            background: "rgba(120, 180, 220, 0.06)",
            border: "1px solid rgba(120, 180, 220, 0.22)",
            color: "var(--vellum-90, #e8e6e1)",
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          <span
            aria-hidden="true"
            style={{ fontSize: 16, opacity: 0.8 }}
          >
            💤
          </span>
          <span>
            Delfi has paused. Your available cash is below the
            minimum needed to place a trade. Trading will resume
            automatically once funds free up (a position closes or
            you deposit more).
          </span>
        </div>
      )}

      <DashHero
        mode={mode}
        bankroll={bankroll}
        lockedCapital={lockedCapital}
        totalEquity={totalEquity}
        realizedPnl={pnl}
        realizedPct={pnlPct}
        winRate={winRate}
        closedTrades={closed}
        openTrades={openTrades}
        skippedTrades={skippedTrades}
        equitySeries={equitySeries}
        loaded={loaded}
      />

      <div className="dash-grid">
        <section className="dash-card card-positions">
          <CardHead
            title="Open positions"
            meta={
              openTrades === 0
                ? "0 active"
                : `${openTrades} active · $${lockedCapital.toFixed(0)} deployed`
            }
            onLink={() => goto("positions")}
          />
          {open.length === 0 ? (
            <Empty label={loaded ? "No open positions yet." : "Loading..."} />
          ) : (
            <PositionsTable positions={open.slice(0, 5)} />
          )}
        </section>

        <section className="dash-card card-activity">
          <CardHead title="Recent activity" />
          {activity.length === 0 ? (
            <Empty label={loaded ? "No activity yet, Delfi is scanning." : "Loading..."} />
          ) : (
            <ActivityFeed items={activity} />
          )}
        </section>

        <section className="dash-card card-risk">
          <CardHead
            title="Risk today"
            onLink={() => goto("risk")}
            linkLabel="Risk controls"
          />
          <RiskGauges risk={riskGauges} />
        </section>

        <section className="dash-card card-upcoming">
          <CardHead title="Resolving soon" />
          {resolving.length === 0 ? (
            <Empty label={loaded ? "No positions resolving soon." : "Loading..."} />
          ) : (
            <UpcomingList items={resolving} />
          )}
        </section>

        <section className="dash-card card-summary">
          <CardHead title="This week" onLink={() => goto("performance")} />
          <SummaryCard brier={brier} winRate={winRate} settled={closed} />
        </section>
      </div>
    </div>
  );
}

function DashHero({
  mode, bankroll, lockedCapital, totalEquity,
  realizedPnl, realizedPct, winRate,
  closedTrades, openTrades, skippedTrades,
  equitySeries, loaded,
}: {
  mode: string;
  bankroll: number;
  lockedCapital: number;
  totalEquity: number;
  realizedPnl: number;
  realizedPct: number;
  winRate: number | null;
  closedTrades: number;
  openTrades: number;
  skippedTrades: number;
  equitySeries: { ts: string; v: number }[];
  loaded: boolean;
}) {
  const isSim = mode === "simulation";
  const pnlSign = realizedPnl > 0 ? "+" : realizedPnl < 0 ? "-" : "";
  const pctSign = realizedPct > 0 ? "+" : realizedPct < 0 ? "-" : "";
  const pnlTone = !loaded ? "" : realizedPnl > 0 ? "profit" : realizedPnl < 0 ? "loss" : "";
  const fmtUsd = (n: number) =>
    `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return (
    <section className="dash-hero">
      <div className="hero-balance">
        <div className="hero-balance-head">
          <div className="hero-balance-label">Total equity</div>
          <div className={`hero-balance-mode ${isSim ? "sim" : "live"}`}>
            {isSim ? "Simulation" : "Live"}
          </div>
        </div>
        <div className="hero-balance-row">
          {/* Total equity is the headline. Largest number, leftmost
              position. This is what a user with multiple devices is
              checking — "how much wealth do I have on Polymarket?". */}
          <div className="hero-balance-cell">
            <div className="hero-balance-cell-value t-num">
              {loaded ? (
                <>
                  <span className="hero-balance-cur">$</span>
                  {totalEquity.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </>
              ) : "-"}
            </div>
          </div>
          <div className="hero-delta-div" />
          {/* Cash = spendable bankroll. Cash on hand the bot can
              deploy on the next scan. */}
          <div className="hero-balance-cell">
            <div className="hero-delta-label">Cash</div>
            <div className="hero-delta-val t-num">{loaded ? fmtUsd(bankroll) : "-"}</div>
          </div>
          <div className="hero-delta-div" />
          <div className="hero-balance-cell">
            <div className="hero-delta-label">Locked capital</div>
            <div className="hero-delta-val t-num">{loaded ? fmtUsd(lockedCapital) : "-"}</div>
          </div>
        </div>
        <div className="hero-deltas">
          <div className="hero-delta">
            <div className="hero-delta-label">P&amp;L</div>
            <div className={`hero-delta-val t-num ${pnlTone}`}>
              {loaded ? (
                <>
                  {pnlSign}${Math.abs(realizedPnl).toFixed(2)}{" "}
                  <span className="hero-delta-pct">
                    {pctSign}{Math.abs(realizedPct).toFixed(2)}%
                  </span>
                </>
              ) : "-"}
            </div>
          </div>
          <div className="hero-delta-div" />
          <div className="hero-delta">
            <div className="hero-delta-label">Win rate</div>
            <div className="hero-delta-val t-num">
              {loaded && winRate != null ? `${Math.round(winRate * 100)}%` : "-"}
            </div>
          </div>
          <div className="hero-delta-div" />
          <div className="hero-delta">
            <div className="hero-delta-label">Closed trades</div>
            <div className="hero-delta-val t-num">{loaded ? `${closedTrades}` : "-"}</div>
          </div>
          <div className="hero-delta-div" />
          <div className="hero-delta">
            <div className="hero-delta-label">Open trades</div>
            <div className="hero-delta-val t-num">{loaded ? `${openTrades}` : "-"}</div>
          </div>
          <div className="hero-delta-div" />
          <div className="hero-delta">
            <div className="hero-delta-label">Skipped trades</div>
            <div className="hero-delta-val t-num">{loaded ? `${skippedTrades}` : "-"}</div>
          </div>
        </div>
      </div>

      <div className="hero-chart">
        <div className="hero-chart-head">
          <div className="hero-chart-label">Equity history</div>
        </div>
        {equitySeries.length >= 2 ? (
          <EquityChart series={equitySeries} />
        ) : (
          <div className="hero-chart-placeholder">
            Daily snapshots will appear here as trades settle.
          </div>
        )}
      </div>
    </section>
  );
}

function CardHead({
  title, meta, onLink, live, linkLabel,
}: {
  title: string;
  meta?: string;
  onLink?: () => void;
  live?: boolean;
  linkLabel?: string;
}) {
  return (
    <div className="card-head">
      <div className="card-head-left">
        <h3 className="card-title">{title}</h3>
        {meta && (
          <span className="card-meta">
            {live && <span className="card-live-dot" />}
            {meta}
          </span>
        )}
      </div>
      {onLink && (
        <button type="button" className="card-head-link" onClick={onLink}>
          {linkLabel ?? "View all"} →
        </button>
      )}
    </div>
  );
}

function PositionsTable({ positions }: { positions: PMPosition[] }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <div className="pos-table">
      <div className="pos-row head">
        <div>Market</div>
        <div>Category</div>
        <div>Side</div>
        <div>Size</div>
        <div>M YES %</div>
        <div>D YES %</div>
        <div>D CONF</div>
        <div>Opened</div>
        <div>Closes</div>
        <div />
      </div>
      {positions.map((p) => {
        const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
        const mYesPct = Math.round(marketYes * 100);
        const cp = (p.delfi_probability as number | null | undefined) ?? null;
        const cf = (p.confidence as number | null | undefined) ?? null;
        const dYesPct = cp != null ? Math.round(cp * 100) : null;
        const dConfPct = cf != null ? Math.round(cf * 100) : null;
        const isOpen = expanded.has(p.id);
        const reasoning = ((p.reasoning as string | null | undefined) ?? "").trim();
        const slug = p.slug as string | null | undefined;
        const polyUrl = slug ? `https://polymarket.com/market/${slug}` : null;
        const closesAt = (p.expected_resolution_at as string | null | undefined) ?? null;
        const category = (p.category as string | null | undefined) ?? null;
        return (
          <React.Fragment key={p.id}>
            <div
              className={`pos-row expandable ${isOpen ? "expanded" : ""}`}
              onClick={() => toggle(p.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  toggle(p.id);
                }
              }}
            >
              <div className="pos-q">{p.question}</div>
              <div className="pos-cat">{category ?? "-"}</div>
              <div className={`pos-side ${p.side === "YES" ? "yes" : "no"}`}>{p.side}</div>
              <div className="pos-num t-num">${p.cost_usd.toFixed(0)}</div>
              <div className="pos-num t-num">{mYesPct}%</div>
              <div className="pos-num t-num">{dYesPct != null ? `${dYesPct}%` : "-"}</div>
              <div className="pos-num t-num">{dConfPct != null ? `${dConfPct}%` : "-"}</div>
              <div className="pos-num t-num">{timeAgo(p.created_at)}</div>
              <div className="pos-closes t-num">{daysFromNow(closesAt)}</div>
              <div className={`pos-chevron ${isOpen ? "open" : ""}`}>▸</div>
            </div>
            {isOpen && (
              <div className="pos-detail">
                <div className="pos-detail-grid">
                  <div>
                    <div className="pos-detail-kv-label">Opened</div>
                    <div className="pos-detail-kv-val">
                      {formatDateTime(p.created_at)}
                    </div>
                  </div>
                  <div>
                    <div className="pos-detail-kv-label">Closes</div>
                    <div className="pos-detail-kv-val">
                      {formatDateTime(closesAt)}
                    </div>
                  </div>
                  <div>
                    <div className="pos-detail-kv-label">Entry price</div>
                    <div className="pos-detail-kv-val">{p.entry_price.toFixed(3)}</div>
                  </div>
                  <div>
                    <div className="pos-detail-kv-label">Shares</div>
                    <div className="pos-detail-kv-val">{p.shares.toFixed(2)}</div>
                  </div>
                  <div>
                    <div className="pos-detail-kv-label">Cost</div>
                    <div className="pos-detail-kv-val">${p.cost_usd.toFixed(2)}</div>
                  </div>
                </div>
                <div className="pos-detail-reason">
                  <div className="pos-detail-reason-label">Delfi&apos;s reasoning</div>
                  {reasoning || "No reasoning recorded for this entry."}
                </div>
                {polyUrl && (
                  <a
                    className="pos-detail-link"
                    href={polyUrl}
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      // Tauri webview swallows target="_blank" - route
                      // through the opener plugin to launch the OS browser.
                      try { void openUrl(polyUrl); }
                      catch { window.open(polyUrl, "_blank", "noopener,noreferrer"); }
                    }}
                  >
                    View on Polymarket →
                  </a>
                )}
              </div>
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

function buildActivity(
  evals: MarketEvaluation[],
  open: PMPosition[],
  settled: PMPosition[],
): ActivityItem[] {
  const fromOpen: ActivityItem[] = open.slice(0, 8).map((p) => {
    const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
    const mYesPct = Math.round(marketYes * 100);
    const cp = (p.delfi_probability as number | null | undefined) ?? null;
    const cf = (p.confidence as number | null | undefined) ?? null;
    const dYesPct = cp != null ? `${Math.round(cp * 100)}%` : "-";
    const dConfPct = cf != null ? `${Math.round(cf * 100)}%` : "-";
    const cat = (p.category as string | null | undefined) ?? null;
    return {
      t: fmtTime(p.created_at),
      sortKey: p.created_at ?? "",
      kind: "execute",
      text: `Bought ${p.side} · ${p.question}`,
      meta: `$${p.cost_usd.toFixed(0)} · Market ${mYesPct}% · Delfi ${dYesPct} · Confidence ${dConfPct}${cat ? ` · ${cat}` : ""}`,
      tone: "gold",
    };
  });

  const fromEvals: ActivityItem[] = evals
    .filter((e) => {
      const rec = (e.recommendation ?? "").toUpperCase();
      return rec !== "YES" && rec !== "NO" && rec !== "BUY" && rec !== "BUY_YES" && rec !== "BUY_NO";
    })
    .slice(0, 8)
    .map((e) => {
      const dYesPct = e.delfi_probability != null ? `${Math.round(e.delfi_probability * 100)}%` : "-";
      const mYesPct = e.market_price_yes != null ? `${Math.round(e.market_price_yes * 100)}%` : "-";
      const dConfPct = e.confidence != null ? `${Math.round(e.confidence * 100)}%` : "-";
      return {
        t: fmtTime(e.evaluated_at),
        sortKey: e.evaluated_at ?? "",
        kind: "pass",
        text: `Skipped · ${e.question}`,
        meta: `Market ${mYesPct} · Delfi ${dYesPct} · Confidence ${dConfPct}${e.category ? ` · ${e.category}` : ""}`,
        tone: "muted",
      };
    });

  const fromSettled: ActivityItem[] = settled.slice(0, 6).map((s) => {
    const pnl = (s.realized_pnl_usd as number | null | undefined) ?? 0;
    const status = (s.status as string | null | undefined) ?? "settled";
    // Three states: VOID for market-side invalid resolutions
    // (refund, pnl=0), WIN for pnl > 0, LOSS for pnl < 0. The
    // earlier `pnl >= 0` boolean labelled invalids as WIN +$0.00.
    let label: string;
    let tone: "profit" | "ember" | "muted";
    let kind: ActivityItem["kind"];
    if (status === "invalid") {
      label = "VOID";
      tone = "muted";
      kind = "resolve";
    } else if (pnl > 0) {
      label = "WIN";
      tone = "profit";
      kind = "resolve";
    } else {
      label = "LOSS";
      tone = "ember";
      kind = "resolve-loss";
    }
    const meta =
      status === "invalid"
        ? "Refunded"
        : `${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`;
    return {
      t: fmtTime((s.settled_at as string | null | undefined) ?? null),
      sortKey: (s.settled_at as string | null | undefined) ?? "",
      kind,
      text: `Closed ${label} · ${s.question}`,
      meta,
      tone,
    };
  });

  return [...fromOpen, ...fromEvals, ...fromSettled]
    .sort((a, b) => (a.sortKey < b.sortKey ? 1 : -1))
    .slice(0, 10);
}

function ActivityFeed({ items }: { items: ActivityItem[] }) {
  return (
    <ul className="act-list">
      {items.map((a, i) => (
        <li className={`act-row tone-${a.tone}`} key={i}>
          <span className="act-time t-num">{a.t}</span>
          <span className="act-mark">{ACT_ICON[a.kind]}</span>
          <span className="act-body">
            <span className="act-text">{a.text}</span>
            <span className="act-meta">{a.meta}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function buildRisk(
  summary: PerformanceSummary | null,
  open: PMPosition[],
  config: Risk,
): { dailyLoss: RiskItem; drawdown: RiskItem; exposure: RiskItem } {
  const bankroll = summary?.bankroll ?? summary?.starting_cash ?? 0;
  const starting = summary?.starting_cash ?? bankroll ?? 0;
  // Pull exposure from summary.open_cost (server-side aggregate over
  // the full pm_positions table) instead of summing the limit-50
  // positions array. With >50 positions the array sum was silently
  // truncated and the gauge under-counted.
  const exposure = summary?.open_cost != null
    ? Number(summary.open_cost)
    : open.reduce((s, p) => s + (p.cost_usd ?? 0), 0);
  // Equity = cash + position MTM. Falls back to bankroll + exposure
  // (cost-basis equity) on older sidecars that don't return summary.equity.
  const totalEquity = summary?.equity != null
    ? Number(summary.equity)
    : bankroll + exposure;
  // Drawdown compares EQUITY to starting capital, not bankroll. With
  // bankroll the formula treated open positions as losses (e.g. cash
  // $5 + positions $25 vs $30 starting → bankroll formula read 83%
  // drawdown even though equity matched starting). User-visible bug
  // 2026-05-23: widget showed 76.3% on a profitable account.
  // risk_manager.evaluate uses the equity formula too; this matches.
  const ddPct = starting > 0 ? Math.max(0, ((starting - totalEquity) / starting) * 100) : 0;
  const dailyLoss = Math.max(0, -((summary?.realized_pnl ?? 0)));
  // Daily/weekly loss caps are denominated in starting capital, NOT
  // current bankroll — matches what risk_manager.evaluate does.
  const dailyCap = Math.max(1, starting * (config.daily_loss_limit_pct ?? 0.10));
  const ddCapPct = (config.drawdown_halt_pct ?? 0.40) * 100;
  const exposureCap = Math.max(1, totalEquity);
  return {
    dailyLoss: { used: Math.round(dailyLoss), cap: Math.round(dailyCap), label: "Daily loss limit" },
    drawdown:  { used: +ddPct.toFixed(1), cap: +ddCapPct.toFixed(0), label: "Maximum drawdown" },
    exposure:  { used: Math.round(exposure), cap: Math.round(exposureCap), label: "Capital deployed" },
  };
}

function RiskGauges({ risk }: { risk: { dailyLoss: RiskItem; drawdown: RiskItem; exposure: RiskItem } }) {
  const items: { key: string; unit: "$" | "%"; item: RiskItem }[] = [
    { key: "dailyLoss", unit: "$", item: risk.dailyLoss },
    { key: "drawdown",  unit: "%", item: risk.drawdown },
    { key: "exposure",  unit: "$", item: risk.exposure },
  ];
  return (
    <div className="risk-list">
      {items.map(({ key, unit, item }) => {
        const cap = item.cap || 1;
        const pct = Math.min(100, (item.used / cap) * 100);
        const tone = pct > 75 ? "hot" : pct > 50 ? "warm" : "ok";
        return (
          <div className="risk-row" key={key}>
            <div className="risk-top">
              <span className="risk-label">{item.label}</span>
              <span className={`risk-val t-num tone-${tone}`}>
                {unit === "$" ? "$" : ""}
                {item.used.toLocaleString()}
                {unit === "%" ? "%" : ""}
                <span className="risk-of">
                  &nbsp;/ {unit === "$" ? "$" : ""}
                  {item.cap.toLocaleString()}
                  {unit === "%" ? "%" : ""}
                </span>
              </span>
            </div>
            <div className="risk-bar">
              <div className={`risk-bar-fill tone-${tone}`} style={{ width: `${pct}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function buildResolving(open: PMPosition[]): ResolutionItem[] {
  return open
    .filter((p) => p.expected_resolution_at)
    .sort((a, b) => {
      const ta = new Date((a.expected_resolution_at as string)!).getTime();
      const tb = new Date((b.expected_resolution_at as string)!).getTime();
      return ta - tb;
    })
    .slice(0, 4)
    .map((p) => {
      const cp = (p.delfi_probability as number | null | undefined) ?? null;
      const cf = (p.confidence as number | null | undefined) ?? null;
      const pct = cp != null ? Math.round(cp * 100) : null;
      return {
        q: p.question,
        in: daysFromNow((p.expected_resolution_at as string | null | undefined) ?? null),
        you: pct != null ? `${p.side} ${pct}%` : p.side,
        conviction: cf ?? 0,
      };
    });
}

function UpcomingList({ items }: { items: ResolutionItem[] }) {
  return (
    <ul className="up-list">
      {items.map((r, i) => (
        <li className="up-row" key={i}>
          <div className="up-q">{r.q}</div>
          <div className="up-meta">
            <span className="up-metric">
              <span className="up-metric-label">Closes in</span>
              <span className="up-in t-num">{r.in}</span>
            </span>
            <span className="up-metric">
              <span className="up-metric-label">Side</span>
              <span className="up-you">{r.you}</span>
            </span>
            {r.conviction > 0 && (
              <span className="up-metric">
                <span className="up-metric-label">Confidence</span>
                <span className="up-conv">
                  <span className="up-conv-bar">
                    <span style={{ width: `${r.conviction * 100}%` }} />
                  </span>
                  <span className="up-conv-pct t-num">{Math.round(r.conviction * 100)}%</span>
                </span>
              </span>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function SummaryCard({
  brier, winRate, settled,
}: {
  brier: number | null;
  winRate: number | null;
  settled: number;
}) {
  const headline =
    settled === 0
      ? "No settled trades yet."
      : `${settled} trades settled${winRate != null ? ` · ${Math.round(winRate * 100)}% win rate` : ""}.`;
  const body =
    settled === 0
      ? "Delfi is scanning markets and will open positions when forecasts agree with the market favourite."
      : "Live performance across all settled markets. Open Performance for category and calibration breakdowns.";
  return (
    <div className="sum">
      <div className="sum-head">{headline}</div>
      <p className="sum-body">{body}</p>
      <div className="sum-stats">
        <div className="sum-stat">
          <div className="sum-stat-num t-num">{brier != null ? brier.toFixed(3) : "-"}</div>
          <div className="sum-stat-label">Brier score</div>
        </div>
        <div className="sum-stat">
          <div className="sum-stat-num t-num">
            {winRate != null ? `${Math.round(winRate * 100)}%` : "-"}
          </div>
          <div className="sum-stat-label">Win rate</div>
        </div>
        <div className="sum-stat">
          <div className="sum-stat-num t-num">{settled}</div>
          <div className="sum-stat-label">Trades</div>
        </div>
      </div>
    </div>
  );
}

function Empty({ label }: { label: string }) {
  return <div className="empty-state" style={{ padding: "32px 16px" }}>{label}</div>;
}

// (Equity chart now lives in src/components/EquityChart.tsx and is
//  shared with the Performance page so both views render identical
//  hover behaviour and tick math.)
