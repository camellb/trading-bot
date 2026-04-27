"use client";

import React, { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { getJSON } from "@/lib/fetch-json";
import { useViewMode } from "@/lib/view-mode";

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
  reasoning: string | null;
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
  sortKey: string;
  kind: "execute" | "pass" | "update" | "resolve" | "resolve-loss" | "scan";
  text: string;
  meta: string;
  tone: Tone;
};

type ResolutionItem = { q: string; in: string; you: string; conviction: number };
type RiskItem = { used: number; cap: number; label: string };

// Thin slice of /api/user-config used by the Risk today widget. Only
// the three limit fractions are needed - the full BotConfig shape lives
// in the Risk controls page.
type RiskConfig = {
  daily_loss_limit_pct: number;
  drawdown_halt_pct: number;
  dry_powder_reserve_pct: number;
};

type UserConfigPayload = { user_id: string; config: RiskConfig };

// Fallback when the user has not onboarded yet or the bot is cold.
// Mirrors the "balanced" preset from apps/web/app/onboarding/actions.ts
// so a freshly signed-up user sees sensible caps instead of hardcoded
// 10% / 40% / 100% that ignore their actual settings.
const RISK_CONFIG_FALLBACK: RiskConfig = {
  daily_loss_limit_pct:   0.10,
  drawdown_halt_pct:      0.40,
  dry_powder_reserve_pct: 0.20,
};

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
  // Past the resolution deadline but still status='open' means
  // Polymarket has not yet flipped closed=true (or our settler
  // has not yet swept). Show "resolving" instead of a misleading
  // "now" so the user knows the market is in the resolution
  // window, not still pending a future deadline.
  if (ms <= 0) return "resolving";
  const days = Math.round(ms / 86_400_000);
  if (days === 0) {
    const hours = Math.max(1, Math.round(ms / 3_600_000));
    return `${hours}h`;
  }
  return `${days}d`;
}

