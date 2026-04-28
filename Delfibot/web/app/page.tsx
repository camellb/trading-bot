import Link from "next/link";

// Marketing landing page for the Delfi desktop app. Mirrors the SaaS
// site's structure (the "four minds" / "three systems" / comparison
// table / simulation->live walkthrough / Polymarket explainer) and
// voice (oracle eyebrow, declarative pithy headers), but rewritten
// for the local-first positioning: keys never leave your machine,
// one-time fee, no custody, no subscription.
//
// Voice rules from CLAUDE.md:
// - Persona is oracle / prophecy in marketing copy.
// - Banned: em dashes, "edge", "shadow", "Claude/Anthropic/LLM/prompt".
// - Numbers prominent. Pithy declarative one-liners.

export default function HomePage() {
  return (
    <main className="aurora min-h-screen">
      <Nav />
      <Hero />
      <Problem />
      <FourMinds />
      <ThreeSystems />
      <Comparison />
      <CustodyPromise />
      <Platforms />
      <SimulationToLive />
      <PolymarketExplainer />
      <Pricing />
      <FAQ />
      <FinalCTA />
      <Footer />
    </main>
  );
}

// ── Nav ────────────────────────────────────────────────────────────────

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
        <a href="#vs" className="hover:text-white">Us vs Them</a>
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

// ── Hero ───────────────────────────────────────────────────────────────

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
        The first autonomous Polymarket trader that runs entirely on your
        machine. Your wallet key never leaves your laptop. Your reasoning
        is yours alone.
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
        One-time purchase. Lifetime updates. macOS and Windows.
      </p>
    </section>
  );
}

// ── Problem ────────────────────────────────────────────────────────────

function Problem() {
  return (
    <section className="mx-auto max-w-5xl px-6 py-20 text-center">
      <h2 className="mx-auto max-w-3xl text-4xl font-semibold leading-tight text-white sm:text-5xl">
        You&apos;re not losing to the market.
        <br />
        You&apos;re losing to the machines.
      </h2>
      <p className="mx-auto mt-6 max-w-2xl text-lg text-slate-300">
        Polymarket has become an algorithmic battleground. Bots read
        faster, trade faster, never sleep. Every forecast you build has
        probably already been priced in by a bot before you finished
        reading the question.
      </p>
      <div className="mx-auto mt-12 grid max-w-3xl gap-4 sm:grid-cols-3">
        <Stat
          value="14/20"
          label="of the most profitable Polymarket wallets are bots"
        />
        <Stat
          value="$40M+"
          label="extracted by arbitrage bots in the last 12 months"
        />
        <Stat
          value="25/day"
          label="trades the average active Polymarket user now makes"
        />
      </div>
      <p className="mx-auto mt-12 max-w-2xl text-lg text-slate-300">
        The problem isn&apos;t what you know. It&apos;s how fast you can
        act on it.
      </p>
      <p className="mx-auto mt-4 max-w-2xl text-base text-slate-400">
        Delfi runs that same kind of bot. The difference: it runs on
        your machine, with your keys, and shows you every reasoning step
        before any dollar moves.
      </p>
    </section>
  );
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-[var(--brand-panel)] p-6">
      <p className="text-3xl font-semibold text-[var(--brand-accent)]">
        {value}
      </p>
      <p className="mt-2 text-sm text-slate-400">{label}</p>
    </div>
  );
}

// ── Four minds ─────────────────────────────────────────────────────────

