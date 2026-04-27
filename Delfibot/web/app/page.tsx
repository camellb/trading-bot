import Link from "next/link";

// Marketing landing page. Static, no client JS, no data fetches.
//
// Voice rules from CLAUDE.md:
// - Persona is oracle / prophecy in marketing copy.
// - Product surfaces use clinical precision (the dashboard inside the
//   app, not this page).
// - Banned: em dashes, "edge", "Claude/Anthropic/LLM/prompt".
// - ROI is the most prominent metric.

export default function HomePage() {
  return (
    <main className="aurora min-h-screen">
      <Nav />
      <Hero />
      <Doctrine />
      <HowItWorks />
      <Pricing />
      <FAQ />
      <Footer />
    </main>
  );
}

function Nav() {
  return (
    <nav className="mx-auto flex max-w-6xl items-center justify-between px-6 py-6">
      <Link href="/" className="flex items-center gap-2">
        <span className="text-xl font-semibold tracking-[0.18em] text-white">
          DELFI
        </span>
      </Link>
      <div className="flex items-center gap-6 text-sm text-slate-300">
        <a href="#how" className="hover:text-white">How it works</a>
        <a href="#pricing" className="hover:text-white">Pricing</a>
        <a href="#faq" className="hover:text-white">FAQ</a>
        <Link
          href="/download"
          className="rounded-md bg-[var(--brand-accent)] px-4 py-2 font-medium text-slate-900 hover:bg-[var(--brand-accent-strong)]"
        >
          Download
        </Link>
      </div>
    </nav>
  );
}

function Hero() {
  return (
    <section className="mx-auto max-w-5xl px-6 pt-16 pb-24 text-center">
      <p className="mb-6 text-sm uppercase tracking-[0.4em] text-[var(--brand-accent)]">
        An oracle for prediction markets
      </p>
      <h1 className="mx-auto max-w-4xl text-5xl font-semibold leading-tight text-white sm:text-6xl">
        Back the favourite.
        <br />
        Skip the doubt.
      </h1>
      <p className="mx-auto mt-6 max-w-2xl text-lg text-slate-300">
        Delfi watches Polymarket, backs the side the market itself favours
        on every tradeable contract, and steps aside whenever its own
        forecast disagrees with the price. It runs on your machine. You
        hold your own keys.
      </p>
      <div className="mt-10 flex items-center justify-center gap-4">
        <Link
          href="/download"
          className="rounded-md bg-[var(--brand-accent)] px-6 py-3 font-medium text-slate-900 hover:bg-[var(--brand-accent-strong)]"
        >
          Download for $250
        </Link>
        <a
          href="#how"
          className="rounded-md border border-slate-700 px-6 py-3 font-medium text-slate-200 hover:border-slate-500"
        >
          See how it works
        </a>
      </div>
      <p className="mt-6 text-xs text-slate-500">
        One-time purchase. Lifetime updates. macOS, Windows, Linux.
      </p>
    </section>
  );
}

