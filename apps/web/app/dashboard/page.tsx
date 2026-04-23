"use client";

import React, { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { getJSON } from "@/lib/fetch-json";

// ---- Shapes coming off the bot API (see apps/bot/bot_api.py) -----------

type Summary = {
  mode: "simulation" | "live" | string | null;
  bankroll: number | null;
  equity: number | null;
  starting_cash: number | null;
  open_positions: number | null;
  open_cost: number | null;
  settled_total: number | null;
  settled_wins: number | null;
  win_rate: number | null;
  realized_pnl: number | null;
  brier: number | null;
  resolved_predictions: number | null;
  total_predictions: number | null;
  test_end: string | null;
};

type OpenPosition = {
  id: number;
  market_id: string;
  question: string;
  category: string | null;
  side: "YES" | "NO";
  shares: number;
  entry_price: number;
  cost_usd: number;
  claude_probability: number | null;
  ev_bps: number | null;
  confidence: number | null;
  expected_resolution_at: string | null;
  created_at: string | null;
  slug: string | null;
};

type SettledPosition = {
  id: number;
  question: string;
  side: "YES" | "NO";
  cost_usd: number;
  claude_probability: number | null;
  settlement_outcome: string | null;
  realized_pnl_usd: number | null;
  settled_at: string | null;
};

type PositionsPayload = { open: OpenPosition[]; settled: SettledPosition[] };

type Evaluation = {
  id: number;
  evaluated_at: string | null;
  question: string;
  category: string | null;
  market_price_yes: number | null;
  claude_probability: number | null;
  confidence: number | null;
  ev_bps: number | null;
  recommendation: string | null;
  slug: string | null;
};

type EvaluationsPayload = { evaluations: Evaluation[] };

// ---- UI shape adapters --------------------------------------------------

type Tone = "gold" | "muted" | "teal" | "profit";
type ActivityItem = {
  t: string;
  kind: "execute" | "pass" | "update" | "resolve" | "resolve-loss" | "scan";
  text: string;
  meta: string;
  tone: Tone;
};

type ResolutionItem = { q: string; in: string; you: string; conviction: number };
type RiskItem = { used: number; cap: number; label: string };

const ACT_ICON: Record<ActivityItem["kind"], string> = {
  execute: "◆",
  pass: "–",
  update: "~",
  resolve: "✓",
  "resolve-loss": "✕",
  scan: "·",
};

function fmtTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function daysFromNow(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return "-";
  const ms = d - Date.now();
  if (ms <= 0) return "now";
  const days = Math.round(ms / 86_400_000);
  if (days === 0) {
    const hours = Math.max(1, Math.round(ms / 3_600_000));
    return `${hours}h`;
  }
  return `${days}d`;
}

function evaluationsToActivity(evals: Evaluation[], settled: SettledPosition[]): ActivityItem[] {
  const fromEvals: ActivityItem[] = evals.slice(0, 8).map((e) => {
    const rec = (e.recommendation ?? "").toUpperCase();
    const traded = rec === "YES" || rec === "NO" || rec === "BUY";
    const kind: ActivityItem["kind"] = traded ? "execute" : "pass";
    const pWin = e.claude_probability != null ? e.claude_probability.toFixed(2) : "-";
    const conf = e.confidence != null ? e.confidence.toFixed(2) : "-";
    const pre = traded ? `Opened ${rec} · ` : "Passed · ";
    return {
      t: fmtTime(e.evaluated_at),
      kind,
      text: `${pre}${e.question}`,
      meta: `p_win ${pWin} · conf ${conf}${e.category ? ` · ${e.category}` : ""}`,
      tone: traded ? "gold" : "muted",
    };
  });

  const fromSettled: ActivityItem[] = settled.slice(0, 4).map((s) => {
    const pnl = s.realized_pnl_usd ?? 0;
    const win = pnl >= 0;
    return {
      t: fmtTime(s.settled_at),
      kind: win ? "resolve" : "resolve-loss",
      text: `Resolved ${win ? "WIN" : "LOSS"} · ${s.question}`,
      meta: `${win ? "+" : ""}$${pnl.toFixed(2)}`,
      tone: win ? "profit" : "muted",
    };
  });

  return [...fromEvals, ...fromSettled]
    .sort((a, b) => (a.t < b.t ? 1 : -1))
    .slice(0, 10);
}

function openToResolutions(open: OpenPosition[]): ResolutionItem[] {
  return open
    .filter((p) => p.expected_resolution_at)
    .sort((a, b) => {
      const ta = new Date(a.expected_resolution_at!).getTime();
      const tb = new Date(b.expected_resolution_at!).getTime();
      return ta - tb;
    })
    .slice(0, 4)
    .map((p) => {
      const pct =
        p.claude_probability != null ? Math.round(p.claude_probability * 100) : null;
      return {
        q: p.question,
        in: daysFromNow(p.expected_resolution_at),
        you: pct != null ? `${p.side} ${pct}%` : p.side,
        conviction: p.confidence ?? 0,
      };
    });
}

function buildRisk(summary: Summary, open: OpenPosition[]): {
  dailyLoss: RiskItem;
  drawdown: RiskItem;
  exposure: RiskItem;
} {
  const bankroll = summary.bankroll ?? summary.starting_cash ?? 0;
  const starting = summary.starting_cash ?? bankroll ?? 0;
  const exposure = open.reduce((s, p) => s + (p.cost_usd ?? 0), 0);

  const ddPct = starting > 0 ? Math.max(0, ((starting - bankroll) / starting) * 100) : 0;
  const dailyLoss = Math.max(0, -(summary.realized_pnl ?? 0));
  const dailyCap = starting * 0.1;

  return {
    dailyLoss: { used: Math.round(dailyLoss), cap: Math.round(dailyCap), label: "Daily loss cap" },
    drawdown:  { used: +ddPct.toFixed(1), cap: 40, label: "Drawdown" },
    exposure:  { used: Math.round(exposure), cap: Math.round(starting || 1), label: "Gross exposure" },
  };
}

// ---- The page ----------------------------------------------------------

export default function DashboardPage() {
  const [summary, setSummary]       = useState<Summary | null>(null);
  const [positions, setPositions]   = useState<PositionsPayload | null>(null);
  const [evaluations, setEvals]     = useState<EvaluationsPayload | null>(null);
  const [loaded, setLoaded]         = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [s, p, e] = await Promise.all([
        getJSON<Summary>("/api/summary"),
        getJSON<PositionsPayload>("/api/positions"),
        getJSON<EvaluationsPayload>("/api/evaluations?limit=20"),
      ]);
      if (cancelled) return;
      setSummary(s);
      setPositions(p);
      setEvals(e);
      setLoaded(true);
    };
    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const open     = positions?.open ?? [];
  const settled  = positions?.settled ?? [];
  const evals    = evaluations?.evaluations ?? [];
  const activity = useMemo(() => evaluationsToActivity(evals, settled), [evals, settled]);
  const resolving = useMemo(() => openToResolutions(open), [open]);
  const risk      = useMemo(() => buildRisk(summary ?? ({} as Summary), open), [summary, open]);

  const mode     = summary?.mode ?? "simulation";
  const bankroll = summary?.bankroll ?? summary?.starting_cash ?? 0;
  const starting = summary?.starting_cash ?? 0;
  const pnl      = summary?.realized_pnl ?? 0;
  const pnlPct   = starting > 0 ? (pnl / starting) * 100 : 0;

  return (
    <div className="dash">
      <DashHero
        mode={mode}
        bankroll={bankroll}
        starting={starting}
        realizedPnl={pnl}
        realizedPct={pnlPct}
        loaded={loaded}
      />

      <div className="dash-grid">
        <section className="dash-card card-positions">
          <CardHead
            title="Open positions"
            meta={`${open.length} active`}
            href="/dashboard/positions"
          />
          {open.length === 0 ? (
            <Empty label={loaded ? "No open positions yet." : "Loading..."} />
          ) : (
            <PositionsTable positions={open.slice(0, 5)} />
          )}
        </section>

        <section className="dash-card card-activity">
          <CardHead title="Recent activity" href="/dashboard/activity" />
          {activity.length === 0 ? (
            <Empty label={loaded ? "No activity yet - Delfi is scanning." : "Loading..."} />
          ) : (
            <ActivityFeed items={activity} />
          )}
        </section>

        <section className="dash-card card-risk">
          <CardHead title="Risk today" meta="Delfi's guardrails" href="/dashboard/risk" linkLabel="Risk controls" />
          <RiskGauges risk={risk} />
        </section>

        <section className="dash-card card-upcoming">
          <CardHead title="Resolving soon" meta="Next 30 days" />
          {resolving.length === 0 ? (
            <Empty label={loaded ? "No positions resolving soon." : "Loading..."} />
          ) : (
            <UpcomingList items={resolving} />
          )}
        </section>

        <section className="dash-card card-summary">
          <CardHead title="This week" meta="Performance snapshot" href="/dashboard/performance" />
          <SummaryCard
            brier={summary?.brier ?? null}
            winRate={summary?.win_rate ?? null}
            settled={summary?.settled_total ?? 0}
          />
        </section>
      </div>
    </div>
  );
}