function FourMinds() {
  const minds = [
    {
      name: "Thinks",
      body:
        "Reads the news. Checks historical base rates. Weighs structured data. Produces its own probability estimate for every market.",
    },
    {
      name: "Calculates",
      body:
        "Flat fractional sizing, scaled by per-archetype tuning. Drawdown circuit breakers, daily and weekly loss caps. Never over-exposed. Never emotional.",
    },
    {
      name: "Learns",
      body:
        "Tracks its own accuracy by category and Brier score. Every 50 resolved trades, it proposes calibrations for you to approve. Delfi gets sharper the longer it runs.",
    },
    {
      name: "Explains",
      body:
        "Every trade comes with its full reasoning: the probability estimate, the research sources, the gates it cleared, the risk logic. You see what Delfi saw. You see why it traded.",
    },
  ];
  return (
    <section className="mx-auto max-w-6xl px-6 py-20">
      <h2 className="mb-3 text-center text-4xl font-semibold text-white">
        An agent with four minds.
      </h2>
      <p className="mx-auto mb-12 max-w-2xl text-center text-lg text-slate-300">
        Everything a good trader does. Running for you, on your machine,
        24/7.
      </p>
      <div className="grid gap-6 md:grid-cols-2">
        {minds.map((m) => (
          <div
            key={m.name}
            className="rounded-xl border border-slate-800 bg-[var(--brand-panel)] p-6"
          >
            <h3 className="mb-2 text-lg font-semibold text-[var(--brand-accent)]">
              {m.name}
            </h3>
            <p className="text-sm leading-relaxed text-slate-300">{m.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

// ── Three independent systems ──────────────────────────────────────────

function ThreeSystems() {
  const systems = [
    {
      n: "01",
      title: "Probability Engine",
      body:
        "Delfi reads every active market and produces its own probability estimate, grounded in news, historical base rates, and structured data.",
    },
    {
      n: "02",
      title: "Position Sizer",
      body:
        "Every trade is sized as a small flat fraction of bankroll, scaled by per-archetype multipliers you control. The single gate: Delfi's forecast must agree with the market's pick. If they disagree, Delfi skips the trade.",
    },
    {
      n: "03",
      title: "Risk Manager",
      body:
        "Before any trade executes, Delfi checks the portfolio: daily loss cap, weekly loss cap, drawdown halt, streak cooldown, dry-powder reserve. If the book is already stressed, Delfi passes.",
    },
  ];
  return (
    <section id="how" className="mx-auto max-w-6xl px-6 py-20">
      <h2 className="mb-3 text-center text-4xl font-semibold text-white">
        Every trade passes through three independent systems.
      </h2>
      <p className="mx-auto mb-12 max-w-2xl text-center text-lg text-slate-300">
        No single component can override the others. No black boxes. Each
        layer is auditable in the dashboard.
      </p>
      <div className="grid gap-6 md:grid-cols-3">
        {systems.map((s) => (
          <div
            key={s.n}
            className="rounded-xl border border-slate-800 bg-[var(--brand-panel)] p-6"
          >
            <p className="mb-3 text-3xl font-semibold text-[var(--brand-accent)]">
              {s.n}
            </p>
            <h3 className="mb-2 text-lg font-semibold text-white">
              {s.title}
            </h3>
            <p className="text-sm leading-relaxed text-slate-300">{s.body}</p>
          </div>
        ))}
      </div>
      <div className="mt-10 flex items-center justify-center gap-3 text-xs uppercase tracking-[0.3em] text-slate-500">
        <span>Scan</span>
        <Arrow />
        <span>Estimate</span>
        <Arrow />
        <span>Size</span>
        <Arrow />
        <span>Verify</span>
        <Arrow />
        <span>Execute</span>
      </div>
    </section>
  );
}

function Arrow() {
  return (
    <svg
      className="h-3 w-3 text-slate-700"
      viewBox="0 0 12 12"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M3 1l5 5-5 5V1z" />
    </svg>
  );
}

// ── Comparison table ───────────────────────────────────────────────────

function Comparison() {
  const rows: { label: string; values: ("yes" | "no" | "partial")[] }[] = [
    { label: "Trades 24/7 automatically",        values: ["no",  "yes",     "yes",     "yes"] },
    { label: "Reasoning transparent per trade",  values: ["partial", "no",  "no",      "yes"] },
    { label: "Probability-based",                values: ["yes", "no",      "partial", "yes"] },
    { label: "Institutional risk sizing",        values: ["no",  "partial", "no",      "yes"] },
    { label: "Self-calibrating over time",       values: ["no",  "no",      "no",      "yes"] },
    { label: "No coding required",               values: ["yes", "no",      "yes",     "yes"] },
    { label: "Full control of risk parameters",  values: ["yes", "no",      "partial", "yes"] },
    { label: "Works across all market categories", values: ["partial", "no", "partial", "yes"] },
    { label: "Runs on your machine",             values: ["yes", "no",      "no",      "yes"] },
    { label: "Your private key never leaves",    values: ["yes", "no",      "no",      "yes"] },
  ];
  const cols = ["Manual Trading", "Arbitrage Bots", "Copy Trading", "Delfi"];
  return (
    <section id="vs" className="mx-auto max-w-6xl px-6 py-20">
      <h2 className="mb-3 text-center text-4xl font-semibold text-white">
        Three kinds of Polymarket bots ... and then there&apos;s Delfi.
      </h2>
      <p className="mx-auto mb-12 max-w-3xl text-center text-lg text-slate-300">
        Arbitrage bots compete on speed. Copy-trading tools compete on who
        to follow. Delfi competes on accuracy, discipline, and
        transparency, all at once.
      </p>
      <div className="overflow-x-auto rounded-xl border border-slate-800 bg-[var(--brand-panel)]">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-slate-400">
              <th className="px-5 py-4 text-left font-medium">Capability</th>
              {cols.map((c, i) => (
                <th
                  key={c}
                  className={
                    "px-5 py-4 text-center font-medium " +
                    (i === cols.length - 1
                      ? "text-[var(--brand-accent)]"
                      : "")
                  }
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.label}
                className="border-b border-slate-800/60 last:border-0"
              >
                <td className="px-5 py-3 text-slate-300">{r.label}</td>
                {r.values.map((v, i) => (
                  <td key={i} className="px-5 py-3 text-center">
                    <Mark value={v} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mx-auto mt-8 max-w-2xl text-center text-sm text-slate-400">
        Delfi is the only Polymarket trader that combines deep reasoning
        with institutional risk math, while leaving custody entirely with
        you. Everything else is a subset.
      </p>
    </section>
  );
}

function Mark({ value }: { value: "yes" | "no" | "partial" }) {
  if (value === "yes") {
    return (
      <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-[var(--brand-good)]/15 text-[var(--brand-good)]">
        <Check />
      </span>
    );
  }
  if (value === "no") {
    return <span className="text-lg text-[var(--brand-bad)]">&#10005;</span>;
  }
  return <span className="text-xs uppercase tracking-wider text-slate-500">partial</span>;
}

// ── Custody promise (the local-first pitch) ────────────────────────────

function CustodyPromise() {
  return (
    <section className="mx-auto max-w-5xl px-6 py-20">
      <div className="rounded-2xl border border-[var(--brand-accent)]/40 bg-[var(--brand-panel)] p-10 sm:p-14">
        <p className="mb-3 text-sm uppercase tracking-[0.4em] text-[var(--brand-accent)]">
          The local-first promise
        </p>
        <h2 className="mb-6 text-4xl font-semibold leading-tight text-white sm:text-5xl">
          Your funds. Your keys. Your machine.
        </h2>
        <p className="mb-6 text-lg leading-relaxed text-slate-300">
          Delfi installs and runs entirely on your laptop. Your Polymarket
          private key sits in your operating system&apos;s keychain, where
          it has always lived. Delfi reads it only when it places a trade,
          and only inside your own process. Nothing about your wallet ever
          leaves the machine you&apos;re reading this on.
        </p>
        <ul className="mb-8 space-y-3 text-base text-slate-200">
          <li className="flex gap-3">
            <Check />
            <span>We never see your wallet address.</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>We never custody your capital.</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>We never know which trades you make.</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>
              We could go offline tomorrow and your bot would keep
              running.
            </span>
          </li>
        </ul>
        <p className="text-sm text-slate-400">
          The trade-off: you bring your own Polymarket key and your own
          model API key. Delfi caps how often it scans each market so the
          API spend stays predictable. The dashboard shows every cost in
          real time.
        </p>
      </div>
    </section>
  );
}

// ── Platform availability (macOS + Windows) ────────────────────────────

function Platforms() {
  return (
    <section id="platforms" className="mx-auto max-w-5xl px-6 py-20">
      <h2 className="mb-3 text-center text-4xl font-semibold text-white">
        Available for macOS and Windows.
      </h2>
      <p className="mx-auto mb-12 max-w-2xl text-center text-lg text-slate-300">
        Same license. Same dashboard. Same engine. Install on every
        machine you own; run wherever your laptop is awake.
      </p>
      <div className="grid gap-6 sm:grid-cols-2">
        <PlatformCard
          icon={<MacIcon />}
          name="macOS"
          detail="Apple Silicon. M1, M2, M3, M4."
          arch="arm64"
          suffix=".dmg"
        />
        <PlatformCard
          icon={<WindowsIcon />}
          name="Windows"
          detail="Windows 10 and 11."
          arch="x64"
          suffix=".msi"
        />
      </div>
      <p className="mx-auto mt-10 max-w-xl text-center text-xs text-slate-500">
        Delfi is not yet code-signed by Apple or Microsoft. First launch
        will show a Gatekeeper or SmartScreen warning; click through once
        and the app installs normally.
      </p>
    </section>
  );
}

function PlatformCard({
  icon,
  name,
  detail,
  arch,
  suffix,
}: {
  icon: React.ReactNode;
  name: string;
  detail: string;
  arch: string;
  suffix: string;
}) {
  return (
    <Link
      href="/download"
      className="group flex items-center justify-between rounded-2xl border border-slate-800 bg-[var(--brand-panel)] p-7 transition hover:border-[var(--brand-accent)]"
    >
      <div className="flex items-center gap-5">
        <div className="text-slate-300 transition group-hover:text-[var(--brand-accent)]">
          {icon}
        </div>
        <div>
          <p className="text-xl font-semibold text-white">{name}</p>
          <p className="text-sm text-slate-400">{detail}</p>
          <p className="mt-1 text-xs uppercase tracking-wider text-slate-500">
            {arch} &middot; {suffix}
          </p>
        </div>
      </div>
      <span className="rounded-md bg-[var(--brand-accent)] px-4 py-2 text-sm font-medium text-slate-900 transition group-hover:bg-[var(--brand-accent-strong)]">
        Download
      </span>
    </Link>
  );
}

function MacIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="32"
      height="32"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M16.6 12.6c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.1-2.8.9-3.5.9-.7 0-1.9-.8-3.1-.8-1.6 0-3.1.9-3.9 2.4-1.7 2.9-.4 7.2 1.2 9.5.8 1.1 1.7 2.4 2.9 2.4 1.2 0 1.6-.8 3-.8s1.8.8 3 .8 2-1.2 2.8-2.3c.9-1.3 1.2-2.6 1.2-2.7 0 0-2.3-.9-2.3-3.6zM14.4 5.7c.6-.7 1-1.7.9-2.7-.9.1-2 .6-2.6 1.3-.6.6-1.1 1.6-.9 2.6.9.1 1.9-.5 2.6-1.2z" />
    </svg>
  );
}

function WindowsIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="32"
      height="32"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M3 5.5L11 4v8H3V5.5zm0 13L11 20v-8H3v6.5zM12 4l9-1.5V12h-9V4zm0 16l9 1.5V12h-9v8z" />
    </svg>
  );
}

// ── Simulation -> Live walkthrough ─────────────────────────────────────

function SimulationToLive() {
  const steps = [
    {
      title: "Try Delfi without risking a cent.",
      body:
        "Start in Simulation mode. Every decision Delfi would make live, with paper capital. Same forecasts. Same sizing. Same risk caps.",
    },
    {
      title: "Watch Delfi prove itself.",
      body:
        "Let it run for a day or a week. Read the reasoning, check the P&L, see how it handles volatile news. You decide when the track record is enough.",
    },
    {
      title: "Switch to Live when the numbers convince you.",
      body:
        "Paste your Polymarket private key into the OS keychain and toggle to Live. Delfi keeps running at the same settings. Only the capital is real now.",
    },
  ];
  return (
    <section className="mx-auto max-w-6xl px-6 py-20">
      <h2 className="mb-12 text-center text-4xl font-semibold text-white">
        Simulation first. Live when you&apos;re ready.
      </h2>
      <div className="grid gap-8 md:grid-cols-3">
        {steps.map((s, i) => (
          <div key={s.title} className="flex gap-5">
            <div className="text-3xl font-semibold text-[var(--brand-accent)]">
              {String(i + 1).padStart(2, "0")}
            </div>
            <div>
              <h3 className="mb-2 text-lg font-semibold text-white">
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

// ── Polymarket explainer (for newcomers) ───────────────────────────────

function PolymarketExplainer() {
  return (
    <section className="mx-auto max-w-4xl px-6 py-20">
      <h2 className="mb-6 text-center text-3xl font-semibold text-white">
        New to Polymarket?
      </h2>
      <div className="space-y-5 text-base leading-relaxed text-slate-300">
        <p>
          Polymarket is a marketplace for real-world questions. Each
          question trades between 0% and 100%, and the price is the
          crowd&apos;s probability. A question trading at 44% means the
          market thinks there&apos;s a 44% chance it resolves yes.
        </p>
        <p>
          But the markets are often wrong. People bet on what they want
          to be true. They anchor on headlines and ignore base rates. A
          patient reader can forecast outcomes more accurately than the
          crowd. The hard part is doing it consistently, sizing each trade
          correctly, and walking away when the read isn&apos;t strong
          enough.
        </p>
        <p>
          Delfi does all of that for you. It reads every tradeable market,
          builds its own forecast, sizes each trade, and acts when the
          forecast clears every gate. You don&apos;t need to be a
          prediction market expert. You just need a machine to run Delfi
          on.
        </p>
      </div>
    </section>
  );
}

// ── Pricing ────────────────────────────────────────────────────────────

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
        <p className="mt-2 text-sm text-slate-400">
          one-time, no subscription
        </p>
        <ul className="mx-auto mt-8 max-w-md space-y-3 text-left text-sm text-slate-300">
          <li className="flex gap-3">
            <Check />
            <span>Autonomous Polymarket trader, runs on your machine</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>BYO Polymarket key and model API key</span>
          </li>
          <li className="flex gap-3">
            <Check />
            <span>Real-time forecasting dashboard with full reasoning</span>
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
            <span>macOS (Apple Silicon) and Windows 10 and 11 (x64)</span>
          </li>
        </ul>
        <Link
          href="/download"
          className="mt-10 inline-block rounded-md bg-[var(--brand-accent)] px-8 py-3 font-medium text-slate-900 hover:bg-[var(--brand-accent-strong)]"
        >
          Buy and download
        </Link>
        <p className="mt-4 text-xs text-slate-500">
          Model API costs are billed by your provider directly. Polymarket
          fees are paid on-chain.
        </p>
      </div>
    </section>
  );
}

// ── FAQ ────────────────────────────────────────────────────────────────

function FAQ() {
  const items = [
    {
      q: "Where do my private keys live?",
      a: "In your operating system's keychain (macOS Keychain, Windows Credential Locker). Delfi reads them only inside your own process; they never travel to any server we control. We can't see your wallet address even if we wanted to.",
    },
    {
      q: "How does Delfi make trading decisions?",
      a: "Delfi follows the market favourite on every tradeable contract. Before placing a trade it runs an independent forecast. If the forecast disagrees with the price, it skips. If it agrees, it stakes a small flat fraction of bankroll, scaled by per-archetype tuning that you control.",
    },
    {
      q: "Is this different from arbitrage bots and copy-trading tools?",
      a: "Yes. Arbitrage bots compete on speed and exploit price inconsistencies. Copy-trading mirrors top traders. Delfi reasons about each market the way a sharp human trader would, with research, probability modeling, and calibrated risk sizing. See the comparison table above.",
    },
    {
      q: "What happens if Delfi is wrong?",
      a: "You lose money on that trade. Delfi is probabilistic, not psychic. It aims to be right more often than wrong, not infallible. Over hundreds of trades, calibrated forecasting compounds into real returns. Daily and weekly loss caps you set during onboarding stop a bad streak from compounding.",
    },
    {
      q: "What does it cost to run beyond the $250?",
      a: "Forecasting API usage (charged by your model provider directly) and Polymarket trading fees (paid on-chain). Delfi caps how often it scans each market to keep API spend predictable. Most users see a few dollars per day in API costs at default settings.",
    },
    {
      q: "Do I need a Polymarket account first?",
      a: "Not to start. You can install Delfi and run it in Simulation mode forever, with synthetic capital and the same forecasts and risk math as live mode. When you want to switch to Live trading, you'll need a funded Polymarket account and its private key, both of which you already control.",
    },
    {
      q: "Can I lose money?",
      a: "Yes. Markets can be wrong about anything, and so can a model. Delfi includes circuit breakers (daily loss limit, weekly loss limit, drawdown halt, streak cooldown) you can tune in the dashboard. Set them conservatively. Trade only what you can afford to lose.",
    },
    {
      q: "Will my Delfi keep working if you go away?",
      a: "Yes. Delfi runs locally and does not phone home for trading decisions. The license check is a yearly online verification; if our verification endpoint is down for an extended period, Delfi falls back to an offline grace mode and keeps trading.",
    },
    {
      q: "Can I turn Delfi off?",
      a: "Any time. The dashboard has an emergency stop button. Open positions stay open until they resolve. No new trades are placed until you turn it back on.",
    },
    {
      q: "Is this legal where I live?",
      a: "Polymarket and prediction markets are regulated differently in every jurisdiction. Some permit it, some restrict it, some prohibit it. Confirm legality in your own region before trading. If in doubt, consult a local advisor.",
    },
    {
      q: "What's the refund policy?",
      a: "14 days, no questions asked, provided you have not yet placed a live trade through the app.",
    },
  ];
  return (
    <section id="faq" className="mx-auto max-w-3xl px-6 py-20">
      <h2 className="mb-12 text-center text-4xl font-semibold text-white">
        Q&amp;A before you sign up
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

// ── Final CTA ──────────────────────────────────────────────────────────

function FinalCTA() {
  return (
    <section className="mx-auto max-w-3xl px-6 py-24 text-center">
      <h2 className="text-4xl font-semibold leading-tight text-white sm:text-5xl">
        Stop reading.
        <br />
        Start trading.
      </h2>
      <p className="mx-auto mt-6 max-w-xl text-base text-slate-300">
        Install Delfi in three minutes. It will take care of the rest.
      </p>
      <div className="mt-10 flex items-center justify-center gap-4">
        <Link
          href="/download"
          className="rounded-md bg-[var(--brand-accent)] px-8 py-3 font-medium text-slate-900 hover:bg-[var(--brand-accent-strong)]"
        >
          Download for $250
        </Link>
      </div>
    </section>
  );
}

// ── Footer + shared icon ──────────────────────────────────────────────

function Footer() {
  return (
    <footer className="mt-12 border-t border-slate-800 px-6 py-10 text-center text-xs text-slate-500">
      <p className="mx-auto max-w-xl">
        Delfi forecasts. You decide. Prediction markets carry real risk;
        trade only what you can afford to lose. Past performance does not
        guarantee future results.
      </p>
      <p className="mt-3">
        &copy; {new Date().getFullYear()} Delfi. All rights reserved.
      </p>
    </footer>
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
