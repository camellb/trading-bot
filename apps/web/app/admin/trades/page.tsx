"use client";

import { useEffect, useMemo, useState } from "react";
import { getJSON } from "@/lib/fetch-json";

type ModeFilter = "all" | "live" | "sim";
type StatusFilter = "all" | "open" | "won" | "lost";

type AdminTrade = {
  id:                  number;
  created_at:          string | null;
  user_id:             string;
  email:               string | null;
  display_name:        string | null;
  mode:                string | null;
  market_id:           string | null;
  slug:                string | null;
  question:            string | null;
  category:            string | null;
  market_archetype:    string | null;
  side:                string | null;
  cost_usd:            number | null;
  entry_price:         number | null;
  claude_probability:  number | null;
  status:              string | null;
  realized_pnl_usd:    number | null;
  settled_at:          string | null;
};

type TradesPayload = {
  trades: AdminTrade[];
  total:  number;
  limit:  number;
  offset: number;
};

const PAGE_SIZE = 50;

function fmtDateTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

function fmtProb(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtPrice(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return v.toFixed(3);
}

function statusPill(status: string | null, pnl: number | null): { label: string; klass: string } {
  if (status === "open") return { label: "open", klass: "pill-open" };
  if (status === "invalid") return { label: "invalid", klass: "pill-skip" };
  if (status === "closed_early") return { label: "closed", klass: "pill-skip" };
  if (status === "settled") {
    const p = pnl ?? 0;
    if (p > 0) return { label: "won", klass: "pill-won" };
    if (p < 0) return { label: "lost", klass: "pill-lost" };
    return { label: "flat", klass: "pill-skip" };
  }
  return { label: status ?? "-", klass: "pill-skip" };
}

function truncate(s: string | null, n: number): string {
  if (!s) return "-";
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

export default function AdminTradesPage() {
  const [data, setData]       = useState<TradesPayload | null>(null);
  const [loaded, setLoaded]   = useState(false);
  const [error, setError]     = useState<string | null>(null);
  const [mode, setMode]       = useState<ModeFilter>("all");
  const [status, setStatus]   = useState<StatusFilter>("all");
  const [q, setQ]             = useState("");
  const [qDebounced, setQD]   = useState("");
  const [offset, setOffset]   = useState(0);

  useEffect(() => {
    const t = setTimeout(() => setQD(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    setOffset(0);
  }, [mode, status, qDebounced]);

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams();
    if (mode !== "all") params.set("mode", mode);
    if (status !== "all") params.set("status", status);
    if (qDebounced) params.set("q", qDebounced);
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(offset));

    const load = async () => {
      setError(null);
      try {
        const res = await getJSON<TradesPayload>(`/api/admin/trades?${params.toString()}`);
        if (cancelled) return;
        setData(res);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load trades");
      } finally {
        if (!cancelled) setLoaded(true);
      }
    };
    load();
    return () => { cancelled = true; };
  }, [mode, status, qDebounced, offset]);

  const trades = data?.trades ?? [];
  const total = data?.total ?? 0;
  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + PAGE_SIZE, total);
  const canPrev = offset > 0;
  const canNext = offset + PAGE_SIZE < total;

  const summary = useMemo(() => {
    if (!data) return "";
    if (total === 0) return "0 trades";
    return `${pageStart}-${pageEnd} of ${total.toLocaleString()} trades`;
  }, [data, total, pageStart, pageEnd]);

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Trades</h1>
            <p className="page-sub">Every position across every user, with mode, P&amp;L, and the forecast behind it.</p>
          </div>
        </div>
      </div>

      <div className="page-toolbar" style={{ flexWrap: "wrap", gap: 12 }}>
        <div style={{ flex: "1 1 280px", maxWidth: 360 }}>
          <input
            className="ob-input"
            placeholder="Search by email, user id, or market"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>

        <div className="tab-bar" style={{ padding: 0, margin: 0 }}>
          {(["all", "live", "sim"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setMode(k)}
              className={`tab ${mode === k ? "on" : ""}`}
              style={{ textTransform: "uppercase" }}
            >
              {k === "all" ? "All modes" : k}
            </button>
          ))}
        </div>

        <div className="tab-bar" style={{ padding: 0, margin: 0 }}>
          {(["all", "open", "won", "lost"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setStatus(k)}
              className={`tab ${status === k ? "on" : ""}`}
              style={{ textTransform: "capitalize" }}
            >
              {k}
            </button>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">{loaded ? summary : "Loading trades..."}</h2>
        </div>

        {error ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">{error}</div></div></div>
        ) : !loaded ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">Loading...</div></div></div>
        ) : trades.length === 0 ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">No trades match.</div></div></div>
        ) : (
          <table className="table-simple">
            <thead>
              <tr>
                <th>When</th>
                <th>Mode</th>
                <th>User</th>
                <th>Market</th>
                <th>Side</th>
                <th>Size</th>
                <th>M YES %</th>
                <th>D YES %</th>
                <th>P&amp;L</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => {
                const st = statusPill(t.status, t.realized_pnl_usd);
                const userLabel = t.email || t.display_name || t.user_id.slice(0, 8);
                const marketUrl = t.slug ? `https://polymarket.com/event/${t.slug}` : null;
                const catLine = [t.category, t.market_archetype].filter(Boolean).join(" · ");
                return (
                  <tr key={t.id}>
                    <td className="mono" style={{ color: "var(--vellum-60)", whiteSpace: "nowrap" }}>
                      {fmtDateTime(t.created_at)}
                    </td>
                    <td>
                      <span className={`pill ${t.mode === "live" ? "pill-no" : "pill-open"}`}>
                        {t.mode === "live" ? "LIVE" : "SIM"}
                      </span>
                    </td>
                    <td>
                      <div style={{ fontSize: 13 }}>{truncate(userLabel, 28)}</div>
                    </td>
                    <td style={{ maxWidth: 360 }}>
                      {marketUrl ? (
                        <a href={marketUrl} target="_blank" rel="noreferrer" style={{ color: "var(--vellum)" }}>
                          {truncate(t.question, 64)}
                        </a>
                      ) : (
                        truncate(t.question, 64)
                      )}
                      {catLine ? (
                        <div className="split-desc" style={{ fontSize: 11 }}>{catLine}</div>
                      ) : null}
                    </td>
                    <td>
                      <span className={`pill ${t.side === "YES" ? "pill-yes" : "pill-no"}`}>
                        {t.side ?? "-"}
                      </span>
                    </td>
                    <td className="mono">
                      {t.cost_usd !== null && t.cost_usd !== undefined
                        ? `$${t.cost_usd.toLocaleString("en-US", { maximumFractionDigits: 2 })}`
                        : "-"}
                    </td>
                    <td className="mono">
                      {t.entry_price != null && t.side
                        ? `${Math.round((t.side === "YES" ? t.entry_price : 1 - t.entry_price) * 100)}%`
                        : "-"}
                    </td>
                    <td className="mono" style={{ color: "var(--vellum-60)" }}>
                      {t.claude_probability != null
                        ? `${Math.round(t.claude_probability * 100)}%`
                        : "-"}
                    </td>
                    <td className={`mono ${
                      (t.realized_pnl_usd ?? 0) > 0 ? "cell-up"
                      : (t.realized_pnl_usd ?? 0) < 0 ? "cell-down"
                      : ""
                    }`}>
                      {t.status === "open" ? "-" : fmtMoney(t.realized_pnl_usd)}
                    </td>
                    <td>
                      <span className={`pill ${st.klass}`}>{st.label}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        {loaded && total > PAGE_SIZE ? (
          <div className="split-row" style={{ justifyContent: "space-between" }}>
            <div className="split-body">
              <div className="split-desc">Page {Math.floor(offset / PAGE_SIZE) + 1} of {Math.ceil(total / PAGE_SIZE)}</div>
            </div>
            <div className="split-right" style={{ display: "flex", gap: 8 }}>
              <button
                className="btn-sm"
                disabled={!canPrev}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                ← Prev
              </button>
              <button
                className="btn-sm"
                disabled={!canNext}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next →
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}
