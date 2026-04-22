"use client";

import React from "react";
import Link from "next/link";

type Position = {
  q: string;
  side: "YES" | "NO";
  entry: number;
  mark: number;
  size: number;
  pnl: number;
  forecastPct: number;
  closes: string;
};

type Activity = {
  t: string;
  kind: "execute" | "pass" | "update" | "resolve" | "scan";
  text: string;
  meta: string;
  tone: "gold" | "muted" | "teal" | "profit";
};

type Resolution = { q: string; in: string; you: string; conviction: number };

type RiskItem = { used: number; cap: number; label: string };

const DEMO = {
  portfolio: {
    value: 14827.42,
    deltaDay: 312.8,
    deltaDayPct: 2.15,
    deltaWeek: 1204.15,
    deltaWeekPct: 8.83,
    startValue: 10000,
  },
  equity: [
    10000, 10120, 10098, 10220, 10310, 10280, 10440, 10510, 10490, 10600, 10780,
    10840, 10920, 11020, 10990, 11150, 11240, 11190, 11330, 11480, 11560, 11620,
    11790, 11850, 11940, 12080, 12220, 12310, 12470, 12590, 12680, 12820, 12960,
    13080, 13210, 13340, 13480, 13600, 13750, 13860, 13990, 14120, 14240, 14380,
    14510, 14620, 14740, 14820, 14827, 14827.42,
  ],
  positions: [
    { q: "Fed cuts rates by 25bp in December?", side: "YES", entry: 44, mark: 52, size: 420, pnl: 71.4, forecastPct: 78, closes: "18d" },
    { q: "BTC closes above $120k by Dec 31?", side: "NO", entry: 38, mark: 33, size: 260, pnl: 34.2, forecastPct: 74, closes: "42d" },
    { q: "GPT-5 released in Q1 2026?", side: "YES", entry: 61, mark: 68, size: 180, pnl: 20.8, forecastPct: 72, closes: "71d" },
    { q: "US GDP Q4 > 2.5% advance est?", side: "NO", entry: 55, mark: 48, size: 140, pnl: 9.8, forecastPct: 68, closes: "9d" },
    { q: "Taylor Swift tour extension announced?", side: "YES", entry: 29, mark: 27, size: 80, pnl: -1.6, forecastPct: 66, closes: "5d" },
  ] as Position[],
  activity: [
    { t: "14:02:17", kind: "execute", text: "Opened YES position · Fed rate cut Dec", meta: "$420 · p_win 0.78 · conf 0.81", tone: "gold" },
    { t: "13:48:42", kind: "pass", text: "Passed on BTC > $115k · p_win below floor", meta: "p_win 0.58 · min 0.65", tone: "muted" },
    { t: "13:31:09", kind: "update", text: "Updated forecast · GPT-5 Q1 → 0.72", meta: "was 0.68", tone: "teal" },
    { t: "13:05:50", kind: "resolve", text: "Resolved WIN · CPI above 3.2%", meta: "+$142.80", tone: "profit" },
    { t: "12:41:22", kind: "scan", text: "Scanned 384 active markets", meta: "12 shortlisted", tone: "muted" },
    { t: "11:58:03", kind: "execute", text: "Opened NO position · BTC $120k", meta: "$260 · p_win 0.74 · conf 0.72", tone: "gold" },
    { t: "11:22:17", kind: "pass", text: "Passed on Election odds shift · correlated", meta: "event risk", tone: "muted" },
  ] as Activity[],
  resolutions: [
    { q: "GDP Q4 advance estimate", in: "9d", you: "NO 48%", conviction: 0.72 },
    { q: "Fed rate cut December", in: "18d", you: "YES 52%", conviction: 0.81 },
    { q: "CPI December print", in: "22d", you: "—", conviction: 0.0 },
  ] as Resolution[],
  risk: {
    dailyLoss: { used: 142, cap: 500, label: "Daily loss cap" } as RiskItem,
    drawdown: { used: 3.2, cap: 15, label: "Drawdown" } as RiskItem,
    exposure: { used: 1080, cap: 3000, label: "Gross exposure" } as RiskItem,
  },
  summary: {
    headline: "A steady week. 6 wins, 2 losses, 1 hold.",
    body:
      "Delfi is running slightly hot on tech-release markets and has tightened size there. Rate-path trades carried the week. Calibration on geopolitical tags drifted; next week's tune proposes lowering their size by 12%.",
    metric: { brier: 0.083, win: 68, trades: 14 },
  },
  scanning: { markets: 384, shortlist: 12, topForecast: 78, focus: "Macro · Rates" },
};