function evaluationsToActivity(
  evals: Evaluation[],
  open: OpenPosition[],
  settled: SettledPosition[],
): ActivityItem[] {
  // Buys come from the user's own pm_positions rows - authoritative and
  // survive the shared-evaluation visibility filter (evaluations are
  // user-scoped by join_time; positions are user-scoped by user_id, so a
  // position always shows even when its evaluation predates the user).
  const fromOpen: ActivityItem[] = open.slice(0, 8).map((p) => {
    const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
    const mYesPct   = Math.round(marketYes * 100);
    const dYesPct   = p.claude_probability != null ? `${Math.round(p.claude_probability * 100)}%` : "-";
    const dConfPct  = p.confidence != null ? `${Math.round(p.confidence * 100)}%` : "-";
    return {
      t: fmtTime(p.created_at),
      sortKey: p.created_at ?? "",
      kind: "execute",
      text: `Bought ${p.side} · ${p.question}`,
      meta: `$${p.cost_usd.toFixed(0)} · M YES ${mYesPct}% · D YES ${dYesPct} · D CONF ${dConfPct}${p.category ? ` · ${p.category}` : ""}`,
      tone: "gold",
    };
  });

  // Skipped evaluations only - traded rows are already surfaced as buys
  // with better metadata from the positions feed above.
  const fromEvals: ActivityItem[] = evals
    .filter((e) => {
      const rec = (e.recommendation ?? "").toUpperCase();
      return rec !== "YES" && rec !== "NO" && rec !== "BUY";
    })
    .slice(0, 8)
    .map((e) => {
      const dYesPct  = e.claude_probability != null ? `${Math.round(e.claude_probability * 100)}%` : "-";
      const mYesPct  = e.market_price_yes   != null ? `${Math.round(e.market_price_yes   * 100)}%` : "-";
      const dConfPct = e.confidence         != null ? `${Math.round(e.confidence         * 100)}%` : "-";
      return {
        t: fmtTime(e.evaluated_at),
        sortKey: e.evaluated_at ?? "",
        kind: "pass",
        text: `Skipped · ${e.question}`,
        meta: `M YES ${mYesPct} · D YES ${dYesPct} · D CONF ${dConfPct}${e.category ? ` · ${e.category}` : ""}`,
        tone: "muted",
      };
    });

  const fromSettled: ActivityItem[] = settled.slice(0, 4).map((s) => {
    const pnl = s.realized_pnl_usd ?? 0;
    const win = pnl >= 0;
    return {
      t: fmtTime(s.settled_at),
      sortKey: s.settled_at ?? "",
      kind: win ? "resolve" : "resolve-loss",
      text: `Closed ${win ? "WIN" : "LOSS"} · ${s.question}`,
      meta: `${win ? "+" : ""}$${pnl.toFixed(2)}`,
      tone: win ? "profit" : "muted",
    };
  });

  return [...fromOpen, ...fromEvals, ...fromSettled]
    .sort((a, b) => (a.sortKey < b.sortKey ? 1 : -1))
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

function buildRisk(
  summary: Summary,
  open: OpenPosition[],
  config: RiskConfig,
): {
  dailyLoss: RiskItem;
  drawdown: RiskItem;
  exposure: RiskItem;
} {
  const bankroll = summary.bankroll ?? summary.starting_cash ?? 0;
  const starting = summary.starting_cash ?? bankroll ?? 0;
  const exposure = open.reduce((s, p) => s + (p.cost_usd ?? 0), 0);

  const ddPct = starting > 0 ? Math.max(0, ((starting - bankroll) / starting) * 100) : 0;
  const dailyLoss = Math.max(0, -(summary.realized_pnl ?? 0));

  // Caps come straight from user_config (or the balanced-preset fallback
  // when the payload hasn't loaded yet). Previously the widget hardcoded
  // 10% / 40% / 100%, which is why moving the sliders in Risk controls
  // left this card unchanged.
  const dailyCap = Math.max(1, bankroll * config.daily_loss_limit_pct);
  const ddCapPct = config.drawdown_halt_pct * 100;
  const exposureCap = Math.max(
    1,
    bankroll * Math.max(0, 1 - config.dry_powder_reserve_pct),
  );

  return {
    dailyLoss: { used: Math.round(dailyLoss), cap: Math.round(dailyCap), label: "Daily loss cap" },
    drawdown:  { used: +ddPct.toFixed(1), cap: +ddCapPct.toFixed(0), label: "Drawdown" },
    exposure:  { used: Math.round(exposure), cap: Math.round(exposureCap), label: "Gross exposure" },
  };
}

// ---- The page ----------------------------------------------------------

export default function DashboardPage() {
  const [summary, setSummary]       = useState<Summary | null>(null);
  const [positions, setPositions]   = useState<PositionsPayload | null>(null);
  const [evaluations, setEvals]     = useState<EvaluationsPayload | null>(null);
  const [riskConfig, setRiskConfig] = useState<RiskConfig | null>(null);
  const [loaded, setLoaded]         = useState(false);
  const { version: viewModeVersion } = useViewMode();

  useEffect(() => {
    let cancelled = false;
    // Clear prior payloads so a mode switch doesn't flash stale data.
    // riskConfig is tenant-scoped, not mode-scoped, so we keep the prior
    // value to avoid flashing the fallback preset when mode toggles.
    setLoaded(false);
    setSummary(null);
    setPositions(null);
    setEvals(null);
    const load = async () => {
      const [s, p, e, cfg] = await Promise.all([
        getJSON<Summary>("/api/summary"),
        getJSON<PositionsPayload>("/api/positions"),
        getJSON<EvaluationsPayload>("/api/evaluations?limit=20"),
        getJSON<UserConfigPayload>("/api/user-config").catch(() => null),
      ]);
      if (cancelled) return;
      setSummary(s);
      setPositions(p);
      setEvals(e);
      if (cfg?.config) setRiskConfig(cfg.config);
      setLoaded(true);
    };
    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [viewModeVersion]);

  const open     = positions?.open ?? [];
  const settled  = positions?.settled ?? [];
  const evals    = evaluations?.evaluations ?? [];
  const activity = useMemo(() => evaluationsToActivity(evals, open, settled), [evals, open, settled]);
  const resolving = useMemo(() => openToResolutions(open), [open]);
  const risk      = useMemo(
    () => buildRisk(summary ?? ({} as Summary), open, riskConfig ?? RISK_CONFIG_FALLBACK),
    [summary, open, riskConfig],
  );

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
        realizedPnl={pnl}
        realizedPct={pnlPct}
        winRate={summary?.win_rate ?? null}
        closedTrades={summary?.settled_total ?? 0}
        loaded={loaded}
      />

      <div className="dash-grid">
        <section className="dash-card card-positions">
          <CardHead
            title="Open positions"
            meta={
              open.length === 0
                ? "0 active"
                : `${open.length} active · $${open.reduce((s, p) => s + (p.cost_usd || 0), 0).toFixed(0)} deployed`
            }
            href="/dashboard/positions"
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
            <Empty label={loaded ? "No activity yet - Delfi is scanning." : "Loading..."} />
          ) : (
            <ActivityFeed items={activity} />
          )}
        </section>

        <section className="dash-card card-risk">
          <CardHead title="Risk today" href="/dashboard/risk" linkLabel="Risk controls" />
          <RiskGauges risk={risk} />
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
          <CardHead title="This week" href="/dashboard/performance" />
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
  realizedPnl,
  realizedPct,
  winRate,
  closedTrades,
  loaded,
}: {
  mode: string;
  bankroll: number;
  realizedPnl: number;
  realizedPct: number;
  winRate: number | null;
  closedTrades: number;
  loaded: boolean;
}) {
  const isSim = mode === "simulation";
  // Render the sign explicitly so a loss never displays as a positive
  // number. The prior version used Math.abs(realizedPnl) + a conditional
  // "+" prefix, which stripped the sign on losses (e.g. -$12.17 rendered
  // as "$12.17"). Now: "-" for negative, "+" for positive, no sign for
  // exactly zero. Same rule applied to the percent.
  const pnlSign =
    realizedPnl > 0 ? "+" : realizedPnl < 0 ? "-" : "";
  const pctSign =
    realizedPct > 0 ? "+" : realizedPct < 0 ? "-" : "";
  const pnlTone = !loaded
    ? ""
    : realizedPnl > 0
      ? "profit"
      : realizedPnl < 0
        ? "loss"
        : "";
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
            <div className={`hero-delta-val t-num ${pnlTone}`}>
              {loaded ? (
                <>
                  {pnlSign}${Math.abs(realizedPnl).toFixed(2)}{" "}
                  <span className="hero-delta-pct">
                    {pctSign}{Math.abs(realizedPct).toFixed(2)}%
                  </span>
                </>
              ) : (
                "-"
              )}
            </div>
          </div>
          <div className="hero-delta-div"></div>
          <div className="hero-delta">
            <div className="hero-delta-label">Win rate</div>
            <div className="hero-delta-val t-num">
              {loaded && winRate != null ? `${Math.round(winRate * 100)}%` : "-"}
            </div>
          </div>
          <div className="hero-delta-div"></div>
          <div className="hero-delta">
            <div className="hero-delta-label">Closed trades</div>
            <div className="hero-delta-val t-num">
              {loaded ? `${closedTrades}` : "-"}
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
        <div>Closes</div>
        <div></div>
      </div>
      {positions.map((p) => {
        // Market's implied probability of YES regardless of chosen side.
        const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
        const mYesPct   = Math.round(marketYes * 100);
        const dYesPct   = p.claude_probability != null ? Math.round(p.claude_probability * 100) : null;
        const dConfPct  = p.confidence != null ? Math.round(p.confidence * 100) : null;
        const isOpen = expanded.has(p.id);
        const reasoning = (p.reasoning ?? "").trim();
        const polyUrl = p.slug ? `https://polymarket.com/market/${p.slug}` : null;
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
              <div className="pos-cat">{p.category || "-"}</div>
              <div className={`pos-side ${p.side === "YES" ? "yes" : "no"}`}>{p.side}</div>
              <div className="pos-num t-num">${p.cost_usd.toFixed(0)}</div>
              <div className="pos-num t-num">{mYesPct}%</div>
              <div className="pos-num t-num">{dYesPct != null ? `${dYesPct}%` : "-"}</div>
              <div className="pos-num t-num">{dConfPct != null ? `${dConfPct}%` : "-"}</div>
              <div className="pos-closes t-num">{daysFromNow(p.expected_resolution_at)}</div>
              <div className={`pos-chevron ${isOpen ? "open" : ""}`}>▸</div>
            </div>
            {isOpen && (
              <div className="pos-detail">
                <div className="pos-detail-grid">
                  <div>
                    <div className="pos-detail-kv-label">Opened</div>
                    <div className="pos-detail-kv-val">
                      {p.created_at ? new Date(p.created_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-"}
                    </div>
                  </div>
                  <div>
                    <div className="pos-detail-kv-label">Closes</div>
                    <div className="pos-detail-kv-val">
                      {p.expected_resolution_at ? new Date(p.expected_resolution_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-"}
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
                  <div className="pos-detail-reason-label">Delfi's reasoning</div>
                  {reasoning ? reasoning : "No reasoning recorded for this entry."}
                </div>
                {polyUrl && (
                  <a
                    className="pos-detail-link"
                    href={polyUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => e.stopPropagation()}
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
            <span className="up-metric">
              <span className="up-metric-label">Closes in</span>
              <span className="up-in t-num">{r.in}</span>
            </span>
            <span className="up-metric">
              <span className="up-metric-label">Your side</span>
              <span className="up-you">{r.you}</span>
            </span>
            {r.conviction > 0 && (
              <span className="up-metric">
                <span className="up-metric-label">Confidence</span>
                <span className="up-conv">
                  <span className="up-conv-bar">
                    <span style={{ width: r.conviction * 100 + "%" }}></span>
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
