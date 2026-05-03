"use client";

import React, { useState, useEffect, useRef } from "react";
import Link from "next/link";
import "./styles/homepage.css";

// Checkout destination. Wired to Stripe / Lemon Squeezy / Polar via
// the NEXT_PUBLIC_CHECKOUT_URL env var when that lands; falls back to
// a mailto so a buyer can still reach the maintainer in the meantime.
// The actual download link is delivered in the post-purchase email,
// never embedded in this page.
const CHECKOUT_URL =
  process.env.NEXT_PUBLIC_CHECKOUT_URL ||
  "mailto:info@delfibot.com?subject=Delfi%20order";

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
  // Don't decorate mailto: fallback — would break the address.
  if (!url.startsWith("http")) return url;
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
  /** Plain-text label for analytics. Defaults to "Try it today" for
   *  back-compat. Pass the visible text when it differs from the
   *  default. */
  text?: string;
}) {
  const ctaText = text ?? "Try it today";
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
          The first autonomous and self-improving Polymarket bot<br />that trades for you 24/7.
        </p>
        <div className="hero-ctas">
          <CtaLink className="btn-primary" location="hero" text="Download Delfi">Download Delfi</CtaLink>
          <a className="btn-ghost" href="#how">See How It Works →</a>
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
    { icon: "shield", title: "Calculates", desc: "Flat fractional position sizing, scaled by per-archetype tuning you control. Drawdown circuit breakers, daily and weekly loss caps. Never over-exposed. Never emotional." },
    { icon: "cycle", title: "Learns", desc: "Tracks its own accuracy by category and Brier score. Every 50 resolved trades it proposes calibrations for you to approve. Delfi gets sharper the longer it runs." },
    { icon: "eye", title: "Explains", desc: "Every trade comes with its full reasoning: the probability estimate, the research sources, the gates it cleared, and the risk logic. You see what Delfi saw. You see why it traded." },
  ];
  return (
    <section className="section solution" data-screen-label="05 Solution">
      <div className="container">
        <div className="sec-head">
          <h2 className="t-display-l balanced">An agent with four minds.</h2>
          <p>Everything a good trader does. Running for you, on your machine, 24/7.</p>
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
    { n: "02", title: "Position Sizer", desc: "Every trade is sized as a small flat fraction of bankroll, scaled by per-archetype multipliers you control. The single gate: Delfi's forecast must agree with the market's pick. If they disagree, Delfi skips the trade." },
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
    ["Probability-based", "✓", "x", "partial", "✓"],
    ["Institutional risk sizing", "x", "partial", "x", "✓"],
    ["Self-calibrating over time", "x", "x", "x", "✓"],
    ["No coding required", "✓", "x", "✓", "✓"],
    ["Full control of risk parameters", "✓", "x", "partial", "✓"],
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
        <p className="vs-foot">Delfi is the only Polymarket trader that combines deep reasoning with institutional risk math while leaving custody entirely with you. Everything else is a subset.</p>
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
          <CtaLink className="platform-card" location="platform-mac" text="Get Delfi for macOS">
            <div className="platform-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="currentColor"><path d="M16.6 12.6c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.1-2.8.9-3.5.9-.7 0-1.9-.8-3.1-.8-1.6 0-3.1.9-3.9 2.4-1.7 2.9-.4 7.2 1.2 9.5.8 1.1 1.7 2.4 2.9 2.4 1.2 0 1.6-.8 3-.8s1.8.8 3 .8 2-1.2 2.8-2.3c.9-1.3 1.2-2.6 1.2-2.7 0 0-2.3-.9-2.3-3.6zM14.4 5.7c.6-.7 1-1.7.9-2.7-.9.1-2 .6-2.6 1.3-.6.6-1.1 1.6-.9 2.6.9.1 1.9-.5 2.6-1.2z"/></svg>
            </div>
            <div className="platform-body">
              <div className="platform-name">macOS</div>
              <div className="platform-detail">Apple Silicon. M1, M2, M3, M4.</div>
              <div className="platform-arch">arm64 · .dmg</div>
            </div>
            <span className="platform-cta">Get Delfi</span>
          </CtaLink>
          <CtaLink className="platform-card" location="platform-win" text="Get Delfi for Windows">
            <div className="platform-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 5.5L11 4v8H3V5.5zm0 13L11 20v-8H3v6.5zM12 4l9-1.5V12h-9V4zm0 16l9 1.5V12h-9v8z"/></svg>
            </div>
            <div className="platform-body">
              <div className="platform-name">Windows</div>
              <div className="platform-detail">Windows 10 and 11.</div>
              <div className="platform-arch">x64 · .msi</div>
            </div>
            <span className="platform-cta">Get Delfi</span>
          </CtaLink>
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
                  <div className="sim-bullet-desc">Paste your Polymarket private key into the OS keychain and toggle to Live. Delfi keeps running at the same settings. Only the capital is real now.</div>
                </div>
              </li>
            </ul>
            <CtaLink className="sim-cta" location="sim" text="Start free in Simulation">Start free in Simulation →</CtaLink>
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
            <h2 className="newhere-head balanced">Delfi turns anyone into a probabilistic forecaster.</h2>
            <p className="newhere-body">Delfi reads every market on Polymarket, builds its own probability for each one, and trades the ones where the read is strong enough. You don&apos;t need to know what an order book is, what calibration means, or how to size a trade. Delfi does the work.</p>
            <p className="newhere-body muted">The crowd is often wrong. People bet on what they want to be true, anchor on headlines, and ignore base rates. A calibrated forecaster, running 24/7, finds the gaps. That&apos;s Delfi.</p>
            <p className="newhere-body muted">New to prediction markets? Polymarket is a marketplace for real-world questions. Each question trades between 0% and 100%, and the price is the crowd&apos;s probability. A question at 44% means the market thinks there&apos;s a 44% chance it resolves yes.</p>
            <CtaLink className="newhere-cta" location="newhere" text="Try Delfi free">Try Delfi free →</CtaLink>
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

// ─── FAQ ──────────────────────────────────────────────────
function FAQ() {
  const [open, setOpen] = useState(0);
  const items = [
    { q: "What exactly is Delfi?", a: "Delfi is an autonomous Polymarket trader that runs entirely on your machine. It continuously scans every active prediction market, produces its own probability forecasts, and backs those forecasts with small, flat-sized stakes, all within the risk limits you set. You install it once like any other desktop app." },
    { q: "Where do my private keys live?", a: "In your operating system's keychain (macOS Keychain, Windows Credential Locker). Delfi reads them only inside your own process; they never travel to any server we control. We can't see your wallet address even if we wanted to." },
    { q: "How is this different from other Polymarket bots?", a: "Most Polymarket bots are either arbitrage scanners (exploiting price inconsistencies at high speed), copy-trading tools (mirroring top traders), or basic momentum systems. Delfi is none of those. It's a reasoning-based agent that evaluates each market the way a sharp human trader would: research, probability modeling, calibrated risk sizing, and full transparency on every trade." },
    { q: "What happens if Delfi is wrong?", a: "You lose money on that trade. Delfi is probabilistic, not psychic. It aims to be right more often than wrong, not infallible. Over hundreds of trades, calibrated forecasting compounds into real returns. Daily and weekly loss caps you set during onboarding stop a bad streak from compounding." },
    { q: "How much does it cost?", a: "$199 once. No subscription. All future updates included. Beyond that, you pay your model provider directly for forecasting API usage and Polymarket on-chain fees for trades." },
    { q: "Do I need a Polymarket account first?", a: "Not to start. You can install Delfi and run it in Simulation mode forever, with synthetic capital and the same forecasts and risk math as live mode. When you want to switch to Live trading, you'll need a funded Polymarket account and its private key, both of which you already control." },
    { q: "Is my money safe?", a: "Delfi never custodies your funds. Your capital stays in your own Polymarket wallet. Your private key lives in your OS keychain, not on Delfi servers. Delfi reads it only inside your process, only when signing a trade: never at rest, never in logs, never transmitted. We can't withdraw funds, transfer them, or see your wallet address. You can pause Delfi or delete the app at any time." },
    { q: "Will my Delfi keep working if you go away?", a: "Yes. Delfi runs locally and does not phone home for trading decisions. Once installed, the app runs entirely on your computer." },
    { q: "Can I turn Delfi off?", a: "Any time. The dashboard has an emergency stop button. Open positions stay open until they resolve. No new trades are placed until you turn it back on." },
    { q: "Is this legal?", a: "Polymarket and prediction markets are regulated differently in every jurisdiction. Some permit it, some restrict it, some prohibit it. Confirm legality in your own region before trading. If in doubt, consult a local advisor." },
    { q: "What's the refund policy?", a: "14 days, no questions asked, provided you have not yet placed a live trade through the app." },
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
    <section className="final-cta" data-screen-label="12 Final CTA">
      <div className="quantum-grid" />
      <div className="container final-inner">
        <h2 className="final-head balanced">Stop reading. Start trading.</h2>
        <p className="final-sub">$199 once. All future updates included.</p>
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
            <p className="foot-tag">Autonomous Polymarket trader. Runs on your machine. Your keys never leave.</p>
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
      <Solution />
      <Pillars />
      <Versus />
      <CustodyPromise />
      <Simulation />
      <NewHere />
      <Platforms />
      <FAQ />
      <FinalCTA />
      <Footer />
    </>
  );
}