const ACT_ICON: Record<Activity["kind"], string> = {
  execute: "◆",
  pass: "–",
  update: "~",
  resolve: "✓",
  scan: "·",
};

export default function DashboardPage() {
  return (
    <div className="dash">
      <DashHero />
      <DashStatus />

      <div className="dash-grid">
        <section className="dash-card card-positions">
          <CardHead
            title="Open positions"
            meta={`${DEMO.positions.length} active`}
            href="/dashboard/positions"
          />
          <PositionsTable positions={DEMO.positions} />
        </section>

        <section className="dash-card card-activity">
          <CardHead title="Today" meta="Live feed" href="/dashboard/activity" live />
          <ActivityFeed items={DEMO.activity} />
        </section>

        <section className="dash-card card-risk">
          <CardHead title="Risk today" meta="Delfi's guardrails" href="/dashboard/risk" />
          <RiskGauges risk={DEMO.risk} />
        </section>

        <section className="dash-card card-upcoming">
          <CardHead title="Resolving soon" meta="Next 30 days" />
          <UpcomingList items={DEMO.resolutions} />
        </section>

        <section className="dash-card card-summary">
          <CardHead title="This week" meta="AI weekly summary" href="/dashboard/performance" />
          <SummaryCard s={DEMO.summary} />
        </section>
      </div>
    </div>
  );
}

function DashHero() {
  const p = DEMO.portfolio;
  return (
    <section className="dash-hero">
      <div className="hero-balance">
        <div className="hero-balance-head">
          <div className="hero-balance-label">Simulation balance</div>
          <div className="hero-balance-mode sim">Paper</div>
        </div>
        <div className="hero-balance-value t-num">
          <span className="hero-balance-cur">$</span>
          {p.value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
        </div>
        <div className="hero-deltas">
          <div className="hero-delta">
            <div className="hero-delta-label">Today</div>
            <div className="hero-delta-val profit t-num">
              +${p.deltaDay.toFixed(2)} <span className="hero-delta-pct">+{p.deltaDayPct}%</span>
            </div>
          </div>
          <div className="hero-delta-div"></div>
          <div className="hero-delta">
            <div className="hero-delta-label">Since start</div>
            <div className="hero-delta-val profit t-num">
              +${p.deltaWeek.toFixed(2)} <span className="hero-delta-pct">+{p.deltaWeekPct}%</span>
            </div>
          </div>
          <div className="hero-delta-div"></div>
          <div className="hero-delta">
            <div className="hero-delta-label">Started at</div>
            <div className="hero-delta-val t-num">${p.startValue.toLocaleString()}</div>
          </div>
        </div>
      </div>

      <div className="hero-chart">
        <div className="hero-chart-head">
          <div className="hero-chart-label">Equity · 50 days</div>
          <div className="hero-chart-tabs">
            <button className="chart-tab">7D</button>
            <button className="chart-tab">30D</button>
            <button className="chart-tab on">All</button>
          </div>
        </div>
        <EquityChart data={DEMO.equity} />
      </div>
    </section>
  );
}

function DashStatus() {
  const s = DEMO.scanning;
  return (
    <section className="dash-status">
      <div className="status-left">
        <div className="status-icon">
          <span className="status-ping"></span>
          <span className="status-ping-core"></span>
        </div>
        <div className="status-body">
          <div className="status-title">Delfi is scanning</div>
          <div className="status-sub">
            {s.markets} active markets · {s.shortlist} shortlisted · top p_win {(s.topForecast / 100).toFixed(2)} · focus{" "}
            {s.focus}
          </div>
        </div>
      </div>
      <div className="status-actions">
        <button className="btn-ghost sm">Pause agent</button>
        <Link className="btn-ghost sm" href="/dashboard/risk">
          Adjust risk →
        </Link>
      </div>
    </section>
  );
}

function CardHead({
  title,
  meta,
  href,
  live,
}: {
  title: string;
  meta?: string;
  href?: string;
  live?: boolean;
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
          View all →
        </Link>
      )}
    </div>
  );
}