function DashHero({
  mode,
  bankroll,
  starting,
  realizedPnl,
  realizedPct,
  loaded,
}: {
  mode: string;
  bankroll: number;
  starting: number;
  realizedPnl: number;
  realizedPct: number;
  loaded: boolean;
}) {
  const isSim   = mode === "simulation";
  const pnlSign = realizedPnl >= 0 ? "+" : "";
  return (
    <section className="dash-hero">
      <div className="hero-balance">
        <div className="hero-balance-head">
          <div className="hero-balance-label">Balance</div>
          <div className={`hero-balance-mode ${isSim ? "sim" : "live"}`}>
            {isSim ? "Simulation" : "Live"}
          </div>
        </div>
        <div className="hero-balance-value t-num">
          {loaded ? (
            <>
              <span className="hero-balance-cur">$</span>
              {bankroll.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </>
          ) : (
            <span className="hero-balance-loading">-</span>
          )}
        </div>
        <div className="hero-deltas">
          <div className="hero-delta">
            <div className="hero-delta-label">Realized P&amp;L</div>
            <div className={`hero-delta-val t-num ${loaded && realizedPnl >= 0 ? "profit" : ""}`}>
              {loaded ? (
                <>
                  {pnlSign}${Math.abs(realizedPnl).toFixed(2)}{" "}
                  <span className="hero-delta-pct">
                    {pnlSign}{realizedPct.toFixed(2)}%
                  </span>
                </>
              ) : (
                "-"
              )}
            </div>
          </div>
          <div className="hero-delta-div"></div>
          <div className="hero-delta">
            <div className="hero-delta-label">Started at</div>
            <div className="hero-delta-val t-num">
              {loaded ? `$${starting.toLocaleString("en-US", { minimumFractionDigits: 0 })}` : "-"}
            </div>
          </div>
        </div>
      </div>

      <div className="hero-chart">
        <div className="hero-chart-head">
          <div className="hero-chart-label">Equity history</div>
        </div>
        <div className="hero-chart-placeholder">
          Equity time series wiring pending - daily snapshots will appear here
          as the bot records performance_snapshots.
        </div>
      </div>
    </section>
  );
}

function CardHead({
  title,
  meta,
  href,
  live,
  linkLabel,
}: {
  title: string;
  meta?: string;
  href?: string;
  live?: boolean;
  linkLabel?: string;
}) {
  return (
    <div className="card-head">
      <div className="card-head-left">
        <h3 className="card-title">{title}</h3>
        {meta && (
          <span className="card-meta">
            {live && <span className="card-live-dot"></span>}
            {meta}
          </span>
        )}
      </div>
      {href && (
        <Link className="card-head-link" href={href}>
          {linkLabel ?? "View all"} →
        </Link>
      )}
    </div>
  );
}

function PositionsTable({ positions }: { positions: OpenPosition[] }) {
  return (
    <div className="pos-table">
      <div className="pos-row head">
        <div>Market</div>
        <div>Side</div>
        <div>Entry</div>
        <div>Size</div>
        <div>p_win</div>
        <div>Closes</div>
      </div>
      {positions.map((p) => {
        const entryCents = Math.round(p.entry_price * 100);
        const pWin = p.claude_probability != null ? Math.round(p.claude_probability * 100) : null;
        return (
          <div className="pos-row" key={p.id}>
            <div className="pos-q">{p.question}</div>
            <div className={`pos-side ${p.side === "YES" ? "yes" : "no"}`}>{p.side}</div>
            <div className="pos-num t-num">
              <span className="pos-entry">{entryCents}¢</span>
            </div>
            <div className="pos-num t-num">${p.cost_usd.toFixed(0)}</div>
            <div className="pos-num t-num">{pWin != null ? `${pWin}%` : "-"}</div>
            <div className="pos-closes t-num">{daysFromNow(p.expected_resolution_at)}</div>
          </div>
        );
      })}
    </div>
  );
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

function RiskGauges({
  risk,
}: {
  risk: { dailyLoss: RiskItem; drawdown: RiskItem; exposure: RiskItem };
}) {
  const items: { key: string; unit: "$" | "%"; item: RiskItem }[] = [
    { key: "dailyLoss", unit: "$", item: risk.dailyLoss },
    { key: "drawdown",  unit: "%", item: risk.drawdown  },
    { key: "exposure",  unit: "$", item: risk.exposure  },
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
              <div className={`risk-bar-fill tone-${tone}`} style={{ width: pct + "%" }}></div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function UpcomingList({ items }: { items: ResolutionItem[] }) {
  return (
    <ul className="up-list">
      {items.map((r, i) => (
        <li className="up-row" key={i}>
          <div className="up-q">{r.q}</div>
          <div className="up-meta">
            <span className="up-in t-num">{r.in}</span>
            <span className="up-you">{r.you}</span>
            {r.conviction > 0 && (
              <span className="up-conv">
                <span className="up-conv-bar">
                  <span style={{ width: r.conviction * 100 + "%" }}></span>
                </span>
                <span className="up-conv-pct t-num">{Math.round(r.conviction * 100)}%</span>
              </span>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function SummaryCard({
  brier,
  winRate,
  settled,
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
      ? "Delfi is scanning markets and will begin opening positions when forecasts meet the minimum confidence gate. Your performance snapshot will fill in here as trades settle."
      : "Live performance measured across all settled markets. See the Performance page for category breakdowns, calibration, and Brier trend.";
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
  return <div className="dash-empty-inline">{label}</div>;
}
