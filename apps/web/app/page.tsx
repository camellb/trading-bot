"use client";

import React, { useState, useEffect, useRef } from "react";
import Link from "next/link";
import "./styles/homepage.css";

// Checkout destination. Default is the embedded checkout at
// /checkout (Stripe Checkout in `ui_mode: "embedded"`, mounted
// inside the Delfi-branded /checkout page). Override via
// NEXT_PUBLIC_CHECKOUT_URL if we ever need to fall back to a
// hosted Payment Link or third-party processor; the env var
// continues to work for both relative (/checkout) and absolute
// (https://buy.stripe.com/...) URLs. The actual download link
// is delivered in the post-purchase email, never embedded
// here.
const CHECKOUT_URL = process.env.NEXT_PUBLIC_CHECKOUT_URL || "/checkout";

// ─── Landing-page analytics ──────────────────────────────
//
// The Lemon Squeezy sales dashboard tells us only the bottom of the
// funnel (orders, refunds, MRR). To iterate on the page itself we
// need the top of the funnel: how many people see each CTA, click
// it, and reach checkout.
//
// We push two events per CTA click:
//   gtag('event', 'cta_click', { cta_location })   → GA4 funnel + reports
//   fbq('trackCustom', 'CtaClick', { cta_location }) → Meta Pixel
//
// Plus an `IntersectionObserver` per section to record `section_view`
// once when 50% of it scrolls into view — that gives us scroll-depth
// and a per-section heatmap inside Clarity's segments. UTM params
// are appended to CHECKOUT_URL so LS records the originating CTA on
// the order itself; combining LS orders + GA4 events gives us CVR
// per CTA.
//
// Providers are loaded by `lib/analytics.tsx` and gated by the
// cookie banner (`ConsentGate`). Before consent, `window.gtag` and
// `window.fbq` are undefined and these calls are no-ops.
type GtagFn = (...args: unknown[]) => void;
type FbqFn  = (...args: unknown[]) => void;
declare global {
  interface Window {
    gtag?: GtagFn;
    fbq?:  FbqFn;
  }
}

function trackCta(location: string, ctaText: string) {
  try {
    window.gtag?.("event", "cta_click", {
      cta_location: location,
      cta_text:     ctaText,
    });
    window.fbq?.("trackCustom", "CtaClick", {
      cta_location: location,
      cta_text:     ctaText,
    });
  } catch {
    /* analytics failures must never break the click */
  }
}

function trackSectionView(section: string) {
  try {
    window.gtag?.("event", "section_view", { section });
  } catch {
    /* ditto */
  }
}

function withUtm(url: string, location: string): string {
  // Don't decorate mailto: fallback - would break the address.
  if (url.startsWith("mailto:")) return url;
  const sep = url.includes("?") ? "&" : "?";
  const params = new URLSearchParams({
    utm_source:  "delfi-site",
    utm_medium:  "cta",
    utm_content: location,
  });
  return `${url}${sep}${params.toString()}`;
}

/** Tracked checkout CTA. Use everywhere on the landing page. The
 *  visible label is variable per location so we can A/B and so the
 *  analytics events carry the actual button text the user clicked. */
function CtaLink({
  location,
  className,
  children,
  text,
}: {
  location: string;
  className: string;
  children: React.ReactNode;
  /** Plain-text label for analytics. Defaults to "Get Delfi". Pass
   *  the visible text when it differs from the default. */
  text?: string;
}) {
  const ctaText = text ?? "Get Delfi";
  return (
    <a
      className={className}
      href={withUtm(CHECKOUT_URL, location)}
      onClick={() => trackCta(location, ctaText)}
    >
      {children}
    </a>
  );
}

/** One IntersectionObserver per page that fires `section_view` once
 *  per `[data-screen-label]` section as it scrolls past 50% visible.
 *  Cheaper than a hook-per-section: existing `data-screen-label` tags
 *  on every section already provide the natural opt-in list. */
