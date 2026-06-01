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
import { SortableTh, SortKey, useSort } from "../components/SortableTh";
import type { Page, SettingsTab } from "../App";

/**
 * Dashboard - lean two-card layout.
 *
 * Hero (balance + equity chart) on top, then two cards:
 *   - Recent activity: buys + skips + resolves feed
 *   - Open positions: same table + expand mechanic as the Positions tab
 *
 * The earlier mosaic (Risk today / Resolving soon / This week) was
 * removed 2026-05-28 - the hero already shows P&L / win rate /
 * trade counts, Risk page has the gauges, Positions tab shows
 * upcoming closes inline.
 */

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
      const [s, p, ev, eh] = await Promise.all([
        api.summary(),
        // Match Performance's limit so the equity chart on both
        // pages renders the SAME line from the SAME data. With 50
        // the chart was missing older settled trades and the curve
        // ended below the actual realized P&L.
        api.positions(500).then((r) => r.positions),
        api.evaluations(25).then((r) => r.evaluations),
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
      // Gate `loaded` on the server's data_ready signal. On a fresh
      // daemon boot in live mode, the wallet probe takes 1-2s to
      // warm; until then `data_ready` is false and the hero tiles
      // show dashes instead of flashing $0 placeholders that jump
      // to the real value seconds later. Treat `undefined` as ready
      // for backwards-compat with sidecars that don't emit the field.
      setLoaded(s?.data_ready !== false);
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
      || p.status === "closed_early"
      || p.status === "invalid",
    ),
    [positions],
  );

  // Equity time-series for the dashboard chart.
  //
  // Strategy: ALWAYS show the full historical curve back to the
  // first settled trade, then transition to real snapshots once
  // the daemon has been recording them.
  //
  // HISTORICAL PORTION: back-stepped from the equity at the
  // anchor point (= first snapshot's equity, or current equity if
  // no snapshots exist yet) through every settled position
  // chronologically. This reproduces the pre-snapshot chart 1:1
  // for the period before equity_snapshots existed.
  //
  // SNAPSHOT PORTION: real (ts, equity) points captured by the
  // daemon every ~10 min. Joined smoothly to the historical
  // portion at the first snapshot's ts (the back-step's last
  // point lands at the same equity value, so the visual is a
  // continuous curve).
  //
  // Deposits/withdrawals show as natural step-ups in the snapshot
  // region. Historical region cannot capture deposits made BEFORE
  // the first snapshot (we don't track historical deposit events)
  // but the user's full trading history is still visible.
  //
  // Same logic mirrored in Performance.tsx.
  const equitySeries = useMemo(() => {
    if (settled.length === 0 && equitySnapshots.length === 0) {
      return [] as { ts: string; v: number }[];
    }

    const sorted = [...settled].sort((a, b) =>
      ((a.settled_at ?? "") < (b.settled_at ?? "") ? -1 : 1),
    );

    // Anchor: first snapshot if available, else current equity.
    let anchorTs: string;
    let anchorEquity: number;
    let beforeAnchor: typeof sorted;
    if (equitySnapshots.length > 0) {
      const firstSnap = equitySnapshots[0];
      anchorTs = firstSnap.ts;
      anchorEquity = firstSnap.equity;
      beforeAnchor = sorted.filter((r) =>
        (r.settled_at ?? "") < anchorTs,
      );
    } else {
      anchorTs = "";
      anchorEquity = summary?.equity ?? summary?.starting_cash ?? 0;
      beforeAnchor = sorted;
    }

    // Walk backward from the anchor through every settled position
    // before it to recover the starting equity, then walk forward
    // emitting one chart point per settled event.
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
        historical.push({ ts: (r.settled_at as string | null | undefined) ?? "", v: cum });
      }
    }

    // Append real snapshots after the historical portion. The first
    // snapshot's equity equals the back-step's last value, so the
    // curve transitions smoothly.
    const snapshotPoints = equitySnapshots.map((s) => ({
      ts: s.ts, v: s.equity,
    }));

    return [...historical, ...snapshotPoints];
  }, [summary, settled, equitySnapshots]);

  const activity = useMemo(
    () => buildActivity(evaluations, open, settled),
    [evaluations, open, settled],
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
            automatically once more funds are available.
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
        <section className="dash-card card-activity">
          <CardHead title="Recent activity" />
          {activity.length === 0 ? (
            <Empty label={loaded ? "No activity yet." : "Loading..."} />
          ) : (
            <ActivityFeed items={activity} />
          )}
        </section>

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
            <PositionsTable positions={open} />
          )}
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
              checking - "how much wealth do I have on Polymarket?". */}
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

