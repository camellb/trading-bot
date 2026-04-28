import Link from "next/link";

// Download / purchase landing.
//
// Voice: clinical here, oracle voice stayed on /. The user has crossed
// the threshold from interest to intent; copy gives them what they need
// to act, not more atmosphere.
//
// CTA wiring: NEXT_PUBLIC_CHECKOUT_URL points at the eventual hosted
// checkout (Lemon Squeezy / Polar / Stripe). When unset we fall back to
// a contact mailto so early testers reach the maintainer directly.
// Either way, the on-page CTA shape stays identical so the page never
// has to be rewritten when the processor lands.

export const metadata = {
  title: "Download Delfi",
  description:
    "Buy Delfi once for $250, download for your platform, and run it locally. Lifetime updates included.",
};

const CHECKOUT_URL =
  process.env.NEXT_PUBLIC_CHECKOUT_URL ||
  "mailto:info@delfibot.com?subject=Delfi%20early%20access";

const PLATFORMS = [
  {
    id: "macos-arm64",
    name: "macOS",
    detail: "Apple Silicon (M1, M2, M3, M4)",
    arch: "arm64",
    suffix: ".dmg",
  },
  {
    id: "windows-x64",
    name: "Windows",
    detail: "Windows 10 and 11",
    arch: "x64",
    suffix: ".msi",
  },
];

export default function DownloadPage() {
  return (
    <main className="aurora min-h-screen">
      <Nav />
      <section className="mx-auto max-w-4xl px-6 pt-12 pb-20 text-center">
        <h1 className="text-4xl font-semibold text-white sm:text-5xl">
          Get Delfi for your machine
        </h1>
        <p className="mx-auto mt-5 max-w-2xl text-lg text-slate-300">
          One purchase. Every future release. Pick your platform below and
          your download starts after checkout.
        </p>

        <div className="mt-8 inline-flex items-baseline gap-2 rounded-full border border-[var(--brand-accent)]/40 bg-[var(--brand-panel)] px-6 py-3">
          <span className="text-sm uppercase tracking-[0.3em] text-[var(--brand-accent)]">
            Delfi Desktop
          </span>
          <span className="ml-3 text-3xl font-semibold text-white">$250</span>
          <span className="text-sm text-slate-400">one-time</span>
        </div>
      </section>

      <section className="mx-auto max-w-5xl px-6 pb-20">
        <h2 className="mb-6 text-center text-sm uppercase tracking-[0.3em] text-slate-400">
          Choose your platform
        </h2>
        <div className="grid gap-4 sm:grid-cols-2">
          {PLATFORMS.map((p) => (
            <a
              key={p.id}
              href={CHECKOUT_URL}
              className="group flex items-center justify-between rounded-xl border border-slate-800 bg-[var(--brand-panel)] p-6 transition hover:border-[var(--brand-accent)]"
            >
              <div className="text-left">
                <p className="text-lg font-semibold text-white">{p.name}</p>
                <p className="text-sm text-slate-400">{p.detail}</p>
                <p className="mt-1 text-xs uppercase tracking-wider text-slate-500">
                  {p.arch} &middot; {p.suffix}
                </p>
              </div>
              <span className="rounded-md bg-[var(--brand-accent)] px-4 py-2 text-sm font-medium text-slate-900 transition group-hover:bg-[var(--brand-accent-strong)]">
                Buy and download
              </span>
            </a>
          ))}
        </div>
        <p className="mx-auto mt-8 max-w-2xl text-center text-xs text-slate-500">
          The same license key works on every platform. Install Delfi on the
          machines you own; run it on whichever one is awake.
        </p>
      </section>

      <section className="mx-auto max-w-3xl px-6 pb-20">
        <div className="rounded-2xl border border-slate-800 bg-[var(--brand-panel)] p-8">
          <h2 className="mb-4 text-2xl font-semibold text-white">
            What you get
          </h2>
          <ul className="space-y-3 text-sm text-slate-300">
            <Item>
              The Delfi desktop app for the platform you bought, plus every
              future release.
            </Item>
            <Item>
              A license key delivered by email immediately after checkout.
              Activate the app once; it verifies online once a year and runs
              fully offline in between.
            </Item>
            <Item>
              Source-of-truth dashboard: every market scan, every forecast,
              every fill, every settled outcome.
            </Item>
            <Item>
              14-day refund window if you have not yet placed a live trade
              through the app.
            </Item>
          </ul>
        </div>
      </section>

      <section className="mx-auto max-w-3xl px-6 pb-24">
        <h2 className="mb-4 text-2xl font-semibold text-white">
          What you bring
        </h2>
        <ul className="space-y-3 text-sm text-slate-300">
          <Item>
            A funded Polymarket account and the private key for its wallet.
            Delfi reads the key from your OS keychain only on your machine.
          </Item>
          <Item>
            A model API key from any major provider. You pay your
            provider directly for forecast usage; Delfi caps how often
            it scans each market to keep that spend predictable.
          </Item>
          <Item>
            A reasonable starting bankroll. Delfi sizes every trade as a
            small fraction of bankroll, so the smaller the deposit the
            smaller every position; the math still works at $200, just at a
            slower learning rate.
          </Item>
        </ul>
      </section>

      <Footer />
    </main>
  );
}

function Item({ children }: { children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
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
      <span>{children}</span>
    </li>
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
        <Link href="/#how" className="hover:text-white">
          How it works
        </Link>
        <Link href="/#faq" className="hover:text-white">
          FAQ
        </Link>
        <Link
          href="/"
          className="rounded-md border border-slate-700 px-4 py-2 font-medium text-slate-200 hover:border-slate-500"
        >
          Back to home
        </Link>
      </div>
    </nav>
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
