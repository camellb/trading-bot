import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  BotState,
  EventLogRow,
  MarketEvaluation,
  PerformanceSummary,
  PMPosition,
} from "../api";

/**
 * Dashboard page (the desktop equivalent of /dashboard on the SaaS).
 *
 * Layout (top to bottom):
 *   1. Hero - bankroll + realized P&L + win rate + closed trades.
 *   2. Open positions - top 5, with the same metric chips the SaaS uses
 *      (M YES %, D YES %, D CONF %), expandable to reveal reasoning.
 *   3. Recent activity - mixed feed of buys (from positions), skips
 *      (from evaluations), and resolutions (from settled positions).
 *   4. Risk today - daily loss / drawdown / exposure gauges against the
 *      user's actual risk caps from /api/config.
 *   5. Resolving soon - next four positions sorted by
 *      expected_resolution_at.
 *
 * State is page-local. It refreshes every 15 seconds; trade-aware
 * sections (positions, summary) re-read on every refresh so the user
 * sees a buy land within ~15s of the engine recording it.
 */

type Risk = {
  daily_loss_limit_pct?: number;
  drawdown_halt_pct?: number;
  dry_powder_reserve_pct?: number;
};

interface Props {
  state: BotState | null;
  refresh: () => void;
}

export default function Dashboard({ state }: Props) {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [positions, setPositions] = useState<PMPosition[]>([]);
  const [events, setEvents] = useState<EventLogRow[]>([]);
  const [evaluations, setEvaluations] = useState<MarketEvaluation[]>([]);
  const [risk, setRisk] = useState<Risk>({});
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [s, p, e, ev, cfg] = await Promise.all([
        api.summary(),
        api.positions(50).then((r) => r.positions),
        api.events(40).then((r) => r.events),
        api.evaluations(20).then((r) => r.evaluations),
        api.config(),
      ]);
      setSummary(s);
      setPositions(p);
      setEvents(e);
      setEvaluations(ev);
      setRisk({
        daily_loss_limit_pct: numberOr(cfg.daily_loss_limit_pct, 0.10),
        drawdown_halt_pct: numberOr(cfg.drawdown_halt_pct, 0.40),
        dry_powder_reserve_pct: numberOr(cfg.dry_powder_reserve_pct, 0.20),
      });
      setLoaded(true);
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
    () => positions.filter((p) => p.status === "settled"),
    [positions],
  );
  const activity = useMemo(
    () => buildActivityFeed(open, settled, evaluations, events),
    [open, settled, evaluations, events],
  );
  const resolving = useMemo(() => buildResolving(open), [open]);

  const mode = summary?.mode ?? state?.mode ?? "simulation";
  const bankroll = summary?.bankroll ?? summary?.starting_cash ?? 0;
  const starting = summary?.starting_cash ?? 0;
  const pnl = summary?.realized_pnl ?? 0;
  const pnlPct = starting > 0 ? (pnl / starting) * 100 : 0;
  const settledTotal = summary?.settled_total ?? 0;
  const winRate = summary?.win_rate ?? null;

  return (
    <>
      <header className="page-header">
        <h1>Dashboard</h1>
      </header>

      {error && <div className="error">{error}</div>}

      {/* Hero */}
      <section className="hero reveal">
        <div className="hero-eq">
          <div className="hero-label">Balance</div>
          <div className="hero-value large">
            {loaded ? `$${bankroll.toFixed(2)}` : "-"}
          </div>
          <div className="hero-sub">{mode}</div>
        </div>
        <div className="hero-eq">
          <div className="hero-label">Realized P&amp;L</div>
          <div
            className={`hero-value ${pnl > 0 ? "profit" : pnl < 0 ? "ember" : ""}`}
          >
            {loaded ? fmtPnL(pnl) : "-"}
          </div>
          <div className="hero-sub">
            {loaded ? `${fmtPct(pnlPct, true)} on starting` : "-"}
          </div>
        </div>
        <div className="hero-eq">
          <div className="hero-label">Win rate</div>
          <div className="hero-value">
            {winRate !== null && winRate !== undefined
              ? `${(winRate * 100).toFixed(0)}%`
              : "-"}
          </div>
          <div className="hero-sub">{settledTotal} settled</div>
        </div>
        <div className="hero-eq">
          <div className="hero-label">Open positions</div>
          <div className="hero-value">{open.length}</div>
          <div className="hero-sub">
            ${open.reduce((s, p) => s + (p.cost_usd || 0), 0).toFixed(0)} deployed
          </div>
        </div>
        <div className="hero-eq">
          <div className="hero-label">Brier</div>
          <div className="hero-value">
            {summary?.brier != null ? summary.brier.toFixed(3) : "-"}
          </div>
          <div className="hero-sub">lower is better</div>
        </div>
      </section>

      {/* Two-column body */}
      <div className="grid-2">
        <section className="card">
          <h2 className="card-title">Open positions</h2>
          {open.length === 0 ? (
            <p className="empty">
              {loaded ? "No open positions yet." : "Loading..."}
            </p>
          ) : (
            <DashPositionsList positions={open.slice(0, 5)} />
          )}
        </section>

        <section className="card">
          <h2 className="card-title">Recent activity</h2>
          {activity.length === 0 ? (
            <p className="empty">
              {loaded ? "No activity yet. Delfi is scanning." : "Loading..."}
            </p>
          ) : (
            <ActivityFeed items={activity.slice(0, 10)} />
          )}
        </section>

        <section className="card">
          <h2 className="card-title">Risk today</h2>
          <RiskGauges
            risk={risk}
            bankroll={bankroll}
            starting={starting}
            realizedPnl={pnl}
            exposure={open.reduce((s, p) => s + (p.cost_usd || 0), 0)}
          />
        </section>

        <section className="card">
          <h2 className="card-title">Resolving soon</h2>
          {resolving.length === 0 ? (
            <p className="empty">
              {loaded ? "No positions resolving soon." : "Loading..."}
            </p>
          ) : (
            <ul className="reports">
              {resolving.map((r) => (
                <li key={r.id}>
                  <div className="report-head">
                    <span>closes in {r.in}</span>
                    <span>
                      {r.side} {r.dyes != null ? `${r.dyes}%` : ""}
                    </span>
                  </div>
                  <p className="report-thesis">{r.question}</p>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}

// ── Open positions list (compact, with per-row chips + expand) ─────────

function DashPositionsList({ positions }: { positions: PMPosition[] }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="positions-list">
      {positions.map((p) => {
        const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
        const mYesPct = Math.round(marketYes * 100);
        const dYesPct =
          p.claude_probability != null
            ? Math.round(p.claude_probability * 100)
            : null;
        const dConfPct =
          p.confidence != null ? Math.round(p.confidence * 100) : null;
        const isOpen = expanded.has(p.id);
        return (
          <React.Fragment key={p.id}>
            <div
              className={`position-row ${isOpen ? "expanded" : ""}`}
              onClick={() => toggle(p.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  toggle(p.id);
                }
              }}
              style={{
                gridTemplateColumns: "1fr auto auto auto auto auto",
              }}
            >
              <div className="position-q">
                <span className="q-text">{p.question}</span>
                {p.market_archetype && (
                  <span className="q-meta">{p.market_archetype}</span>
                )}
              </div>
              <span className={`side-chip ${p.side}`}>{p.side}</span>
              <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
                ${p.cost_usd.toFixed(0)}
              </span>
              <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
                M {mYesPct}%
              </span>
              <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
                D {dYesPct != null ? `${dYesPct}%` : "-"}
              </span>
              <span className="t-num" style={{ fontSize: 12, color: "var(--vellum-60)" }}>
                {dConfPct != null ? `${dConfPct}%` : "-"}
              </span>
            </div>
            {isOpen && (
              <div className="position-detail">
                <div className="grid-2" style={{ marginBottom: 10 }}>
                  <div>
                    <div className="hero-label">Entry</div>
                    <div className="t-num">{p.entry_price.toFixed(3)}</div>
                  </div>
                  <div>
                    <div className="hero-label">Shares</div>
                    <div className="t-num">{p.shares.toFixed(2)}</div>
                  </div>
                </div>
                <p style={{ margin: 0 }}>
                  {p.reasoning && p.reasoning.trim()
                    ? p.reasoning.trim()
                    : "No reasoning recorded for this entry."}
                </p>
              </div>
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

// ── Activity feed (mixed: buys + skips + settles) ──────────────────────

type ActivityItem = {
  ts: string;
  kind: "execute" | "pass" | "resolve" | "loss" | "scan";
  text: string;
  meta: string;
  tone: "gold" | "muted" | "profit" | "ember";
};

function buildActivityFeed(
  open: PMPosition[],
  settled: PMPosition[],
  evals: MarketEvaluation[],
  events: EventLogRow[],
): ActivityItem[] {
  const items: ActivityItem[] = [];

  for (const p of open.slice(0, 8)) {
    const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
    const mYes = Math.round(marketYes * 100);
    const dYes = p.claude_probability != null
      ? `${Math.round(p.claude_probability * 100)}%`
      : "-";
    items.push({
      ts: p.created_at ?? "",
      kind: "execute",
      text: `Bought ${p.side} - ${p.question.slice(0, 80)}`,
      meta: `$${p.cost_usd.toFixed(0)} | M YES ${mYes}% | D YES ${dYes}${p.category ? ` | ${p.category}` : ""}`,
      tone: "gold",
    });
  }

  for (const e of evals) {
    const rec = (e.recommendation ?? "").toUpperCase();
    if (rec === "YES" || rec === "NO" || rec === "BUY") continue;
    const dYes = e.claude_probability != null
      ? `${Math.round(e.claude_probability * 100)}%`
      : "-";
    const mYes = e.market_price_yes != null
      ? `${Math.round(e.market_price_yes * 100)}%`
      : "-";
    items.push({
      ts: e.evaluated_at,
      kind: "pass",
      text: `Skipped - ${e.question.slice(0, 80)}`,
      meta: `M YES ${mYes} | D YES ${dYes}${e.category ? ` | ${e.category}` : ""}`,
      tone: "muted",
    });
    if (items.length >= 16) break;
  }

  for (const s of settled.slice(0, 4)) {
    const pnl = s.realized_pnl_usd ?? 0;
    const win = pnl >= 0;
    items.push({
      ts: s.settled_at ?? "",
      kind: win ? "resolve" : "loss",
      text: `Closed ${win ? "WIN" : "LOSS"} - ${s.question.slice(0, 80)}`,
      meta: `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`,
      tone: win ? "profit" : "ember",
    });
  }

  // Synthesize scan markers from event log so a freshly running engine
  // shows _something_ even when no trades fired.
  for (const e of events.slice(0, 4)) {
    if (e.event_type !== "polymarket_scan_complete") continue;
    items.push({
      ts: e.timestamp,
      kind: "scan",
      text: e.description,
      meta: e.source,
      tone: "muted",
    });
  }

  return items
    .filter((i) => i.ts)
    .sort((a, b) => (a.ts < b.ts ? 1 : -1));
}

const KIND_MARK: Record<ActivityItem["kind"], string> = {
  execute: "◆",
  pass: "-",
  resolve: "✓",
  loss: "✕",
  scan: "·",
};

function ActivityFeed({ items }: { items: ActivityItem[] }) {
  return (
    <ul className="events">
      {items.map((a, i) => (
        <li key={i}>
          <span className="ts">
            {new Date(a.ts).toLocaleTimeString(undefined, {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </span>
          <span className={`evt sev-${a.tone === "ember" ? 2 : 0}`}>
            {KIND_MARK[a.kind]} {a.kind}
          </span>
          <span className="src">{a.meta.split("|")[0].trim()}</span>
          <span className="desc" title={a.text}>
            {a.text}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ── Risk gauges (3 bars: daily loss / drawdown / exposure) ─────────────

function RiskGauges({
  risk,
  bankroll,
  starting,
  realizedPnl,
  exposure,
}: {
  risk: Risk;
  bankroll: number;
  starting: number;
  realizedPnl: number;
  exposure: number;
}) {
  const dailyLoss = Math.max(0, -realizedPnl);
  const dailyCap = Math.max(1, bankroll * (risk.daily_loss_limit_pct ?? 0.10));
  const ddPct = starting > 0
    ? Math.max(0, ((starting - bankroll) / starting) * 100)
    : 0;
  const ddCapPct = (risk.drawdown_halt_pct ?? 0.40) * 100;
  const exposureCap = Math.max(
    1,
    bankroll * Math.max(0, 1 - (risk.dry_powder_reserve_pct ?? 0.20)),
  );

  const rows = [
    { label: "Daily loss", used: dailyLoss, cap: dailyCap, unit: "$" },
    { label: "Drawdown",   used: ddPct,     cap: ddCapPct, unit: "%" },
    { label: "Exposure",   used: exposure,  cap: exposureCap, unit: "$" },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {rows.map((r) => {
        const pct = Math.min(100, (r.used / r.cap) * 100);
        const tone = pct > 75 ? "ember" : pct > 50 ? "warn" : "teal";
        return (
          <div key={r.label}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                marginBottom: 6,
                fontSize: 12,
              }}
            >
              <span style={{ color: "var(--vellum-60)" }}>{r.label}</span>
              <span
                className="t-num"
                style={{ color: `var(--${tone === "ember" ? "ember" : tone === "warn" ? "warn" : "vellum"})` }}
              >
                {r.unit === "$" ? `$${Math.round(r.used).toLocaleString()}` : `${r.used.toFixed(1)}%`}
                <span style={{ color: "var(--vellum-40)" }}>
                  &nbsp;/ {r.unit === "$" ? `$${Math.round(r.cap).toLocaleString()}` : `${r.cap.toFixed(0)}%`}
                </span>
              </span>
            </div>
            <div
              style={{
                height: 4,
                background: "var(--surface-3)",
                borderRadius: 2,
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  height: "100%",
                  width: `${pct}%`,
                  background: `var(--${tone === "ember" ? "ember" : tone === "warn" ? "warn" : "teal"})`,
                  transition: "width 400ms",
                }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Resolving-soon helpers ─────────────────────────────────────────────

type Resolving = {
  id: number;
  question: string;
  in: string;
  side: string;
  dyes: number | null;
};

function buildResolving(open: PMPosition[]): Resolving[] {
  return open
    .filter((p) => p.expected_resolution_at)
    .sort((a, b) => {
      const ta = new Date(a.expected_resolution_at!).getTime();
      const tb = new Date(b.expected_resolution_at!).getTime();
      return ta - tb;
    })
    .slice(0, 4)
    .map((p) => ({
      id: p.id,
      question: p.question,
      in: daysFromNow(p.expected_resolution_at!),
      side: p.side,
      dyes: p.claude_probability != null
        ? Math.round(p.claude_probability * 100)
        : null,
    }));
}

// ── Format helpers ─────────────────────────────────────────────────────

function fmtPnL(v: number): string {
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}

function fmtPct(v: number, signed = false): string {
  if (!Number.isFinite(v)) return "-";
  const sign = signed ? (v > 0 ? "+" : v < 0 ? "-" : "") : "";
  return `${sign}${Math.abs(v).toFixed(2)}%`;
}

function numberOr(v: unknown, fallback: number): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function daysFromNow(iso: string): string {
  const d = new Date(iso).getTime();
  if (!Number.isFinite(d)) return "-";
  const ms = d - Date.now();
  if (ms <= 0) return "resolving";
  const days = Math.round(ms / 86_400_000);
  if (days === 0) {
    const hours = Math.max(1, Math.round(ms / 3_600_000));
    return `${hours}h`;
  }
  return `${days}d`;
}