// Sort keys for the Dashboard preview table. Mirror the OpenSk
// enum in Positions.tsx so the two tables stay in lockstep
// visually. v1.5.13: collapsed from the bespoke pos-table CSS-grid
// component into a real <table.table-simple> matching the
// Positions tab 1:1 (user requested same column set, same widths,
// same truncate-with-chevron mechanic, same expanded-row layout).
type DashOpenSk = "id"     | "market"   | "category" | "side" | "size"
                | "value"  | "pnl"      | "opened"   | "closes";

function dashOpenKpi(p: PMPosition, f: DashOpenSk): SortKey {
  switch (f) {
    case "id":       return p.id;
    case "market":   return p.question;
    case "category": return (p.category as string | null) ?? "";
    case "side":     return p.side;
    case "size":     return p.cost_usd;
    case "value": {
      const cv = (p as unknown as Record<string, unknown>).current_value_usd as
        | number | null | undefined;
      return cv != null ? Number(cv) : p.cost_usd;
    }
    case "pnl": {
      const cv = (p as unknown as Record<string, unknown>).current_value_usd as
        | number | null | undefined;
      return cv != null ? Number(cv) - p.cost_usd : 0;
    }
    case "opened": {
      const iso = p.created_at as string | null | undefined;
      return iso ? new Date(iso).getTime() : null;
    }
    case "closes": {
      const iso = p.expected_resolution_at as string | null | undefined;
      return iso ? new Date(iso).getTime() : null;
    }
  }
}

// Top-N rows shown in the Dashboard preview before the user has to
// jump to the Positions tab. 5 keeps the dash card visually
// balanced against the activity feed next to it.
const DASH_OPEN_PREVIEW_LIMIT = 5;