function PositionsTable({ positions }: { positions: Position[] }) {
  return (
    <div className="pos-table">
      <div className="pos-row head">
        <div>Market</div>
        <div>Side</div>
        <div>Entry / Mark</div>
        <div>Size</div>
        <div>P&amp;L</div>
        <div>Closes</div>
      </div>
      {positions.map((p, i) => (
        <div className="pos-row" key={i}>
          <div className="pos-q">{p.q}</div>
          <div className={`pos-side ${p.side === "YES" ? "yes" : "no"}`}>{p.side}</div>
          <div className="pos-num t-num">
            <span className="pos-entry">{p.entry}¢</span>
            <span className="pos-arrow">→</span>
            <span className="pos-mark">{p.mark}¢</span>
          </div>
          <div className="pos-num t-num">${p.size}</div>
          <div className={`pos-pnl t-num ${p.pnl >= 0 ? "up" : "down"}`}>
            {p.pnl >= 0 ? "+" : ""}${p.pnl.toFixed(2)}
          </div>
          <div className="pos-closes t-num">{p.closes}</div>
        </div>
      ))}
    </div>
  );
}

function ActivityFeed({ items }: { items: Activity[] }) {
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

function RiskGauges({ risk }: { risk: typeof DEMO.risk }) {
  const items = [
    { key: "dailyLoss", unit: "$", ...risk.dailyLoss },
    { key: "drawdown", unit: "%", ...risk.drawdown },
    { key: "exposure", unit: "$", ...risk.exposure },
  ];
  return (
    <div className="risk-list">
      {items.map((r) => {
        const pct = Math.min(100, (r.used / r.cap) * 100);
        const tone = pct > 75 ? "hot" : pct > 50 ? "warm" : "ok";
        return (
          <div className="risk-row" key={r.key}>
            <div className="risk-top">
              <span className="risk-label">{r.label}</span>
              <span className={`risk-val t-num tone-${tone}`}>
                {r.unit === "$" ? "$" : ""}
                {r.used.toLocaleString()}
                {r.unit === "%" ? "%" : ""}
                <span className="risk-of">
                  &nbsp;/ {r.unit === "$" ? "$" : ""}
                  {r.cap.toLocaleString()}
                  {r.unit === "%" ? "%" : ""}
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

function UpcomingList({ items }: { items: Resolution[] }) {
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

function SummaryCard({ s }: { s: typeof DEMO.summary }) {
  return (
    <div className="sum">
      <div className="sum-head">{s.headline}</div>
      <p className="sum-body">{s.body}</p>
      <div className="sum-stats">
        <div className="sum-stat">
          <div className="sum-stat-num t-num">{s.metric.brier}</div>
          <div className="sum-stat-label">30-day Brier</div>
        </div>
        <div className="sum-stat">
          <div className="sum-stat-num t-num">{s.metric.win}%</div>
          <div className="sum-stat-label">Win rate</div>
        </div>
        <div className="sum-stat">
          <div className="sum-stat-num t-num">{s.metric.trades}</div>
          <div className="sum-stat-label">Trades</div>
        </div>
      </div>
    </div>
  );
}

function EquityChart({ data }: { data: number[] }) {
  const w = 640,
    h = 180,
    pad = 4;
  const min = Math.min(...data),
    max = Math.max(...data);
  const range = max - min || 1;
  const step = (w - pad * 2) / (data.length - 1);
  const points = data.map((v, i) => {
    const x = pad + i * step;
    const y = pad + (h - pad * 2) * (1 - (v - min) / range);
    return [x, y] as const;
  });
  const line = points
    .map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1))
    .join(" ");
  const area =
    line +
    ` L ${points[points.length - 1][0].toFixed(1)} ${h - pad} L ${points[0][0].toFixed(1)} ${
      h - pad
    } Z`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="eq-svg" preserveAspectRatio="none">
      <defs>
        <linearGradient id="eq-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--gold)" stopOpacity="0.35" />
          <stop offset="100%" stopColor="var(--gold)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#eq-fill)" />
      <path
        d={line}
        fill="none"
        stroke="var(--gold)"
        strokeWidth="1.6"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx={points[points.length - 1][0]} cy={points[points.length - 1][1]} r="4" fill="var(--gold)" />
      <circle
        cx={points[points.length - 1][0]}
        cy={points[points.length - 1][1]}
        r="8"
        fill="var(--gold)"
        opacity="0.2"
      />
    </svg>
  );
}
