"use client";

import React, { useState, useEffect, useRef } from "react";
import Link from "next/link";
import "./styles/homepage.css";

import { createClient } from "@/lib/supabase/client";

// ─── CountUp ─────────────────────────────────────────────
function CountUp({ target, duration = 2200, className = "" }: { target: number; duration?: number; className?: string }) {
  const [val, setVal] = useState(0);
  const [finished, setFinished] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const [started, setStarted] = useState(false);
  useEffect(() => {
    if (!ref.current) return;
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting && !started) setStarted(true); });
    }, { threshold: 0.3 });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [started]);
  useEffect(() => {
    if (!started) return;
    const start = performance.now();
    let raf: number;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
      setVal(Math.floor(eased * target));
      if (t < 1) raf = requestAnimationFrame(tick);
      else { setVal(target); setFinished(true); }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [started, target, duration]);
  useEffect(() => {
    if (!finished) return;
    const id = setInterval(() => {
      setVal((v) => v + Math.floor(1 + Math.random() * 3));
    }, 2400);
    return () => clearInterval(id);
  }, [finished]);
  return <span ref={ref} className={className}>{val.toLocaleString("en-US")}</span>;
}

// ─── Top nav ─────────────────────────────────────────────
function TopNav() {
  const [scrolled, setScrolled] = useState(false);
  const [pastHero, setPastHero] = useState(false);
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  useEffect(() => {
    const on = () => {
      setScrolled(window.scrollY > 20);
      setPastHero(window.scrollY > window.innerHeight * 0.85);
    };
    on();
    window.addEventListener("scroll", on, { passive: true });
    window.addEventListener("resize", on);
    return () => {
      window.removeEventListener("scroll", on);
      window.removeEventListener("resize", on);
    };
  }, []);
  useEffect(() => {
    const supabase = createClient();
    supabase.auth.getUser().then(({ data }) => setSignedIn(!!data.user));
    const { data: sub } = supabase.auth.onAuthStateChange((_event, session) => {
      setSignedIn(!!session?.user);
    });
    return () => sub.subscription.unsubscribe();
  }, []);
  return (
    <nav className={`top-nav ${scrolled ? "scrolled" : ""} ${pastHero ? "past-hero" : ""}`}>
      <div className="nav-inner">
        <div className="nav-left">
          <Link href="/" className="wordmark">
            <img src="/brand/mark.svg" alt="" className="wordmark-mark" />
            <span className="wordmark-text">DELFI</span>
          </Link>
          <ul className="nav-links">
            <li><a href="#how">How It Works</a></li>
            <li><a href="#versus">Us vs Them</a></li>
            <li><a href="#proof">Performance</a></li>
            <li><a href="#faq">FAQ</a></li>
          </ul>
        </div>
        <div className="nav-right">
          {signedIn ? (
            <Link className="btn-primary" href="/dashboard">Dashboard</Link>
          ) : (
            <>
              <Link className="nav-login" href="/auth#login">Log In</Link>
              <Link className="btn-primary" href="/auth#signup">Get Started</Link>
            </>
          )}
        </div>
      </div>
    </nav>
  );
}

// ─── Hero ────────────────────────────────────────────────
function Hero() {
  return (
    <section className="hero" id="hero" data-screen-label="01 Hero">
      <div className="hero-img">
        <img src="/brand/oracle-hero.jpg" alt="" />
      </div>
      <div className="hero-vignette" />
      <div className="hero-inner">
        <h1 className="t-display-xl hero-head">
          The future is <br />no longer <span className="hero-accent">a guess</span>
        </h1>
        <p className="hero-sub">
          The first autonomous, self-improving forecasting AI agent for Polymarket.
        </p>
        <div className="hero-ctas">
          <Link className="btn-primary" href="/auth#signup">Get Started</Link>
          <a className="btn-ghost" href="#how">See How It Works →</a>
        </div>
        <HeroPress />
      </div>
    </section>
  );
}