function PositionsTable({ positions }: { positions: PMPosition[] }) {
  // Default sort = the row's own id descending so the most recently
  // opened position lands at the top of the preview. Matches the
  // Positions tab Open table 1:1 per the same-mechanic rule.
  const sort = useSort<DashOpenSk>("id", "desc");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const sorted = useMemo(
    () => sort.apply(positions, dashOpenKpi).slice(0, DASH_OPEN_PREVIEW_LIMIT),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [positions, sort.field, sort.dir],
  );
  return (
    <table className="table-simple">
      <colgroup>
        {/* Identical to the Open positions table on the Positions
            tab. Keep these widths in sync with src/pages/Positions.tsx
            so the user sees the same layout on both surfaces. */}
        <col style={{ width: "44px" }} />
        <col style={{ width: "auto" }} />
        <col style={{ width: "10%" }} />
        <col style={{ width: "56px" }} />
        <col style={{ width: "72px" }} />
        <col style={{ width: "72px" }} />
        <col style={{ width: "72px" }} />
        <col style={{ width: "72px" }} />
        <col style={{ width: "72px" }} />
        <col style={{ width: "28px" }} />
      </colgroup>
      <thead>
        <tr>
          <SortableTh field="id"       sort={sort}>#</SortableTh>
          <SortableTh field="market"   sort={sort}>Market</SortableTh>
          <SortableTh field="category" sort={sort}>Category</SortableTh>
          <SortableTh field="side"     sort={sort}>Side</SortableTh>
          <SortableTh field="size"     sort={sort}>Cost</SortableTh>
          <SortableTh field="value"    sort={sort}>Value</SortableTh>
          <SortableTh field="pnl"      sort={sort}>P&amp;L</SortableTh>
          <SortableTh field="opened"   sort={sort}>Opened</SortableTh>
          <SortableTh field="closes"   sort={sort}>Closes</SortableTh>
          <th />
        </tr>
      </thead>
      <tbody>
        {sorted.map((p) => {
          const isOpen = expanded.has(p.id);
          const reasoning = ((p.reasoning as string | null | undefined) ?? "").trim();
          const slug = p.slug as string | null | undefined;
          const polyUrl = slug ? `https://polymarket.com/market/${slug}` : null;
          const closesAt = (p.expected_resolution_at as string | null | undefined) ?? null;
          const category = (p.category as string | null | undefined) ?? null;
          const cv = (p as unknown as Record<string, unknown>).current_value_usd as
            | number | null | undefined;
          const haveMark = cv != null && p.shares > 0;
          const pnl = haveMark ? (Number(cv) - p.cost_usd) : null;
          const pnlClass =
            pnl == null ? ""
            : pnl > 0 ? "cell-up"
            : pnl < 0 ? "cell-down"
            : "";
          return (
            <React.Fragment key={p.id}>
              <tr
                className={`row-hover${isOpen ? " is-open" : ""}`}
                onClick={() => toggle(p.id)}
                style={{ cursor: "pointer" }}
              >
                <td className="mono" style={{ color: "var(--vellum-40)" }}>{p.id}</td>
                <td className="truncate" title={p.question}>{p.question}</td>
                <td className="truncate" title={category ?? ""}>{category ?? "-"}</td>
                <td><span className={p.side === "YES" ? "pill pill-yes" : "pill pill-no"}>{p.side}</span></td>
                <td className="mono">${p.cost_usd.toFixed(2)}</td>
                <td className="mono">{haveMark ? `$${Number(cv).toFixed(2)}` : "-"}</td>
                <td className={`mono ${pnlClass}`} style={{ whiteSpace: "nowrap" }}>{
                  pnl == null ? "-"
                  : (() => {
                      const pct = p.cost_usd > 0 ? (pnl / p.cost_usd) * 100 : 0;
                      const dol = pnl > 0
                        ? `+$${pnl.toFixed(2)}`
                        : pnl < 0
                          ? `-$${Math.abs(pnl).toFixed(2)}`
                          : "$0.00";
                      const pctStr = pct > 0
                        ? `+${pct.toFixed(1)}%`
                        : pct < 0
                          ? `${pct.toFixed(1)}%`
                          : "0.0%";
                      return `${dol} (${pctStr})`;
                    })()
                }</td>
                <td className="mono" title={p.created_at ? formatDateTime(p.created_at) : ""}>
                  {p.created_at ? timeAgo(p.created_at) : "-"}
                </td>
                <td className="mono">{daysFromNow(closesAt)}</td>
                <td className="mono" style={{ textAlign: "right" }}>
                  <span style={{
                    display: "inline-block",
                    color: isOpen ? "var(--gold)" : "var(--vellum-40)",
                    transform: isOpen ? "rotate(90deg)" : "none",
                    transition: "transform 0.15s ease",
                  }}>▸</span>
                </td>
              </tr>
              {isOpen && (
                <tr className="expanded-row">
                  <td colSpan={10} style={{ padding: "16px 20px 22px" }}>
                    <div className="kv-grid" style={{ marginBottom: 14 }}>
                      <div>
                        <div className="kv-label">Opened</div>
                        <div className="kv-val mono">{formatDateTime(p.created_at)}</div>
                      </div>
                      <div>
                        <div className="kv-label">Closes</div>
                        <div className="kv-val mono">{formatDateTime(closesAt)}</div>
                      </div>
                      <div>
                        <div className="kv-label">Entry price</div>
                        <div className="kv-val mono">{p.entry_price.toFixed(3)}</div>
                      </div>
                      <div>
                        <div className="kv-label">Shares</div>
                        <div className="kv-val mono">{p.shares.toFixed(2)}</div>
                      </div>
                      <div>
                        <div className="kv-label">Cost</div>
                        <div className="kv-val mono">${p.cost_usd.toFixed(2)}</div>
                      </div>
                      <div>
                        <div className="kv-label">Market YES %</div>
                        <div className="kv-val mono">{
                          Math.round((p.side === "YES" ? p.entry_price : 1 - p.entry_price) * 100)
                        }%</div>
                      </div>
                      <div>
                        <div className="kv-label">Delfi YES %</div>
                        <div className="kv-val mono">{
                          p.delfi_probability != null
                            ? `${Math.round(p.delfi_probability * 100)}%`
                            : "-"
                        }</div>
                      </div>
                      <div>
                        <div className="kv-label">Delfi confidence</div>
                        <div className="kv-val mono">{
                          p.confidence != null
                            ? `${Math.round(p.confidence * 100)}%`
                            : "-"
                        }</div>
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
                          try { void openUrl(polyUrl); }
                          catch { window.open(polyUrl, "_blank", "noopener,noreferrer"); }
                        }}
                      >
                        View on Polymarket →
                      </a>
                    )}
                  </td>
                </tr>
              )}
            </React.Fragment>
          );
        })}
      </tbody>
    </table>
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

function Empty({ label }: { label: string }) {
  return <div className="empty-state" style={{ padding: "32px 16px" }}>{label}</div>;
}

// (Equity chart now lives in src/components/EquityChart.tsx and is
//  shared with the Performance page so both views render identical
//  hover behaviour and tick math.)