function useScrollDepthTracking() {
  useEffect(() => {
    const sections = Array.from(
      document.querySelectorAll<HTMLElement>("[data-screen-label]"),
    );
    if (sections.length === 0) return;
    const fired = new Set<string>();
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          const label = (e.target as HTMLElement).dataset.screenLabel;
          if (e.isIntersecting && label && !fired.has(label)) {
            fired.add(label);
            trackSectionView(label);
          }
        }
      },
      { threshold: 0.5 },
    );
    sections.forEach((el) => obs.observe(el));
    return () => obs.disconnect();
  }, []);
}

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
  return (
    <nav className={`top-nav ${scrolled ? "scrolled" : ""} ${pastHero ? "past-hero" : ""}`}>
      <div className="nav-inner">
        <div className="nav-left">
          <Link href="/" className="wordmark">
            <img src="/brand/mark.svg" alt="" className="wordmark-mark" />
            <span className="wordmark-text">DELFI</span>
          </Link>
        </div>
        <div className="nav-center">
          <a className="nav-link" href="#how">How it works</a>
          <a className="nav-link" href="#demo">Demo</a>
          <a className="nav-link" href="#pricing">Pricing</a>
        </div>
        <div className="nav-right">
          <CtaLink className="btn-primary" location="topnav" text="Get Delfi">Get Delfi</CtaLink>
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
          Self-improving autonomous Polymarket bot<br />that trades for you 24/7.
        </p>
        <div className="hero-ctas">
          <CtaLink className="btn-primary" location="hero" text="Get Delfi">Get Delfi</CtaLink>
          <a className="btn-ghost" href="#how">See How It Works →</a>
        </div>
        <div className="hero-stats" aria-label="Social proof">
          <div className="hero-stat">
            <div className="hero-stat-num gold t-num">11,500+</div>
            <div className="hero-stat-sub">downloads</div>
          </div>
          <div className="hero-stat">
            <div className="hero-stat-num vellum t-num">100</div>
            <div className="hero-stat-sub">markets re-scored every 5 min</div>
          </div>
          <div className="hero-stat">
            <div className="hero-stat-num teal t-num">24/7</div>
            <div className="hero-stat-sub">autonomous</div>
          </div>
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

// ─── Pillars + pipeline ──────────────────────────────────
function Pillars() {
  const items = [
    { n: "01", title: "Probability Engine", desc: "Delfi reads every active market and produces its own probability estimate, grounded in news, historical base rates, and structured data." },
    { n: "02", title: "Position Sizer", desc: "Every trade is sized as a small flat fraction of bankroll, scaled by per-archetype multipliers you control. Delfi backs the market favourite on every tradeable market that clears its archetype skip list and risk gates. The forecaster's job is to tune those gates over time, not to second-guess the crowd on individual trades." },
    { n: "03", title: "Risk Manager", desc: "Before any trade executes, Delfi checks the portfolio: daily loss cap, weekly loss cap, drawdown halt, streak cooldown, dry-powder reserve. If the book is already stressed, Delfi passes." },
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
    ["Probability-based", "partial", "x", "partial", "✓"],
    ["Institutional risk sizing", "x", "partial", "x", "✓"],
    ["Self-calibrating over time", "x", "x", "x", "✓"],
    ["No coding required", "✓", "partial", "✓", "✓"],
    ["Full control of risk parameters", "✓", "partial", "partial", "✓"],
    ["Works across all market categories", "partial", "x", "partial", "✓"],
    ["Runs on your machine", "✓", "x", "x", "✓"],
    ["Your private key never leaves", "✓", "x", "x", "✓"],
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
      </div>
    </section>
  );
}

// ─── Proof ───────────────────────────────────────────────
const TRADE_LOG = [
  { ts: "14:32", type: "ENTRY", typeCls: "entry", mkt: "Fed cuts by Dec", meta: "YES · favourite 58% · politics ×1.0 · $4.20", cls: "" },
  { ts: "14:28", type: "SCAN", typeCls: "scan", mkt: "Politics, 47 markets - 2 inside archetype skip list", meta: "", cls: "" },
  { ts: "14:19", type: "RESOLVE", typeCls: "resolve", mkt: "ETH > $6000?", meta: "correct · +$4.70", cls: "pos" },
  { ts: "13:58", type: "ENTRY", typeCls: "entry", mkt: "TikTok ban Q1?", meta: "NO · favourite 59% · tech ×1.0 · $4.20", cls: "" },
  { ts: "13:44", type: "RESOLVE", typeCls: "resolve", mkt: "NFL Week 12 MIA", meta: "incorrect · -$3.10", cls: "neg" },
  { ts: "13:22", type: "ENTRY", typeCls: "entry", mkt: "CPI below 2.5% in June", meta: "YES · favourite 55% · macro ×1.0 · $4.20", cls: "" },
  { ts: "12:58", type: "RESOLVE", typeCls: "resolve", mkt: "Warriors beat Suns?", meta: "correct · +$2.20", cls: "pos" },
  { ts: "12:33", type: "SCAN", typeCls: "scan", mkt: "Sports, 112 markets - 38 inside archetype skip list", meta: "", cls: "" },
  { ts: "12:17", type: "ENTRY", typeCls: "entry", mkt: "UK PM resigns by Q3", meta: "NO · favourite 68% · politics ×1.0 · $4.20", cls: "" },
  { ts: "11:54", type: "RESOLVE", typeCls: "resolve", mkt: "OPEC production cut?", meta: "correct · +$3.80", cls: "pos" },
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
            <div className="proof-num vellum t-num">11,500+</div>
            <div className="proof-label">Downloads</div>
          </div>
          <div className="proof-stat">
            <div className="proof-num gold t-num">0.06</div>
            <div className="proof-label">30-Day Brier Score</div>
          </div>
          <div className="proof-stat">
            <div className="proof-num teal t-num">76%</div>
            <div className="proof-label">Avg 30-Day Win Rate</div>
          </div>
          <div className="proof-stat">
            <div className="proof-num teal t-num">+34%</div>
            <div className="proof-label">Average Monthly ROI</div>
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

// ─── Custody promise (the local-first pitch) ─────────────
function CustodyPromise() {
  return (
    <section className="section custody" data-screen-label="07.5 Custody">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced">Your funds. Your keys.<br />Your machine.</h2>
        </div>
        <div className="custody-grid">
          <p className="custody-body">
            Delfi runs on your computer, not ours. Your Polymarket key never leaves your OS keychain. Delfi reads it only when it places a trade. Your wallet stays invisible to us by design.
          </p>
          <ul className="custody-list">
            <li><span className="custody-tick">✓</span> We never see your wallet address.</li>
            <li><span className="custody-tick">✓</span> We never custody your capital.</li>
            <li><span className="custody-tick">✓</span> We never know which trades you make.</li>
            <li><span className="custody-tick">✓</span> We could go offline tomorrow and your bot would keep running.</li>
          </ul>
        </div>
      </div>
    </section>
  );
}

// ─── Platforms (macOS + Windows) ─────────────────────────
function Platforms() {
  return (
    <section className="section platforms" id="platforms" data-screen-label="07.6 Platforms">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced">Available for macOS<br />and Windows</h2>
        </div>
        <div className="platforms-grid">
          <div className="platform-card is-info">
            <div className="platform-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="currentColor"><path d="M16.6 12.6c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.1-2.8.9-3.5.9-.7 0-1.9-.8-3.1-.8-1.6 0-3.1.9-3.9 2.4-1.7 2.9-.4 7.2 1.2 9.5.8 1.1 1.7 2.4 2.9 2.4 1.2 0 1.6-.8 3-.8s1.8.8 3 .8 2-1.2 2.8-2.3c.9-1.3 1.2-2.6 1.2-2.7 0 0-2.3-.9-2.3-3.6zM14.4 5.7c.6-.7 1-1.7.9-2.7-.9.1-2 .6-2.6 1.3-.6.6-1.1 1.6-.9 2.6.9.1 1.9-.5 2.6-1.2z"/></svg>
            </div>
            <div className="platform-body">
              <div className="platform-name">macOS</div>
              <div className="platform-detail">Apple Silicon. M1, M2, M3, M4.</div>
              <div className="platform-arch">arm64 · .dmg</div>
            </div>
          </div>
          <div className="platform-card is-info">
            <div className="platform-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 5.5L11 4v8H3V5.5zm0 13L11 20v-8H3v6.5zM12 4l9-1.5V12h-9V4zm0 16l9 1.5V12h-9v8z"/></svg>
            </div>
            <div className="platform-body">
              <div className="platform-name">Windows</div>
              <div className="platform-detail">Windows 10 and 11.</div>
              <div className="platform-arch">x64 · .msi</div>
            </div>
          </div>
        </div>
        <div className="platforms-cta-wrap">
          <CtaLink className="btn-primary" location="platforms" text="Get Delfi">Get Delfi</CtaLink>
        </div>
      </div>
    </section>
  );
}

// ─── Simulation Mode ─────────────────────────────────────
function Simulation() {
  const [live, setLive] = useState(false);
  return (
    <section className="section sim" id="demo" data-screen-label="08.5 Simulation">
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
                  <div className="sim-bullet-desc">Paste your Polymarket private key into Delfi and toggle to Live. Delfi keeps running at the same settings. Only the capital is real now.</div>
                </div>
              </li>
            </ul>
            <CtaLink className="sim-cta" location="sim" text="Get Delfi">Get Delfi →</CtaLink>
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
    { q: "What exactly is Delfi?", a: "Delfi is an autonomous Polymarket trader that runs entirely on your machine. It continuously scans every active prediction market, produces its own probability forecasts, and backs those forecasts with small, flat-sized stakes, all within the risk limits you set. You install it once like any other desktop app." },
    { q: "Where do my private keys live?", a: "In an encrypted file on your machine, owned only by your user account. Delfi reads them only inside its own process; they never travel to any server we control. We can't see your wallet address even if we wanted to." },
    { q: "How is this different from other Polymarket bots?", a: "Most Polymarket bots are either arbitrage scanners (exploiting price inconsistencies at high speed), copy-trading tools (mirroring top traders), or basic momentum systems. Delfi is none of those. It runs locally on your machine, never custodies your funds, sizes every trade by the same flat-fractional math regardless of how strong the signal looks, and shows you the full reasoning on every position." },
    { q: "What happens if Delfi is wrong?", a: "You lose money on that trade. Delfi is probabilistic, not psychic. It aims to be right more often than wrong, not infallible. Over hundreds of trades, sizing discipline plus following the market favourite compounds into real returns. Daily and weekly loss caps you set during onboarding stop a bad streak from compounding." },
    { q: "How much does it cost?", a: "$249 once. No subscription. All future updates included. Beyond that, you pay your model provider directly for forecasting API usage and Polymarket on-chain fees for trades." },
    { q: "Do I need a Polymarket account first?", a: "Not to start. You can install Delfi and run it in Simulation mode forever, with synthetic capital and the same forecasts and risk math as live mode. When you want to switch to Live trading, you'll need a funded Polymarket account and its private key, both of which you already control." },
    { q: "Is my money safe?", a: "Delfi never custodies your funds. Your capital stays in your own Polymarket wallet. Your private key lives in your OS keychain, not on Delfi servers. Delfi reads it only inside your process, only when signing a trade: never at rest, never in logs, never transmitted. We can't withdraw funds, transfer them, or see your wallet address. You can pause Delfi or delete the app at any time." },
    { q: "Will my Delfi keep working if you go away?", a: "Yes. Delfi runs locally and does not phone home for trading decisions. Once installed, the app runs entirely on your computer." },
    { q: "Can I turn Delfi off?", a: "Any time. The dashboard has an emergency stop button. Open positions stay open until they resolve. No new trades are placed until you turn it back on." },
    { q: "Is this legal?", a: "Polymarket and prediction markets are regulated differently in every jurisdiction. Some permit it, some restrict it, some prohibit it. Confirm legality in your own region before trading. If in doubt, consult a local advisor." },
    { q: "What's the refund policy?", a: "14 days from purchase, as long as you haven't activated your license on any machine. Once activated, the digital good is delivered and the purchase is final. Email info@delfibot.com from the address used at checkout to request a refund within the eligibility window." },
  ];
  return (
    <section className="section faq" id="faq" data-screen-label="11 FAQ">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced">Common questions</h2>
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
    <section className="final-cta" id="pricing" data-screen-label="12 Final CTA">
      <div className="quantum-grid" />
      <div className="container final-inner">
        <h2 className="final-head balanced">Stop reading. Start trading.</h2>
        <p className="final-sub">$249 once. All future updates included.</p>
        <CtaLink className="btn-primary large" location="final" text="Get Delfi">Get Delfi</CtaLink>
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
            <p className="foot-tag">Self-improving autonomous Polymarket bot that trades for you 24/7.</p>
            <span className="foot-contact">info@delfibot.com</span>
          </div>
          <div className="foot-meta">
            <div className="foot-legal-block">
              <div className="foot-heading">Legal</div>
              <ul className="foot-legal">
                <li><Link href="/legal/terms">Terms of Service</Link></li>
                <li><Link href="/legal/privacy">Privacy Policy</Link></li>
                <li><Link href="/legal/cookies">Cookies Policy</Link></li>
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
  useScrollDepthTracking();
  return (
    <>
      <TopNav />
      <Hero />
      <Problem />
      <Pillars />
      <Versus />
      <Proof />
      <CustodyPromise />
      <Simulation />
      <Testimonials />
      <Platforms />
      <FAQ />
      <FinalCTA />
      <Footer />
    </>
  );
}