function Doctrine() {
  const tenets = [
    {
      title: "Follow the market",
      body:
        "Delfi backs the side the market itself prices as more likely. It does not bet against the price. The forecaster's job is the veto, not the pick.",
    },
    {
      title: "Stake small, learn fast",
      body:
        "Flat sizing, scaled only by per-archetype tuning. Variance per trade stays low so the bankroll learns faster than it bleeds.",
    },
    {
      title: "Every settled trade is a lesson",
      body:
        "Calibration, ROI, and skip-list updates are recomputed every 50 settled trades. The bot proposes; you approve.",
    },
    {
      title: "Your funds, your keys",
      body:
        "Polymarket private key and API keys live in your OS keychain. Delfi never sees them. We never custody anything.",
    },
  ];
  return (
    <section className="mx-auto max-w-6xl px-6 py-16">
      <div className="grid gap-6 md:grid-cols-2">
        {tenets.map((t) => (
          <div
            key={t.title}
            className="rounded-xl border border-slate-800 bg-[var(--brand-panel)] p-6"
          >
            <h3 className="mb-2 text-lg font-semibold text-white">
              {t.title}
            </h3>
            <p className="text-sm leading-relaxed text-slate-300">{t.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function HowItWorks() {
  const steps = [
    {
      n: "01",
      title: "Install Delfi",
      body:
        "Download the signed installer for your OS. The app launches its own local engine; nothing runs in the cloud.",
    },
    {
      n: "02",
      title: "Connect your accounts",
      body:
        "Paste your Polymarket private key and API key once. Both are stored in your OS keychain and never leave your machine.",
    },
    {
      n: "03",
      title: "Watch Delfi work",
      body:
        "Delfi scans Polymarket on a schedule, forecasts each market, and stakes a small bet when the math favours it. The dashboard shows every reasoning step in real time.",
    },
    {
      n: "04",
      title: "Approve what it learns",
      body:
        "Every 50 settled trades Delfi proposes config changes with backtest evidence. You review and apply. No autonomous parameter drift.",
    },
  ];
  return (
    <section id="how" className="mx-auto max-w-6xl px-6 py-20">
      <h2 className="mb-12 text-center text-4xl font-semibold text-white">
        How it works
      </h2>
      <div className="grid gap-8 md:grid-cols-2">
        {steps.map((s) => (
          <div key={s.n} className="flex gap-5">
            <div className="text-3xl font-semibold text-[var(--brand-accent)]">
              {s.n}
            </div>
            <div>
              <h3 className="mb-1 text-lg font-semibold text-white">
                {s.title}
              </h3>
              <p className="text-sm leading-relaxed text-slate-300">{s.body}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function Pricing() {
  return (
    <section id="pricing" className="mx-auto max-w-3xl px-6 py-20">
      <h2 className="mb-3 text-center text-4xl font-semibold text-white">
        One price. Lifetime updates.
      </h2>
      <p className="mb-12 text-center text-slate-400">
        Buy Delfi once. Every future release is yours.
      </p>
      <div className="rounded-2xl border border-[var(--brand-accent)]/40 bg-[var(--brand-panel)] p-10 text-center">
        <p className="text-sm uppercase tracking-[0.3em] text-[var(--brand-accent)]">
          Delfi Desktop
        </p>
        <p className="mt-4 text-6xl font-semibold text-white">$250</p>
        <p className="mt-2 text-sm text-slate-400">one-time, no subscription</p>
        <ul className="mx-auto mt-8 max-w-md space-y-3 text-left text-sm text-slate-300">
          <li className="flex gap-3">
            <Check />
            <span>Autonomous Polymarket trader, runs on your machine</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>BYO Polymarket key and API key</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>Real-time forecasting dashboard</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>Continuous learning from every resolved market</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>Lifetime updates, every release shipped to your machine</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>macOS (arm64 and x86_64), Windows x64, Linux x64</span>
          </li>
        </ul>
        <Link
          href="/download"
          className="mt-10 inline-block rounded-md bg-[var(--brand-accent)] px-8 py-3 font-medium text-slate-900 hover:bg-[var(--brand-accent-strong)]"
        >
          Buy and download
        </Link>
        <p className="mt-4 text-xs text-slate-500">
          API costs are billed by your provider directly. Polymarket fees
          are paid on-chain.
        </p>
      </div>
    </section>
  );
}

function Check() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="mt-0.5 h-5 w-5 shrink-0 text-[var(--brand-good)]"
      fill="currentColor"
      aria-hidden="true"
    >
      <path
        fillRule="evenodd"
        d="M16.7 5.3a1 1 0 0 1 0 1.4l-7.5 7.5a1 1 0 0 1-1.4 0l-3.5-3.5a1 1 0 1 1 1.4-1.4l2.8 2.8 6.8-6.8a1 1 0 0 1 1.4 0z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function FAQ() {
  const items = [
    {
      q: "Where do my private keys live?",
      a: "In your operating system's keychain (macOS Keychain, Windows Credential Locker, Linux Secret Service). Delfi reads them only inside your machine; they never travel to any server we control.",
    },
    {
      q: "How does Delfi make trading decisions?",
      a: "Delfi follows the market favourite on every tradeable contract. Before placing a trade it runs an independent forecast. If that forecast disagrees with the price, it skips. If it agrees, it stakes a small flat fraction of bankroll, adjusted by per-archetype tuning.",
    },
    {
      q: "What does it cost to run beyond the $250?",
      a: "Forecasting API usage (charged by your provider) and Polymarket trading fees (charged on-chain). Delfi caps how often it scans each market to keep API spend predictable.",
    },
    {
      q: "Can I lose money?",
      a: "Yes. Markets can be wrong about anything, and so can a model. Delfi includes circuit breakers (daily loss limit, drawdown halt, streak cooldown) you can tune in the dashboard. Set them conservatively.",
    },
    {
      q: "Will my Delfi keep working if you go away?",
      a: "Yes. Delfi runs locally. The license check is a yearly online verification; if our verification endpoint is down for an extended period, Delfi falls back to an offline grace mode.",
    },
    {
      q: "What's the refund policy?",
      a: "14 days, no questions asked, provided you have not placed live trades through the app.",
    },
  ];
  return (
    <section id="faq" className="mx-auto max-w-3xl px-6 py-20">
      <h2 className="mb-12 text-center text-4xl font-semibold text-white">
        Questions
      </h2>
      <div className="space-y-4">
        {items.map((it) => (
          <details
            key={it.q}
            className="rounded-lg border border-slate-800 bg-[var(--brand-panel)] p-5"
          >
            <summary className="cursor-pointer list-none text-base font-medium text-white">
              {it.q}
            </summary>
            <p className="mt-3 text-sm leading-relaxed text-slate-300">
              {it.a}
            </p>
          </details>
        ))}
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="mt-12 border-t border-slate-800 px-6 py-10 text-center text-xs text-slate-500">
      <p>
        Delfi forecasts. You decide. Prediction markets carry real risk; trade
        only what you can afford to lose.
      </p>
      <p className="mt-2">
        &copy; {new Date().getFullYear()} Delfi. All rights reserved.
      </p>
    </footer>
  );
}