// ─── Hero press strip ───────────────────────────────────
function HeroPress() {
  const names = ["Bloomberg", "TechCrunch", "CoinDesk", "The Block", "Decrypt", "Wired"];
  return (
    <div className="hero-press">
      <div className="hero-press-label">As Seen In</div>
      <div className="hero-press-row">
        <div className="hero-press-track">
          {names.map((n) => (
            <span className="hero-press-item" key={`a-${n}`}>{n}</span>
          ))}
          {names.map((n) => (
            <span className="hero-press-item" aria-hidden="true" key={`b-${n}`}>{n}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Social proof ribbon (stats only) ────────────────────
function Ribbon() {
  return (
    <section className="ribbon" aria-label="Social proof">
      <div className="ribbon-inner">
        <div className="ribbon-stats">
          <div className="ribbon-stat">
            <div className="ribbon-num gold t-num">11,500+</div>
            <div className="ribbon-sub">active users</div>
          </div>
          <div className="ribbon-stat">
            <div className="ribbon-num vellum t-num">$50M+</div>
            <div className="ribbon-sub">in trades</div>
          </div>
          <div className="ribbon-stat">
            <div className="ribbon-num teal t-num">99.2%</div>
            <div className="ribbon-sub">uptime</div>
          </div>
        </div>
      </div>
    </section>
  );
}

// ─── Problem ─────────────────────────────────────────────
function Problem() {
  return (
    <section className="section problem" data-screen-label="04 Problem">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced problem-head">You&apos;re not losing to the market. <br />You&apos;re losing to the machines.</h2>
        </div>
        <p className="problem-body">
          Polymarket has become an algorithmic battleground. Machines read faster, trade faster, and never sleep. Every forecast you build has probably already been priced in by a bot ... before you finished reading the question.
        </p>
        <div className="problem-stats">
          <div className="problem-stat">
            <div className="problem-num">14/20</div>
            <div className="problem-label">of the most profitable Polymarket wallets are bots</div>
          </div>
          <div className="problem-stat">
            <div className="problem-num">$40M+</div>
            <div className="problem-label">extracted by arbitrage bots<br />in the last 12 months</div>
          </div>
          <div className="problem-stat">
            <div className="problem-num">25/day</div>
            <div className="problem-label">trades the average active<br />Polymarket user now makes</div>
          </div>
        </div>
        <p className="problem-close">
          The problem isn&apos;t what you know.<br className="br-keep" />
          <span>It&apos;s how fast you can act on it.</span>
        </p>
      </div>
    </section>
  );
}

// ─── Solution (4 cards) ──────────────────────────────────
const SOL_ICONS: Record<string, React.ReactElement> = {
  brain: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M9 3a3 3 0 0 0-3 3v.5a3 3 0 0 0-2 5.5v1a3 3 0 0 0 3 3 3 3 0 0 0 3 3V3z"/><path d="M15 3a3 3 0 0 1 3 3v.5a3 3 0 0 1 2 5.5v1a3 3 0 0 1-3 3 3 3 0 0 1-3 3V3z"/></svg>,
  shield: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M12 3 4 6v6c0 4.5 3.2 8.3 8 9 4.8-.7 8-4.5 8-9V6l-8-3z"/><path d="M9 12l2 2 4-4"/></svg>,
  cycle: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M21 12a9 9 0 0 1-15.5 6.3"/><path d="M3 12a9 9 0 0 1 15.5-6.3"/><path d="M21 4v5h-5M3 20v-5h5"/></svg>,
  eye: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>,
};
function Solution() {
  const cards = [
    { icon: "brain", title: "Thinks", desc: "Reads the news. Checks historical base rates. Weighs structured data. Produces its own probability estimate for every market." },
    { icon: "shield", title: "Calculates", desc: "Flat 1-3% position sizing, scaled by confidence in the call. Drawdown circuit breakers, daily loss caps, and event correlation guards. Never over-exposed. Never emotional." },
    { icon: "cycle", title: "Learns", desc: "Tracks its own accuracy by category and Brier score. Every 50 resolved trades it proposes calibrations for you to approve. Delfi gets sharper the longer it runs." },
    { icon: "eye", title: "Explains", desc: "Every trade comes with its full reasoning: the probability estimate, the research sources, the gates it cleared, and the risk logic. You see what Delfi saw. You see why it traded." },
  ];
  return (
    <section className="section solution" data-screen-label="05 Solution">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced">An AI agent with four minds.</h2>
          <p>Everything a good trader does. Running for you 24/7.</p>
        </div>
        <div className="solution-grid">
          {cards.map((c) => (
            <div className="sol-card" key={c.title}>
              <div className="sol-icon">{SOL_ICONS[c.icon]}</div>
              <h3 className="sol-title">{c.title}</h3>
              <p className="sol-desc">{c.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ─── Pillars + pipeline ──────────────────────────────────
function Pillars() {
  const items = [
    { n: "01", title: "Probability Engine", desc: "Delfi reads every active market and produces its own probability estimate, grounded in news, historical base rates, and structured data." },
    { n: "02", title: "Position Sizer", desc: "Every trade is sized flat at 1-3% of bankroll, scaled by Delfi's confidence in the call. Two independent gates, direction agreement and minimum win probability, must clear before a dollar moves. Low-confidence calls get a smaller stake, not a skip." },
    { n: "03", title: "Risk Manager", desc: "Before any trade executes, Delfi checks the portfolio: daily loss caps, drawdown circuit breakers, event correlation guards. If the book is already stressed, Delfi passes." },
  ];
  const nodes = ["Scan", "Estimate", "Size", "Verify", "Execute"];
  return (
    <section className="section pillars" id="how" data-screen-label="06 How It Works">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced pillars-head">Every trade passes through <br />three independent systems</h2>
        </div>
        <div className="pillars-grid">
          {items.map((p) => (
            <div className="pillar-card" key={p.n}>
              <div className="pillar-num t-num">{p.n}</div>
              <h3 className="pillar-title">{p.title}</h3>
              <p className="pillar-desc">{p.desc}</p>
            </div>
          ))}
        </div>
        <AnimatedPipeline nodes={nodes} />
      </div>
    </section>
  );
}

// ─── Versus table ────────────────────────────────────────
function Versus() {
  const rows: [string, string, string, string, string][] = [
    ["Trades 24/7 automatically", "x", "✓", "✓", "✓"],
    ["Reasoning transparent per trade", "partial", "x", "x", "✓"],
    ["Probability-based", "✓", "x", "partial", "✓"],
    ["Institutional risk sizing", "x", "partial", "x", "✓"],
    ["Self-calibrating over time", "x", "x", "x", "✓"],
    ["No coding required", "✓", "x", "✓", "✓"],
    ["Full control of risk parameters", "✓", "x", "partial", "✓"],
    ["Works across all market categories", "partial", "x", "partial", "✓"],
  ];
  const cell = (v: string) => {
    if (v === "✓") return <span className="vs-check">✓</span>;
    if (v === "x") return <span className="vs-cross">✕</span>;
    if (v === "partial") return <span className="vs-partial">partial</span>;
    return v;
  };
  return (
    <section className="section versus" id="versus" data-screen-label="07 Us vs Them">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced versus-head">There are three kinds of Polymarket bots ... and then there&apos;s Delfi</h2>
          <p>Arbitrage bots compete on speed. Copy-trading tools compete on who to follow. Delfi competes on accuracy, discipline, and transparency, all at once.</p>
        </div>
        <div className="vs-table-wrap">
          <table className="vs-table">
            <thead>
              <tr>
                <th className="criteria-col"></th>
                <th>Manual Trading</th>
                <th>Arbitrage Bots</th>
                <th>Copy Trading</th>
                <th className="delfi-col">Delfi</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r[0]}>
                  <td className="criteria-col">{r[0]}</td>
                  <td>{cell(r[1])}</td>
                  <td>{cell(r[2])}</td>
                  <td>{cell(r[3])}</td>
                  <td className="delfi-col">{cell(r[4])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="vs-foot">Delfi is the only AI agent on Polymarket that combines deep reasoning with institutional-grade risk math. Everything else is a subset.</p>
      </div>
    </section>
  );
}

// ─── Proof ───────────────────────────────────────────────
const TRADE_LOG = [
  { ts: "14:32", type: "ENTRY", typeCls: "entry", mkt: "Fed cuts by Dec", meta: "YES · M YES 58% · D YES 78% · D CONF 81%", cls: "" },
  { ts: "14:28", type: "SCAN", typeCls: "scan", mkt: "Politics, 47 mkts - no forecasts cleared gates", meta: "", cls: "" },
  { ts: "14:19", type: "RESOLVE", typeCls: "resolve", mkt: "ETH > $6000?", meta: "correct · +$47", cls: "pos" },
  { ts: "13:58", type: "ENTRY", typeCls: "entry", mkt: "TikTok ban Q1?", meta: "NO · M YES 41% · D YES 23% · D CONF 74%", cls: "" },
  { ts: "13:44", type: "RESOLVE", typeCls: "resolve", mkt: "NFL Week 12 MIA", meta: "incorrect · −$31", cls: "neg" },
  { ts: "13:22", type: "ENTRY", typeCls: "entry", mkt: "CPI below 2.5% in June", meta: "YES · M YES 55% · D YES 72% · D CONF 68%", cls: "" },
  { ts: "12:58", type: "RESOLVE", typeCls: "resolve", mkt: "Warriors beat Suns?", meta: "correct · +$22", cls: "pos" },
  { ts: "12:33", type: "SCAN", typeCls: "scan", mkt: "Sports, 112 mkts - 3 forecasts cleared gates", meta: "", cls: "" },
  { ts: "12:17", type: "ENTRY", typeCls: "entry", mkt: "UK PM resigns by Q3", meta: "NO · M YES 32% · D YES 19% · D CONF 70%", cls: "" },
  { ts: "11:54", type: "RESOLVE", typeCls: "resolve", mkt: "OPEC production cut?", meta: "correct · +$38", cls: "pos" },
];

function CalibrationChart() {
  const pts: [number, number][] = [
    [0.05, 0.06], [0.15, 0.17], [0.25, 0.24], [0.35, 0.36],
    [0.45, 0.43], [0.55, 0.57], [0.65, 0.63], [0.75, 0.76],
    [0.85, 0.84], [0.95, 0.94],
  ];
  const w = 400, h = 240, pad = 32;
  const x = (v: number) => pad + v * (w - pad * 2);
  const y = (v: number) => h - pad - v * (h - pad * 2);
  const path = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${x(p[0])} ${y(p[1])}`).join(" ");

  return (
    <svg className="chart-svg" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="xMidYMid meet">
      {[0.25, 0.5, 0.75].map((v) => (
        <g key={v}>
          <line x1={x(v)} y1={pad} x2={x(v)} y2={h - pad} stroke="rgba(232,228,216,0.05)" />
          <line x1={pad} y1={y(v)} x2={w - pad} y2={y(v)} stroke="rgba(232,228,216,0.05)" />
        </g>
      ))}
      <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} stroke="rgba(232,228,216,0.2)" />
      <line x1={pad} y1={pad} x2={pad} y2={h - pad} stroke="rgba(232,228,216,0.2)" />
      <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} stroke="rgba(232,228,216,0.3)" strokeDasharray="4 4" />
      <path d={path} fill="none" stroke="var(--teal)" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" style={{ filter: "drop-shadow(0 0 4px rgba(0,255,255,0.5))" }} />
      {pts.map((p, i) => (
        <circle key={i} cx={x(p[0])} cy={y(p[1])} r="2.5" fill="var(--teal)" />
      ))}
      <text x={x(0.5)} y={h - 6} fill="rgba(232,228,216,0.4)" fontSize="9" fontFamily="monospace" textAnchor="middle" letterSpacing="1.5">PREDICTED PROBABILITY</text>
      <text x={10} y={y(0.5)} fill="rgba(232,228,216,0.4)" fontSize="9" fontFamily="monospace" textAnchor="middle" transform={`rotate(-90, 10, ${y(0.5)})`} letterSpacing="1.5">ACTUAL OUTCOME RATE</text>
    </svg>
  );
}

function Proof() {
  const loop = [...TRADE_LOG, ...TRADE_LOG];
  return (
    <section className="section proof" id="proof" data-screen-label="08 Proof">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced proof-head">Sharper than the crowd.<br className="br-keep" /> Measurably.</h2>
        </div>

        <div className="proof-stats">
          <div className="proof-stat">
            <div className="proof-num vellum t-num"><CountUp target={34788} /></div>
            <div className="proof-label">Predictions Resolved</div>
          </div>
          <div className="proof-stat">
            <div className="proof-num gold t-num">0.087</div>
            <div className="proof-label">30-Day Brier Score</div>
          </div>
          <div className="proof-stat">
            <div className="proof-num teal t-num">68%</div>
            <div className="proof-label">Win Rate, Last 30 Days</div>
          </div>
          <div className="proof-stat">
            <div className="proof-num teal t-num">+47%</div>
            <div className="proof-label">Avg ROI Across Positions</div>
          </div>
        </div>

        <div className="proof-grid">
          <div className="chart-panel">
            <div className="panel-label">Calibration Curve</div>
            <CalibrationChart />
            <p className="chart-caption">Delfi&apos;s estimates closely match real-world outcomes. That is the definition of calibration. The dashed line is perfect calibration; the teal line is Delfi.</p>
          </div>
          <div className="terminal-panel">
            <div className="term-head">
              <span className="dot r"></span><span className="dot y"></span><span className="dot g"></span>
              <span className="term-title">delfi · recent trades</span>
              <span className="term-live"><span className="live-dot"></span> Live</span>
            </div>
            <div className="term-body">
              <div className="term-scroll">
                {loop.map((t, i) => (
                  <div className="term-line" key={i}>
                    <span className="term-ts">{t.ts}</span>
                    <span className={`term-type ${t.typeCls}`}>{t.type}</span>
                    <span className="term-mkt">{t.mkt}</span>
                    <span className="term-meta"><span className={t.cls}>{t.meta}</span></span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

// ─── Simulation Mode ─────────────────────────────────────
function Simulation() {
  const [live, setLive] = useState(false);
  return (
    <section className="section sim" data-screen-label="08.5 Simulation">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced">Try Delfi without risking a cent</h2>
        </div>

        <div className="sim-grid">
          <div className="sim-copy">
            <ul className="sim-bullets">
              <li>
                <span className="sim-bullet-dot"></span>
                <div>
                  <div className="sim-bullet-title">Start in Simulation Mode.</div>
                  <div className="sim-bullet-desc">Every decision is what Delfi would make live. The only difference is that the capital is paper.</div>
                </div>
              </li>
              <li>
                <span className="sim-bullet-dot"></span>
                <div>
                  <div className="sim-bullet-title">Watch Delfi prove itself.</div>
                  <div className="sim-bullet-desc">Let Delfi run for a day or a week. Read the reasoning, check the P&amp;L, see how it handles volatile news. You decide when the track record is enough.</div>
                </div>
              </li>
              <li>
                <span className="sim-bullet-dot"></span>
                <div>
                  <div className="sim-bullet-title">Switch to Live when the numbers convince you.</div>
                  <div className="sim-bullet-desc">Connect your wallet and toggle to Live. Delfi keeps running at the same settings. Only the capital is real now.</div>
                </div>
              </li>
            </ul>
            <Link className="sim-cta" href="/auth#signup">Try it now →</Link>
          </div>

          <div className="sim-mock" aria-hidden="true">
            <div className="sim-mock-head">
              <div className={`sim-toggle ${live ? "is-live" : "is-sim"}`} role="tablist">
                <button
                  type="button"
                  role="tab"
                  aria-selected={!live}
                  className={`sim-toggle-btn ${!live ? "on" : ""}`}
                  onClick={() => setLive(false)}
                >Simulation</button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={live}
                  className={`sim-toggle-btn ${live ? "on" : ""}`}
                  onClick={() => setLive(true)}
                >Live</button>
                <span className={`sim-toggle-thumb ${live ? "right" : "left"}`} aria-hidden="true" />
              </div>
            </div>

            <div className={`sim-card ${live ? "is-live" : "is-sim"}`}>
              <div className="sim-card-top">
                <span className={`sim-pill ${live ? "live" : "sim"}`}>
                  <span className="sim-pill-dot"></span>
                  {live ? "LIVE" : "SIMULATION"}
                </span>
                <span className="sim-card-time t-num">14:02:17 UTC</span>
              </div>

              <div className="sim-card-q">Fed cuts rates by 25bp in December?</div>

              <div className="sim-card-row">
                <div className="sim-card-cell">
                  <div className="sim-cell-label">Market</div>
                  <div className="sim-cell-val t-num">44%</div>
                </div>
                <div className="sim-card-cell">
                  <div className="sim-cell-label">Delfi Forecast</div>
                  <div className="sim-cell-val gold t-num">61%</div>
                </div>
                <div className="sim-card-cell">
                  <div className="sim-cell-label">Confidence</div>
                  <div className="sim-cell-val teal t-num">0.81</div>
                </div>
              </div>

              <div className="sim-card-exec">
                <div className="sim-exec-row">
                  <span className="sim-exec-label">Size</span>
                  <span className="sim-exec-val t-num">$420.00</span>
                </div>
                <div className="sim-exec-row">
                  <span className="sim-exec-label">Funded by</span>
                  <span className="sim-exec-val">
                    {live ? "Your wallet" : "Simulation fund"}
                  </span>
                </div>
                <div className="sim-exec-row">
                  <span className="sim-exec-label">P&amp;L impact</span>
                  <span className={`sim-exec-val ${live ? "live" : "paper"} t-num`}>
                    +$71.40
                  </span>
                </div>
              </div>

              <div className="sim-card-foot">
                <span className="sim-foot-label">Reasoning</span>
                <span className="sim-foot-text">Three Fed speakers this week signaled dovish bias. CPI trending 0.2% below consensus. Base rate of cuts post-dovish-trio: 73%.</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

// ─── New here ────────────────────────────────────────────
function NewHere() {
  return (
    <section className="section newhere" data-screen-label="09 New Here">
      <div className="container">
        <div className="newhere-grid">
          <div>
            <h2 className="newhere-head balanced">New to Polymarket?</h2>
            <p className="newhere-body">Polymarket is a marketplace for real-world questions. Each question trades between 0% and 100%, and the price is the crowd&apos;s probability. A question trading at 44% means the market thinks there&apos;s a 44% chance it resolves yes.</p>
            <p className="newhere-body muted">But the markets are often wrong. People bet on what they want to be true. They anchor on headlines and ignore base rates. A patient reader can forecast outcomes more accurately than the crowd. The hard part is doing it consistently, sizing each trade correctly, and walking away when the read isn&apos;t strong enough.</p>
            <p className="newhere-body muted">Delfi does all of that for you. It reads every tradeable market, builds its own forecast, sizes each trade to its confidence, and acts when the forecast clears every gate.</p>
            <p className="newhere-body muted">You don&apos;t need to be a prediction market expert. You just need an account with Delfi.</p>
            <Link className="newhere-cta" href="/auth#signup">Start for free today →</Link>
          </div>
          <div className="edge-viz">
            <div className="edge-q">Fed cuts rates in December?</div>
            <div className="edge-row">
              <span className="edge-label">Market Prediction</span>
              <span className="edge-num t-num">44%</span>
            </div>
            <div className="edge-row delfi">
              <span className="edge-label">Delfi Prediction</span>
              <span className="edge-num t-num">62%</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

// ─── Testimonials ─────────────────────────────────────────
function Testimonials() {
  const quotes = [
    { body: "Before Delfi I was scrolling Polymarket at 2am like an idiot. Now I scroll it once a week and check P&L. Got higher returns and less anxiety.", name: "Marcus K.", role: "Amsterdam, NL" },
    { body: "Every Polymarket bot I looked at was a black box with marketing attached. No probability model, no sources, no sizing logic. Delfi shows all three on every trade. I can still disagree with a call, but I can't accuse the thing of being opaque. That's rare.", name: "Jenna R.", role: "Cape Coral, USA" },
    { body: "I'd never placed a Polymarket trade before. I connected a wallet with $400, picked the conservative risk profile, and let Delfi run. Three weeks in, I'm up roughly $150 and I check the Telegram summary over coffee. I still don't really understand prediction markets.", name: "Daniel O.", role: "Singapore, SG" },
    { body: "I tried three other Polymarket bots before Delfi. One was a copy-trading tool, one was an arbitrage scanner, one just didn't work. Delfi is the only one that actually thinks about each market.", name: "Alex T.", role: "Manchester, UK" },
  ];
  return (
    <section className="section testimonials" data-screen-label="10 Testimonials">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced">Traders who stopped <br />trading by hand</h2>
        </div>
        <div className="test-grid">
          {quotes.map((q) => (
            <div className="test-card" key={q.name}>
              <span className="test-quote-mark">&quot;</span>
              <p className="test-body">{q.body}</p>
              <div className="test-author">
                <span className="test-author-dot"></span>
                <div>
                  <div className="test-author-name">{q.name}</div>
                  <div className="test-author-role">{q.role}</div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ─── FAQ ──────────────────────────────────────────────────
function FAQ() {
  const [open, setOpen] = useState(0);
  const items = [
    { q: "What exactly is Delfi?", a: "Delfi is an autonomous AI agent that trades on Polymarket for you. It continuously scans every active prediction market, produces its own probability forecasts, and backs those forecasts with small, confidence-scaled stakes, all within the risk limits you set." },
    { q: "How is this different from other Polymarket bots?", a: "Most Polymarket bots are either arbitrage scanners (exploiting price inconsistencies at high speed), copy-trading tools (mirroring top traders), or basic momentum systems. Delfi is none of those. It's a reasoning-based AI agent that evaluates each market the way a sharp human trader would, with research, probability modeling, and calibrated risk sizing." },
    { q: "What happens if Delfi is wrong?", a: "You lose money on that trade. Delfi is probabilistic, not psychic. It aims to be right more often than it is wrong, not infallible. Over hundreds of trades, calibrated forecasting compounds into real returns. On any single trade, anything can happen. All losses are capped by the daily and weekly risk limits you set during onboarding." },
    { q: "Do I need a Polymarket account first?", a: "No. You can sign up for Delfi free today and explore the product without a Polymarket account. When you're ready to trade live, Delfi will guide you through connecting a wallet. If you're new to Polymarket entirely, Delfi is actually one of the easiest ways in. You get the benefits of active trading without having to become an active trader." },
    { q: "Is my money safe?", a: "Delfi never custodies your funds. Your capital stays in your own Polymarket wallet. Delfi only has execution permission. It can place trades within the limits you set, but it cannot withdraw funds or transfer them anywhere. You can pause trading or revoke permissions at any time." },
    { q: "How much does it cost?", a: "You can register for free and explore every feature: watch Delfi evaluate markets, read its reasoning, and review its live performance. When you're ready to trade live, subscriptions start at $69 per month, or $45 per month on the annual plan (a 35% saving)." },
    { q: "Is this legal?", a: "In the United States, Polymarket is regulated under the CFTC following its QCX acquisition and amended order of designation. Delfi operates as an automated trading tool on top of a regulated exchange. In the United Kingdom, the European Union, Canada, Australia, and many other jurisdictions, prediction market rules vary widely. Some countries permit it, some restrict it, some prohibit it outright. Confirm legality in your own region before trading. If in doubt, consult a local regulator or legal advisor." },
    { q: "Can I turn Delfi off?", a: "Yes, any time. Use the /stop command in Telegram or the emergency stop button in the dashboard. Open positions remain open until resolution. No new trades will be placed until you resume." },
  ];
  return (
    <section className="section faq" id="faq" data-screen-label="11 FAQ">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced">Q&amp;A before you sign up</h2>
        </div>
        <div className="faq-list">
          {items.map((item, i) => (
            <div className={`faq-item ${open === i ? "open" : ""}`} key={i}>
              <button className="faq-q" onClick={() => setOpen(open === i ? -1 : i)} aria-expanded={open === i}>
                <span>{item.q}</span>
                <span className="faq-icon">+</span>
              </button>
              <div className="faq-a">
                <div className="faq-a-inner">{item.a}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ─── Final CTA ────────────────────────────────────────────
function FinalCTA() {
  return (
    <section className="final-cta" data-screen-label="12 Final CTA">
      <div className="quantum-grid" />
      <div className="container final-inner">
        <h2 className="final-head balanced">Stop reading. Start trading.</h2>
        <p className="final-sub">Sign up in three minutes. Delfi will take care of the rest.</p>
        <Link className="btn-primary large" href="/auth#signup">Get Started</Link>
      </div>
    </section>
  );
}

// ─── Footer ───────────────────────────────────────────────
function Footer() {
  return (
    <footer className="site-footer">
      <div className="container">
        <div className="foot-main">
          <div className="foot-brand">
            <Link href="/" className="wordmark">
              <img src="/brand/mark.svg" alt="" className="wordmark-mark" />
              <span className="wordmark-text">DELFI</span>
            </Link>
            <p className="foot-tag">Autonomous AI agent for Polymarket. Deep reasoning meets institutional risk math.</p>
            <span className="foot-contact">info@delfibot.com</span>
          </div>
          <div className="foot-meta">
            <div className="foot-legal-block">
              <div className="foot-heading">Legal</div>
              <ul className="foot-legal">
                <li><Link href="/legal/terms">Terms of Service</Link></li>
                <li><Link href="/legal/privacy">Privacy Policy</Link></li>
                <li><Link href="/legal/risk">Risk Disclosure</Link></li>
              </ul>
            </div>
          </div>
        </div>
        <hr className="foot-divider" />
        <div className="foot-bottom">
          <span className="foot-copy">© 2026 Delfi · All rights reserved.</span>
          <span className="foot-risk">Prediction market trading involves real financial risk. Past performance does not guarantee future results.</span>
        </div>
      </div>
    </footer>
  );
}

// ─── Animated pipeline ───────────────────────────────────
function AnimatedPipeline({ nodes }: { nodes: string[] }) {
  const [step, setStep] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!ref.current) return;
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) setVisible(true); });
    }, { threshold: 0.2 });
    io.observe(ref.current);
    return () => io.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    const id = setInterval(() => {
      setStep((s) => (s + 1) % (nodes.length + 2));
    }, 900);
    return () => clearInterval(id);
  }, [visible, nodes.length]);
  const complete = step === nodes.length + 1 || step === nodes.length;
  return (
    <div className={`pipeline ${complete ? "complete" : ""}`} ref={ref}>
      <div className="pipeline-inner">
        {nodes.map((n, i) => (
          <React.Fragment key={n}>
            <div className={`pipe-node ${i < step ? "done" : ""} ${i === step - 1 ? "active" : ""}`}>
              <span className="pipe-dot"></span>
              <span className="pipe-label">{n}</span>
            </div>
            {i < nodes.length - 1 && (
              <span className={`pipe-line ${i < step - 1 ? "full" : i === step - 1 ? "filling" : ""}`}>
                <span className="pipe-line-fill"></span>
              </span>
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────
export default function HomePage() {
  return (
    <>
      <TopNav />
      <Hero />
      <Ribbon />
      <Problem />
      <Solution />
      <Pillars />
      <Versus />
      <Proof />
      <Simulation />
      <NewHere />
      <Testimonials />
      <FAQ />
      <FinalCTA />
      <Footer />
    </>
  );
}
