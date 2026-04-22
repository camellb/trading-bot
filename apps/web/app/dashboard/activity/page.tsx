"use client";

import { useEffect, useMemo, useState } from "react";
import "../../styles/content.css";

type Kind = "execute" | "pass" | "resolve" | "update";

type Event = {
  id: string;
  ts: number;
  dateLabel: string;
  timeLabel: string;
  kind: Kind;
  title: string;
  meta: string;
  detail?: string;
};

type OpenPosition = {
  id: number;
  question: string;
  side: "YES" | "NO";
  cost_usd: number;
  entry_price: number;
  claude_probability: number | null;
  confidence: number | null;
  reasoning: string | null;
  created_at: string | null;
};

type SettledPosition = {
  id: number;
  question: string;
  side: "YES" | "NO";
  cost_usd: number;
  realized_pnl_usd: number | null;
  settlement_outcome: string | null;
  settled_at: string | null;
  claude_probability: number | null;
};

type PositionsPayload = { open: OpenPosition[]; settled: SettledPosition[] };

type Evaluation = {
  id: number;
  evaluated_at: string | null;
  question: string;
  claude_probability: number | null;
  market_price_yes: number | null;
  recommendation: string | null;
  reasoning: string | null;
  ev_bps: number | null;
};

type EvalsPayload = { evaluations: Evaluation[] };

type Suggestion = {
  id: number;
  created_at: string | null;
  param_name: string;
  current_value: number | null;
  proposed_value: number | null;
  evidence: string | null;
  status: string;
};

type SuggestionsPayload = { suggestions: Suggestion[] };

const KIND_COPY: Record<Kind, { label: string }> = {
  execute: { label: "Trade" },
  pass:    { label: "Pass" },
  resolve: { label: "Resolution" },
  update:  { label: "Signal" },
};

type Filter = "all" | Kind;

async function getJSON<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

