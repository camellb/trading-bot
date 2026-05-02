import React, { useCallback, useEffect, useMemo, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { api, isConnectionError, MarketEvaluation, PMPosition } from "../api";
import { formatDate, formatDateTime, daysFromNow as daysFromNowFmt } from "../lib/format";
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

type Filter = "all" | "open" | "closed" | "skipped";

// Local aliases that delegate to the central tz-aware formatters
// (src/lib/format.ts). Kept as small wrappers so the rest of the
// file reads the same as before.
const fmt = formatDate;
const fmtDateTime = formatDateTime;
function daysFromNow(iso: string | null | undefined): string {
  if (!iso) return "-";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "-";
  if (t - Date.now() <= 0) return "resolving";
  // The lib version returns "Xd Yh" / "Xh Ym"; this page likes a
  // single-token form for the table. Fall through to that when we
  // have a positive future delta.
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
                | "myes"  | "dyes"     | "dconf" | "closes";
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
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedPos, setExpandedPos] = useState<Set<number>>(new Set());
  const [expandedEval, setExpandedEval] = useState<Set<number>>(new Set());

  const refresh = useCallback(async () => {
    try {
      const [p, e] = await Promise.all([
        api.positions(100).then((r) => r.positions),
        api.evaluations(50).then((r) => r.evaluations),
      ]);
      setPositions(p);
      setEvals(e);
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
    () => positions.filter((p) => p.status === "settled" || p.status === "closed"),
    [positions],
  );
  const skipped = useMemo(
    () => evals.filter((e) => decision(e.recommendation) === "SKIP"),
    [evals],
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
            Open ({open.length})
          </button>
          <button className={`chip ${filter === "closed" ? "on" : ""}`} onClick={() => setFilter("closed")}>
            Closed ({settled.length})
          </button>
          <button className={`chip ${filter === "skipped" ? "on" : ""}`} onClick={() => setFilter("skipped")}>
            Skipped ({skipped.length})
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
              <thead>
                <tr>
                  <SortableTh field="market"   sort={openSort}>Market</SortableTh>
                  <SortableTh field="category" sort={openSort}>Category</SortableTh>
                  <SortableTh field="side"     sort={openSort}>Side</SortableTh>
                  <SortableTh field="size"     sort={openSort}>Size</SortableTh>
                  <SortableTh field="myes"     sort={openSort}>M YES %</SortableTh>
                  <SortableTh field="dyes"     sort={openSort}>D YES %</SortableTh>
                  <SortableTh field="dconf"    sort={openSort}>D CONF</SortableTh>
                  <SortableTh field="closes"   sort={openSort}>Closes</SortableTh>
                  <th style={{ width: 28 }} />
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
                        className="row-hover"
                        onClick={() => togglePos(p.id)}
                        style={{ cursor: "pointer" }}
                      >
                        <td>{p.question}</td>
                        <td>{category ?? "-"}</td>
                        <td><span className={p.side === "YES" ? "pill pill-yes" : "pill pill-no"}>{p.side}</span></td>
                        <td className="mono">${p.cost_usd.toFixed(0)}</td>
                        <td className="mono">{mYesPct}%</td>
                        <td className="mono">{dYesPct != null ? `${dYesPct}%` : "-"}</td>
                        <td className="mono">{dConfPct != null ? `${dConfPct}%` : "-"}</td>
                        <td className="mono">{daysFromNow(closesAt)}</td>
                        <td className="mono" style={{
                          color: isOpen ? "var(--gold)" : "var(--vellum-40)",
                          transform: isOpen ? "rotate(90deg)" : "none",
                          display: "inline-block",
                          transition: "transform 0.15s ease",
                        }}>▸</td>
                      </tr>
                      {isOpen && (
                        <tr className="expanded-row">
                          <td colSpan={9} style={{ padding: "16px 20px 22px" }}>
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
                  const won = outcome ? outcome === s.side : pnl >= 0;
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
                      <td><span className={won ? "pill pill-won" : "pill pill-lost"}>{won ? "WON" : "LOST"}</span></td>
                      <td className="mono">{s.entry_price.toFixed(3)}</td>
                      <td className="mono">{mYesPct}%</td>
                      <td className="mono">{dYesPct != null ? `${dYesPct}%` : "-"}</td>
                      <td className={`mono ${pnl >= 0 ? "cell-up" : "cell-down"}`}>
                        {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
                      </td>
                      <td className="mono">{fmt(settledAt)}</td>
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
                        <td className="mono">{fmt(e.evaluated_at)}</td>
                        <td className="mono" style={{
                          color: isOpen ? "var(--gold)" : "var(--vellum-40)",
                          transform: isOpen ? "rotate(90deg)" : "none",
                          display: "inline-block",
                          transition: "transform 0.15s ease",
                        }}>▸</td>
                      </tr>
                      {isOpen && (
                        <tr className="expanded-row">
                          <td colSpan={7} style={{ padding: "16px 20px 22px" }}>
                            <div className="pos-detail-reason">
                              <div className="pos-detail-reason-label">Why Delfi skipped</div>
                              {reasoning || "No reasoning recorded."}
                            </div>
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
    </div>
  );
}
