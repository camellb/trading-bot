import React, { useCallback, useEffect, useMemo, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { api, EventLogRow, isConnectionError, MarketEvaluation, PerformanceSummary, PMPosition } from "../api";
import { formatDateTime, daysFromNow as daysFromNowFmt, timeAgo } from "../lib/format";
import { SortableTh, SortKey, useSort } from "../components/SortableTh";

// Tauri webviews swallow `target="_blank"` clicks by default - the link
// just does nothing because the new window can't open inside the
// embedded WKWebView. Route external URLs through the opener plugin
// (already wired in tauri.conf + capabilities/default.json) so they
// open in the user's actual browser. Falls back to window.open in dev
// mode (vite preview outside Tauri) where the plugin isn't injected.
function openExternal(url: string): void {
  try {
    void openUrl(url);
  } catch {
    try {
      window.open(url, "_blank", "noopener,noreferrer");
    } catch {
      /* swallow - external link failures are non-fatal UX */
    }
  }
}

/**
 * Positions - SaaS-parity layout.
 *
 * page-wrap with chip filters (All / Open / Closed / Skipped) and three
 * panels (Open / Closed / Skipped) shown or hidden by filter. Rows are
 * clickable to expand reasoning + key-value detail.
 */

type Filter = "all" | "open" | "closed" | "skipped" | "errors";

// Local aliases that delegate to the central tz-aware formatters
// (src/lib/format.ts). Kept as small wrappers so the rest of the
// file reads the same as before. Closed/Skipped/Errors columns now
// show relative time (timeAgo) with the full ISO on hover; only
// the long-form fmtDateTime is needed locally for the title text.
const fmtDateTime = formatDateTime;
function daysFromNow(iso: string | null | undefined): string {
  if (!iso) return "-";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "-";
  // After the Polymarket trading window closes (the `endDate` we
  // count down to), the market enters UMA's optimistic-oracle
  // settlement: a proposer submits the resolution, a dispute
  // window runs (typically 2-12 hours), then funds are released.
  // The bot's resolver checks every 60s and settles the row
  // automatically once Polymarket flips `closed: true`. Until
  // then the position stays open and earns no extra P&L — it's
  // just waiting on the oracle. "settling" reads clearer than
  // "resolving"; the latter sounded instantaneous to users.
  if (t - Date.now() <= 0) return "settling";
  return daysFromNowFmt(iso);
}
function decision(raw: string | null): "BUY YES" | "BUY NO" | "SKIP" {
  const up = (raw ?? "").toUpperCase();
  if (up === "BUY_YES" || up === "YES") return "BUY YES";
  if (up === "BUY_NO" || up === "NO") return "BUY NO";
  return "SKIP";
}

// ── Sortable column keys + getters ──────────────────────────────────────
//
// Each table has its own enum of sortable columns. The getKpi
// functions return the raw scalar each click should sort by - never
// the formatted string, so "+10%" sorts after "+9%" not before it.

type OpenSk    = "market" | "category" | "side" | "size"
                | "myes"  | "dyes"     | "dconf" | "opened" | "closes";
type ClosedSk  = "market" | "category" | "side" | "outcome"
                | "entry" | "myes"     | "dyes"  | "pnl" | "settled";
type SkippedSk = "market" | "category" | "myes" | "dyes"
                | "dconf" | "evaluated";

function openKpi(p: PMPosition, f: OpenSk): SortKey {
  switch (f) {
    case "market":   return p.question;
    case "category": return (p.category as string | null) ?? "";
    case "side":     return p.side;
    case "size":     return p.cost_usd;
    case "myes": {
      const m = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
      return m;
    }
    case "dyes":     return (p.claude_probability as number | null) ?? null;
    case "dconf":    return (p.confidence as number | null) ?? null;
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

function closedKpi(p: PMPosition, f: ClosedSk): SortKey {
  switch (f) {
    case "market":   return p.question;
    case "category": return (p.category as string | null) ?? "";
    case "side":     return p.side;
    case "outcome": {
      const o = p.settlement_outcome as string | null | undefined;
      const won = o ? o === p.side : ((p.realized_pnl_usd as number | null) ?? 0) >= 0;
      return won ? "WON" : "LOST";
    }
    case "entry":    return p.entry_price;
    case "myes":     return p.side === "YES" ? p.entry_price : 1 - p.entry_price;
    case "dyes":     return (p.claude_probability as number | null) ?? null;
    case "pnl":      return (p.realized_pnl_usd as number | null) ?? null;
    case "settled": {
      const iso = p.settled_at as string | null | undefined;
      return iso ? new Date(iso).getTime() : null;
    }
  }
}

// Polymarket V2 returns raw internal error strings (order hashes,
// 6-decimal USDC uints). Translate the patterns we know about into
// something a non-developer can act on. Unknown errors fall through
// to the original text so we don't accidentally hide useful info.
function humanizePolymarketError(raw: string): string {
  // 5-share minimum:
  //   "order 0x... is invalid. Size (1.73) lower than the minimum: 5"
  const sizeMin = raw.match(
    /Size \(([\d.]+)\) lower than the minimum:\s*([\d.]+)/i
  );
  if (sizeMin) {
    return (
      `Order too small: ${sizeMin[1]} shares (Polymarket needs ` +
      `${sizeMin[2]}). Wait for a cheaper market or raise the stake.`
    );
  }
  // $1 notional minimum:
  //   "invalid amount for a marketable BUY order ($0.17), min size: $1"
  const usdMin = raw.match(
    /marketable BUY order \(\$([\d.]+)\),\s*min size:\s*\$([\d.]+)/i
  );
  if (usdMin) {
    return (
      `Order $${usdMin[1]} is below Polymarket's $${usdMin[2]} ` +
      `minimum. Raise the stake to clear the floor.`
    );
  }
  // Insufficient balance — raw uints in 6-decimal USDC:
  //   "balance: 8101422, order amount: 21684640"
  const bal = raw.match(
    /balance:\s*(\d+),\s*order amount:\s*(\d+)/i
  );
  if (bal) {
    const have = (Number(bal[1]) / 1e6).toFixed(2);
    const need = (Number(bal[2]) / 1e6).toFixed(2);
    return (
      `Not enough on Polymarket: order needed $${need}, wallet has ` +
      `$${have}. Deposit more or lower the stake.`
    );
  }
  // Signer mismatch (should never appear post-SDK-1.0.1 but kept as
  // a safety belt — if Polymarket ever regresses the api-key
  // binding, the message stays human):
  if (/signer address has to be the address of the API KEY/i.test(raw)) {
    return (
      "Polymarket rejected the order signer. Try re-saving your " +
      "Polymarket private key in Settings."
    );
  }
  // Anything else: hand back the raw error. We don't pretend.
  return raw;
}

function skippedKpi(e: MarketEvaluation, f: SkippedSk): SortKey {
  switch (f) {
    case "market":   return e.question;
    case "category": return e.category ?? "";
    case "myes":     return e.market_price_yes ?? null;
    case "dyes":     return e.claude_probability ?? null;
    case "dconf":    return e.confidence ?? null;
    case "evaluated": {
      const iso = e.evaluated_at;
      return iso ? new Date(iso).getTime() : null;
    }
  }
}

export default function Positions() {
  const [filter, setFilter] = useState<Filter>("all");
  const [positions, setPositions] = useState<PMPosition[]>([]);
  const [evals, setEvals] = useState<MarketEvaluation[]>([]);
  const [events, setEvents] = useState<EventLogRow[]>([]);
  // Server-side aggregate counts. Used for chip labels so the chip
  // numbers reconcile with the Dashboard tile (which also reads from
  // /api/summary). Without this, the chips show counts limited to
  // the fetched 100 positions / 50 evals and disagree with the
  // Dashboard for any user with more history.
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedPos, setExpandedPos] = useState<Set<number>>(new Set());
  const [expandedEval, setExpandedEval] = useState<Set<number>>(new Set());

  const refresh = useCallback(async () => {
    try {
      const [p, e, s, ev] = await Promise.all([
        api.positions(100).then((r) => r.positions),
        api.evaluations(50).then((r) => r.evaluations),
        api.summary(),
        // Pull a generous slice so the Errors tab has history. We
        // filter client-side to order_error rows below; everything
        // else from event_log we ignore.
        api.events(200).then((r) => r.events),
      ]);
      setPositions(p);
      setEvals(e);
      setSummary(s);
      setEvents(ev);
      setLoaded(true);
      // Clear error only on confirmed success - prevents the 0.3s
      // flash where a stale error vanishes pre-await and then the
      // refresh fails again, re-showing the banner.
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, [refresh]);

  const open = useMemo(
    () => positions.filter((p) => p.status === "open"),
    [positions],
  );
  const settled = useMemo(
    // `closed` is a stale status string no code path emits today;
    // pm_executor.py only writes open / settled / invalid / closed_early.
    // Show natural settlements AND early-policy exits side-by-side under
    // the Closed pane; the status badge in the table disambiguates.
    () => positions.filter(
      (p) => p.status === "settled"
          || p.status === "invalid"
          || p.status === "closed_early"
    ),
    [positions],
  );
  const skipped = useMemo(
    () => evals.filter((e) => decision(e.recommendation) === "SKIP"),
    [evals],
  );
  // Order errors from event_log. Most-recent-first ordering is
  // preserved by the server (api.events() returns DESC by id).
  const errors = useMemo(
    () => events.filter((e) => e.event_type === "order_error"),
    [events],
  );

  // Sort states. One per table so they're independent. Defaults
  // mirror "most recent first" or "biggest first" depending on
  // what users will scan for in that view.
  const openSort    = useSort<OpenSk>("size", "desc");
  const closedSort  = useSort<ClosedSk>("settled", "desc");
  const skippedSort = useSort<SkippedSk>("evaluated", "desc");

  const openRows = useMemo(
    () => openSort.apply(open, openKpi),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [open, openSort.field, openSort.dir],
  );
  const closedRows = useMemo(
    () => closedSort.apply(settled, closedKpi),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [settled, closedSort.field, closedSort.dir],
  );
  const skippedRows = useMemo(
    () => skippedSort.apply(skipped, skippedKpi),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [skipped, skippedSort.field, skippedSort.dir],
  );

  const deployed = open.reduce((s, p) => s + (p.cost_usd ?? 0), 0);

  const togglePos = (id: number) =>
    setExpandedPos((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const toggleEval = (id: number) =>
    setExpandedEval((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Positions</h1>
          </div>
        </div>
      </div>

      {error && !isConnectionError(error) && (
        <div className="error">{error}</div>
      )}

      <div className="page-toolbar">
        <div className="page-toolbar-left">
          <button className={`chip ${filter === "all" ? "on" : ""}`} onClick={() => setFilter("all")}>
            All
          </button>
          <button className={`chip ${filter === "open" ? "on" : ""}`} onClick={() => setFilter("open")}>
            Open ({summary?.open_positions ?? open.length})
          </button>
          <button className={`chip ${filter === "closed" ? "on" : ""}`} onClick={() => setFilter("closed")}>
            Closed ({summary?.settled_total ?? settled.length})
          </button>
          <button className={`chip ${filter === "skipped" ? "on" : ""}`} onClick={() => setFilter("skipped")}>
            Skipped ({summary?.skipped_total ?? skipped.length})
          </button>
          <button className={`chip ${filter === "errors" ? "on" : ""}`} onClick={() => setFilter("errors")}>
            Errors ({errors.length})
          </button>
        </div>
      </div>

      {(filter === "all" || filter === "open") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Open positions</h2>
            <span className="panel-meta">
              {open.length} active · ${deployed.toFixed(0)} deployed
            </span>
          </div>
          {open.length === 0 ? (
            <div className="empty-state">
              {loaded
                ? "No open positions yet. Delfi is evaluating markets, positions will appear once a trade clears the gate."
                : "Loading..."}
            </div>
          ) : (
            <table className="table-simple">
              <colgroup>
                <col style={{ width: "auto" }} />
                <col style={{ width: "12%" }} />
                <col style={{ width: "60px" }} />
                <col style={{ width: "70px" }} />
                <col style={{ width: "70px" }} />
                <col style={{ width: "70px" }} />
                <col style={{ width: "70px" }} />
                <col style={{ width: "80px" }} />
                <col style={{ width: "80px" }} />
                <col style={{ width: "28px" }} />
              </colgroup>
              <thead>
                <tr>
                  <SortableTh field="market"   sort={openSort}>Market</SortableTh>
                  <SortableTh field="category" sort={openSort}>Category</SortableTh>
                  <SortableTh field="side"     sort={openSort}>Side</SortableTh>
                  <SortableTh field="size"     sort={openSort}>Size</SortableTh>
                  <SortableTh field="myes"     sort={openSort}>M YES %</SortableTh>
                  <SortableTh field="dyes"     sort={openSort}>D YES %</SortableTh>
                  <SortableTh field="dconf"    sort={openSort}>D CONF</SortableTh>
                  <SortableTh field="opened"   sort={openSort}>Opened</SortableTh>
                  <SortableTh field="closes"   sort={openSort}>Closes</SortableTh>
                  <th />
                </tr>
              </thead>
              <tbody>
                {openRows.map((p) => {
                  const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
                  const mYesPct = Math.round(marketYes * 100);
                  const cp = (p.claude_probability as number | null | undefined) ?? null;
                  const cf = (p.confidence as number | null | undefined) ?? null;
                  const dYesPct = cp != null ? Math.round(cp * 100) : null;
                  const dConfPct = cf != null ? Math.round(cf * 100) : null;
                  const isOpen = expandedPos.has(p.id);
                  const reasoning = ((p.reasoning as string | null | undefined) ?? "").trim();
                  const slug = p.slug as string | null | undefined;
                  const polyUrl = slug ? `https://polymarket.com/market/${slug}` : null;
                  const closesAt = (p.expected_resolution_at as string | null | undefined) ?? null;
                  const category = (p.category as string | null | undefined) ?? null;
                  return (
                    <React.Fragment key={p.id}>
                      <tr
                        className={`row-hover${isOpen ? " is-open" : ""}`}
                        onClick={() => togglePos(p.id)}
                        style={{ cursor: "pointer" }}
                      >
                        <td className="truncate" title={p.question}>{p.question}</td>
                        <td className="truncate" title={category ?? ""}>{category ?? "-"}</td>
                        <td><span className={p.side === "YES" ? "pill pill-yes" : "pill pill-no"}>{p.side}</span></td>
                        <td className="mono">${p.cost_usd.toFixed(0)}</td>
                        <td className="mono">{mYesPct}%</td>
                        <td className="mono">{dYesPct != null ? `${dYesPct}%` : "-"}</td>
                        <td className="mono">{dConfPct != null ? `${dConfPct}%` : "-"}</td>
                        <td className="mono">{timeAgo(p.created_at)}</td>
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
                                <div className="kv-val mono">{fmtDateTime(p.created_at)}</div>
                              </div>
                              <div>
                                <div className="kv-label">Closes</div>
                                <div className="kv-val mono">{fmtDateTime(closesAt)}</div>
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
                                  openExternal(polyUrl);
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
          )}
        </div>
      )}

      {(filter === "all" || filter === "closed") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Closed positions</h2>
            <span className="panel-meta">{settled.length} settled</span>
          </div>
          {settled.length === 0 ? (
            <div className="empty-state">
              {loaded ? "No closed trades yet." : "Loading..."}
            </div>
          ) : (
            <table className="table-simple">
              <thead>
                <tr>
                  <SortableTh field="market"   sort={closedSort}>Market</SortableTh>
                  <SortableTh field="category" sort={closedSort}>Category</SortableTh>
                  <SortableTh field="side"     sort={closedSort}>Side</SortableTh>
                  <SortableTh field="outcome"  sort={closedSort}>Outcome</SortableTh>
                  <SortableTh field="entry"    sort={closedSort}>Entry</SortableTh>
                  <SortableTh field="myes"     sort={closedSort}>M YES %</SortableTh>
                  <SortableTh field="dyes"     sort={closedSort}>D YES %</SortableTh>
                  <SortableTh field="pnl"      sort={closedSort}>P&amp;L</SortableTh>
                  <SortableTh field="settled"  sort={closedSort}>Settled</SortableTh>
                </tr>
              </thead>
              <tbody>
                {closedRows.map((s) => {
                  const pnl = (s.realized_pnl_usd as number | null | undefined) ?? 0;
                  const outcome = s.settlement_outcome as string | null | undefined;
                  const settledAt = s.settled_at as string | null | undefined;
                  const status = (s.status as string | null | undefined) ?? "settled";
                  // Four states now. INVALID resolutions (50/50 void)
                  // render as VOID, not LOST. CLOSED_EARLY rows show
                  // the exit reason as the outcome label and use the
                  // P&L sign to colour-code the pill. Natural settlements
                  // are decided by `settlement_outcome == side`.
                  const isInvalid = status === "invalid" || outcome === "INVALID";
                  const isClosedEarly = status === "closed_early";
                  const won = !isInvalid && !isClosedEarly && (outcome ? outcome === s.side : pnl > 0);
                  const closeReason = (s.close_reason as string | null | undefined) ?? null;
                  const reasonLabel = closeReason === "take_profit" ? "TP"
                                    : closeReason === "stop_loss"   ? "SL"
                                    : closeReason === "time_decay"  ? "TIME"
                                    : "EARLY";
                  const outcomeLabel = isInvalid
                    ? "VOID"
                    : isClosedEarly
                      ? `EXIT ${reasonLabel}`
                      : (won ? "WON" : "LOST");
                  const outcomeClass = isInvalid
                    ? "pill pill-void"
                    : isClosedEarly
                      ? (pnl >= 0 ? "pill pill-won" : "pill pill-lost")
                      : (won ? "pill pill-won" : "pill pill-lost");
                  const pnlCellClass = pnl > 0
                    ? "cell-up"
                    : (pnl < 0 ? "cell-down" : "");
                  const pnlText = pnl > 0
                    ? `+$${pnl.toFixed(2)}`
                    : (pnl < 0
                        ? `-$${Math.abs(pnl).toFixed(2)}`
                        : "$0.00");
                  const category = (s.category as string | null | undefined) ?? null;
                  // Market implied probability YES at entry. entry_price is
                  // the price paid for the chosen side, so for a NO entry
                  // we flip it to derive the implied YES probability.
                  const marketYes = s.side === "YES" ? s.entry_price : 1 - s.entry_price;
                  const mYesPct = Math.round(marketYes * 100);
                  const cp = (s.claude_probability as number | null | undefined) ?? null;
                  const dYesPct = cp != null ? Math.round(cp * 100) : null;
                  return (
                    <tr key={s.id} className="row-hover">
                      <td>{s.question}</td>
                      <td>{category ?? "-"}</td>
                      <td><span className={s.side === "YES" ? "pill pill-yes" : "pill pill-no"}>{s.side}</span></td>
                      <td><span className={outcomeClass}>{outcomeLabel}</span></td>
                      <td className="mono">{s.entry_price.toFixed(3)}</td>
                      <td className="mono">{mYesPct}%</td>
                      <td className="mono">{dYesPct != null ? `${dYesPct}%` : "-"}</td>
                      <td className={`mono ${pnlCellClass}`}>{pnlText}</td>
                      <td className="mono" title={settledAt ? fmtDateTime(settledAt) : ""}>
                        {settledAt ? timeAgo(settledAt) : "-"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {(filter === "all" || filter === "skipped") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Skipped evaluations</h2>
            <span className="panel-meta">{skipped.length} skipped</span>
          </div>
          {skipped.length === 0 ? (
            <div className="empty-state">
              {loaded ? "No skipped evaluations to show." : "Loading..."}
            </div>
          ) : (
            <table className="table-simple">
              <thead>
                <tr>
                  <SortableTh field="market"    sort={skippedSort}>Market</SortableTh>
                  <SortableTh field="category"  sort={skippedSort}>Category</SortableTh>
                  <SortableTh field="myes"      sort={skippedSort}>M YES %</SortableTh>
                  <SortableTh field="dyes"      sort={skippedSort}>D YES %</SortableTh>
                  <SortableTh field="dconf"     sort={skippedSort}>D CONF</SortableTh>
                  <SortableTh field="evaluated" sort={skippedSort}>Evaluated</SortableTh>
                  <th>Result</th>
                  <th style={{ width: 28 }} />
                </tr>
              </thead>
              <tbody>
                {skippedRows.map((e) => {
                  const isOpen = expandedEval.has(e.id);
                  const dYesPct = e.claude_probability != null ? Math.round(e.claude_probability * 100) : null;
                  const mYesPct = e.market_price_yes != null ? Math.round(e.market_price_yes * 100) : null;
                  const dConfPct = e.confidence != null ? Math.round(e.confidence * 100) : null;
                  const reasoning = (e.reasoning ?? "").trim();
                  // Polymarket slug → external link. Matches the open-row
                  // pattern further up the file.
                  const slug = e.slug as string | null | undefined;
                  const polyUrl = slug ? `https://polymarket.com/market/${slug}` : null;
                  return (
                    <React.Fragment key={e.id}>
                      <tr
                        className="row-hover"
                        onClick={() => toggleEval(e.id)}
                        style={{ cursor: "pointer" }}
                      >
                        <td>{e.question}</td>
                        <td>{e.category ?? "-"}</td>
                        <td className="mono">{mYesPct != null ? `${mYesPct}%` : "-"}</td>
                        <td className="mono">{dYesPct != null ? `${dYesPct}%` : "-"}</td>
                        <td className="mono">{dConfPct != null ? `${dConfPct}%` : "-"}</td>
                        <td className="mono" title={e.evaluated_at ? fmtDateTime(e.evaluated_at) : ""}>
                          {e.evaluated_at ? timeAgo(e.evaluated_at) : "-"}
                        </td>
                        <td>
                          {(() => {
                            // RESULT pill: PENDING / YES / NO / VOID.
                            //
                            // Back-filled by resolve_skipped_evaluations
                            // every 15 min once the market closes. NULL
                            // = market still open. INVALID = the market
                            // resolved as 50/50 / void on Polymarket.
                            const o = (e.settlement_outcome || "").toUpperCase();
                            let label = "PENDING";
                            let cls = "skip-result pending";
                            if (o === "YES") { label = "YES"; cls = "skip-result yes"; }
                            else if (o === "NO") { label = "NO";  cls = "skip-result no";  }
                            else if (o === "INVALID") { label = "VOID"; cls = "skip-result void"; }
                            return <span className={cls}>{label}</span>;
                          })()}
                        </td>
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
                          <td colSpan={8} style={{ padding: "16px 20px 22px" }}>
                            <div className="pos-detail-reason">
                              <div className="pos-detail-reason-label">Why Delfi skipped</div>
                              {reasoning || "No reasoning recorded."}
                            </div>
                            {polyUrl && (
                              <a
                                className="pos-detail-link"
                                href={polyUrl}
                                onClick={(ev) => {
                                  ev.preventDefault();
                                  ev.stopPropagation();
                                  openExternal(polyUrl);
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
          )}
        </div>
      )}

      {(filter === "all" || filter === "errors") && (
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Order errors</h2>
            <span className="panel-meta">{errors.length} rejected by Polymarket</span>
          </div>
          {errors.length === 0 ? (
            <div className="empty-state">
              {loaded
                ? "No order errors. Every live order has been accepted by Polymarket."
                : "Loading..."}
            </div>
          ) : (
            <table className="table-simple" style={{ tableLayout: "fixed", width: "100%" }}>
              <colgroup>
                {/* Market: takes a comfortable chunk; truncates long
                    questions. Side: pill width. Stake: enough for
                    "25.26 @ $0.850" without wrapping. Reason: takes
                    the rest, which is the column with real information
                    density - was previously capped at 320px causing
                    the rest of the table to balloon with dead space.
                    When: short relative-time cell. */}
                <col style={{ width: "26%" }} />
                <col style={{ width: 64 }} />
                <col style={{ width: 140 }} />
                <col />
                <col style={{ width: 96 }} />
              </colgroup>
              <thead>
                <tr>
                  <th>Market</th>
                  <th>Side</th>
                  <th>Size</th>
                  <th>Reason</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {errors.map((row) => {
                  // Parse the description written by
                  // pm_executor._open_live. Format:
                  //   "Order rejected on '<question>': <SIDE>
                  //    <size>@$<price>. <error message>"
                  const desc = row.description || "";
                  const m = desc.match(
                    /^Order rejected on '(.+?)':\s*(\S+)\s+([\d.]+)@\$([\d.]+)\.\s*(.*)$/
                  );
                  const question = m?.[1] ?? "—";
                  const side     = m?.[2] ?? "—";
                  const size     = m?.[3] ?? null;
                  const price    = m?.[4] ?? null;
                  const reasonRaw = m?.[5] ?? desc;
                  // Pull the human-readable error out of the SDK's
                  // PolyApiException wrapper for prettier display.
                  const polyErr = reasonRaw.match(
                    /error_message=\{'error':\s*'(.+?)'\}/
                  );
                  const reason = humanizePolymarketError(polyErr?.[1] ?? reasonRaw);
                  const sideClass =
                    side === "YES" ? "side-yes"
                    : side === "NO" ? "side-no"
                    : "";
                  return (
                    <tr key={row.id}>
                      <td className="truncate" title={question}>{question}</td>
                      <td className={`mono ${sideClass}`}>{side}</td>
                      <td className="mono" style={{ whiteSpace: "nowrap" }}>
                        {size && price
                          ? `$${(Number(size) * Number(price)).toFixed(0)}`
                          : "—"}
                      </td>
                      <td
                        style={{
                          color: "var(--vellum-60)",
                          whiteSpace: "normal",
                          wordBreak: "break-word",
                          overflowWrap: "anywhere",
                          lineHeight: 1.4,
                        }}
                      >
                        {reason}
                      </td>
                      <td className="mono" style={{ whiteSpace: "nowrap" }}
                          title={row.timestamp ? fmtDateTime(row.timestamp) : ""}>
                        {row.timestamp ? timeAgo(row.timestamp) : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