function dayLabel(d: Date): string {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const ts = d.getTime();
  if (ts >= startOfToday) return "Today";
  if (ts >= startOfToday - 86_400_000) return "Yesterday";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function timeLabel(d: Date): string {
  return d.toLocaleTimeString("en-US", { hour12: false });
}

export default function ActivityPage() {
  const [filter, setFilter] = useState<Filter>("all");
  const [positions, setPositions] = useState<PositionsPayload | null>(null);
  const [evaluations, setEvals] = useState<EvalsPayload | null>(null);
  const [suggestions, setSuggestions] = useState<SuggestionsPayload | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [p, e, s] = await Promise.all([
        getJSON<PositionsPayload>("/api/positions"),
        getJSON<EvalsPayload>("/api/evaluations?limit=50"),
        getJSON<SuggestionsPayload>("/api/suggestions"),
      ]);
      if (cancelled) return;
      setPositions(p);
      setEvals(e);
      setSuggestions(s);
      setLoaded(true);
    };
    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const events: Event[] = useMemo(() => {
    const out: Event[] = [];

    for (const p of positions?.open ?? []) {
      if (!p.created_at) continue;
      const d = new Date(p.created_at);
      if (Number.isNaN(d.getTime())) continue;
      const entryCents = Math.round(p.entry_price * 100);
      const pWin = p.claude_probability != null ? p.claude_probability.toFixed(2) : "—";
      const conf = p.confidence != null ? p.confidence.toFixed(2) : "—";
      out.push({
        id: `open-${p.id}`,
        ts: d.getTime(),
        dateLabel: dayLabel(d),
        timeLabel: timeLabel(d),
        kind: "execute",
        title: `Opened ${p.side} · ${p.question}`,
        meta: `$${p.cost_usd.toFixed(0)} · ${entryCents}¢ · p_win ${pWin} · conf ${conf}`,
        detail: p.reasoning ?? undefined,
      });
    }

    for (const s of positions?.settled ?? []) {
      if (!s.settled_at) continue;
      const d = new Date(s.settled_at);
      if (Number.isNaN(d.getTime())) continue;
      const pnl = s.realized_pnl_usd ?? 0;
      const outcome = (s.settlement_outcome ?? "").toUpperCase();
      const win = pnl >= 0;
      const pnlStr = `${win ? "+" : ""}$${pnl.toFixed(2)}`;
      out.push({
        id: `settled-${s.id}`,
        ts: d.getTime(),
        dateLabel: dayLabel(d),
        timeLabel: timeLabel(d),
        kind: "resolve",
        title: `${win ? "Won" : "Lost"} ${s.side} · ${s.question}`,
        meta: `${outcome || "—"} · ${pnlStr}`,
      });
    }

    for (const e of evaluations?.evaluations ?? []) {
      const rec = (e.recommendation ?? "").toUpperCase();
      const traded = rec === "YES" || rec === "NO" || rec === "BUY";
      if (traded) continue;
      if (!e.evaluated_at) continue;
      const d = new Date(e.evaluated_at);
      if (Number.isNaN(d.getTime())) continue;
      const delfi = e.claude_probability != null ? Math.round(e.claude_probability * 100) : null;
      const market = e.market_price_yes != null ? Math.round(e.market_price_yes * 100) : null;
      out.push({
        id: `eval-${e.id}`,
        ts: d.getTime(),
        dateLabel: dayLabel(d),
        timeLabel: timeLabel(d),
        kind: "pass",
        title: `Passed · ${e.question}`,
        meta: `Delfi ${delfi != null ? `${delfi}¢` : "—"} · market ${market != null ? `${market}¢` : "—"} · ${rec || "SKIP"}`,
        detail: e.reasoning ?? undefined,
      });
    }

    for (const s of suggestions?.suggestions ?? []) {
      if (!s.created_at) continue;
      const d = new Date(s.created_at);
      if (Number.isNaN(d.getTime())) continue;
      const cur = s.current_value != null ? s.current_value.toString() : "—";
      const prop = s.proposed_value != null ? s.proposed_value.toString() : "—";
      out.push({
        id: `sugg-${s.id}`,
        ts: d.getTime(),
        dateLabel: dayLabel(d),
        timeLabel: timeLabel(d),
        kind: "update",
        title: `Suggestion · ${s.param_name} ${cur} → ${prop}`,
        meta: s.status,
        detail: s.evidence ?? undefined,
      });
    }

    out.sort((a, b) => b.ts - a.ts);
    return out;
  }, [positions, evaluations, suggestions]);

  const items = filter === "all" ? events : events.filter((a) => a.kind === filter);

  const byDate = items.reduce<Record<string, Event[]>>((acc, a) => {
    (acc[a.dateLabel] = acc[a.dateLabel] || []).push(a);
    return acc;
  }, {});

  const counts = useMemo(() => {
    const c: Record<Kind, number> = { execute: 0, pass: 0, resolve: 0, update: 0 };
    for (const e of events) c[e.kind] += 1;
    return c;
  }, [events]);

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Activity log</h1>
            <p className="page-sub">Every decision Delfi made, in order. Passes and resolutions included, not just trades.</p>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div className="page-toolbar-left">
          <button className={`chip ${filter === "all" ? "on" : ""}`} onClick={() => setFilter("all")}>
            All ({events.length})
          </button>
          <button className={`chip ${filter === "execute" ? "on" : ""}`} onClick={() => setFilter("execute")}>
            Trades ({counts.execute})
          </button>
          <button className={`chip ${filter === "resolve" ? "on" : ""}`} onClick={() => setFilter("resolve")}>
            Resolutions ({counts.resolve})
          </button>
          <button className={`chip ${filter === "pass" ? "on" : ""}`} onClick={() => setFilter("pass")}>
            Passes ({counts.pass})
          </button>
          <button className={`chip ${filter === "update" ? "on" : ""}`} onClick={() => setFilter("update")}>
            Signal updates ({counts.update})
          </button>
        </div>
      </div>

      {items.length === 0 ? (
        <div className="panel">
          <div className="empty-state">
            {loaded
              ? "No activity yet. Delfi will start populating this feed once it evaluates its first markets."
              : "Loading..."}
          </div>
        </div>
      ) : (
        Object.entries(byDate).map(([date, dayEvents]) => (
          <div className="panel" key={date}>
            <div className="panel-head">
              <h2 className="panel-title">{date}</h2>
              <span className="panel-meta">{dayEvents.length} events</span>
            </div>
            {dayEvents.map((e) => (
              <div className="split-row" key={e.id}>
                <div className="split-body" style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
                  <span className="mono" style={{ fontSize: 12, color: "var(--vellum-40)", minWidth: 80 }}>
                    {e.timeLabel}
                  </span>
                  <span className="pill" style={{ minWidth: 78, textAlign: "center" }}>
                    {KIND_COPY[e.kind].label}
                  </span>
                  <div style={{ flex: 1 }}>
                    <div className="split-title">{e.title}</div>
                    <div className="split-desc">{e.meta}</div>
                    {e.detail && (
                      <div className="split-desc" style={{ marginTop: 6, color: "var(--vellum-60)" }}>
                        {e.detail}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  );
}
