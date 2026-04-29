import React, { useCallback, useEffect, useMemo, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  api,
  BotState,
  MarketEvaluation,
  PerformanceSummary,
  PMPosition,
} from "../api";
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

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function daysFromNow(iso: string | null | undefined): string {
  if (!iso) return "-";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "-";
  const ms = t - Date.now();
  if (ms <= 0) return "resolving";
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

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [s, p, ev, cfg] = await Promise.all([
        api.summary(),
        api.positions(50).then((r) => r.positions),
        api.evaluations(25).then((r) => r.evaluations),
        api.config(),
      ]);
      setSummary(s);
      setPositions(p);
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
    () => positions.filter((p) => p.status === "settled" || p.status === "closed"),
    [positions],
  );

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
  const pnl = summary?.realized_pnl ?? 0;
  const pnlPct = starting > 0 ? (pnl / starting) * 100 : 0;
  const winRate = summary?.win_rate ?? null;
  const closed = summary?.settled_total ?? 0;
  const brier = summary?.brier ?? null;

  const deployed = open.reduce((s, p) => s + (p.cost_usd || 0), 0);

  return (
    <div className="dash">
      {error && <div className="error">{error}</div>}

      <DashHero
        mode={mode}
        bankroll={bankroll}
        realizedPnl={pnl}
        realizedPct={pnlPct}
        winRate={winRate}
        closedTrades={closed}
        loaded={loaded}
      />

      <div className="dash-grid">
        <section className="dash-card card-positions">
          <CardHead
            title="Open positions"
            meta={
              open.length === 0
                ? "0 active"
                : `${open.length} active · $${deployed.toFixed(0)} deployed`
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
            onLink={() => goto("settings", "risk")}
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
  mode, bankroll, realizedPnl, realizedPct, winRate, closedTrades, loaded,
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
  const pnlSign = realizedPnl > 0 ? "+" : realizedPnl < 0 ? "-" : "";
  const pctSign = realizedPct > 0 ? "+" : realizedPct < 0 ? "-" : "";
  const pnlTone = !loaded ? "" : realizedPnl > 0 ? "profit" : realizedPnl < 0 ? "loss" : "";
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
          ) : "-"}
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
        </div>
      </div>

      <div className="hero-chart">
        <div className="hero-chart-head">
          <div className="hero-chart-label">Equity history</div>
        </div>
        <div className="hero-chart-placeholder">
          Daily snapshots will appear here as trades settle.
        </div>
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
        <div>Closes</div>
        <div />
      </div>
      {positions.map((p) => {
        const marketYes = p.side === "YES" ? p.entry_price : 1 - p.entry_price;
        const mYesPct = Math.round(marketYes * 100);
        const cp = (p.claude_probability as number | null | undefined) ?? null;
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
              <div className="pos-closes t-num">{daysFromNow(closesAt)}</div>
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
                      {closesAt ? new Date(closesAt).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-"}
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
    const cp = (p.claude_probability as number | null | undefined) ?? null;
    const cf = (p.confidence as number | null | undefined) ?? null;
    const dYesPct = cp != null ? `${Math.round(cp * 100)}%` : "-";
    const dConfPct = cf != null ? `${Math.round(cf * 100)}%` : "-";
    const cat = (p.category as string | null | undefined) ?? null;
    return {
      t: fmtTime(p.created_at),
      sortKey: p.created_at ?? "",
      kind: "execute",
      text: `Bought ${p.side} · ${p.question}`,
      meta: `$${p.cost_usd.toFixed(0)} · M ${mYesPct}% · D ${dYesPct} · CONF ${dConfPct}${cat ? ` · ${cat}` : ""}`,
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
      const dYesPct = e.claude_probability != null ? `${Math.round(e.claude_probability * 100)}%` : "-";
      const mYesPct = e.market_price_yes != null ? `${Math.round(e.market_price_yes * 100)}%` : "-";
      const dConfPct = e.confidence != null ? `${Math.round(e.confidence * 100)}%` : "-";
      return {
        t: fmtTime(e.evaluated_at),
        sortKey: e.evaluated_at ?? "",
        kind: "pass",
        text: `Skipped · ${e.question}`,
        meta: `M ${mYesPct} · D ${dYesPct} · CONF ${dConfPct}${e.category ? ` · ${e.category}` : ""}`,
        tone: "muted",
      };
    });

  const fromSettled: ActivityItem[] = settled.slice(0, 6).map((s) => {
    const pnl = (s.realized_pnl_usd as number | null | undefined) ?? 0;
    const win = pnl >= 0;
    return {
      t: fmtTime((s.settled_at as string | null | undefined) ?? null),
      sortKey: (s.settled_at as string | null | undefined) ?? "",
      kind: win ? "resolve" : "resolve-loss",
      text: `Closed ${win ? "WIN" : "LOSS"} · ${s.question}`,
      meta: `${win ? "+" : ""}$${pnl.toFixed(2)}`,
      tone: win ? "profit" : "ember",
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
  const exposure = open.reduce((s, p) => s + (p.cost_usd ?? 0), 0);
  const ddPct = starting > 0 ? Math.max(0, ((starting - bankroll) / starting) * 100) : 0;
  const dailyLoss = Math.max(0, -((summary?.realized_pnl ?? 0)));
  const dailyCap = Math.max(1, bankroll * (config.daily_loss_limit_pct ?? 0.10));
  const ddCapPct = (config.drawdown_halt_pct ?? 0.40) * 100;
  // Gross exposure cap is a HARD limit, not a moving one. Computed
  // off starting cash * (1 - dry_powder_reserve) so it doesn't shrink
  // as positions accumulate. Matches the risk_manager check that
  // halts new trades when open_cost crosses this cap.
  const exposureCap = Math.max(
    1,
    starting * Math.max(0, 1 - (config.dry_powder_reserve_pct ?? 0.20)),
  );
  return {
    dailyLoss: { used: Math.round(dailyLoss), cap: Math.round(dailyCap), label: "Daily loss cap" },
    drawdown:  { used: +ddPct.toFixed(1), cap: +ddCapPct.toFixed(0), label: "Drawdown" },
    exposure:  { used: Math.round(exposure), cap: Math.round(exposureCap), label: "Gross exposure" },
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
      const cp = (p.claude_probability as number | null | undefined) ?? null;
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
