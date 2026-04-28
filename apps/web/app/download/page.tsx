import Link from "next/link";
import "../styles/homepage.css";

// Download / purchase landing on the SaaS marketing app.
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
    "Buy Delfi once for $250, download for your platform, and run it locally. Lifetime updates included. macOS and Windows.",
};

const CHECKOUT_URL =
  process.env.NEXT_PUBLIC_CHECKOUT_URL ||
  "mailto:info@delfibot.com?subject=Delfi%20early%20access";

const PLATFORMS: {
  id: string;
  name: string;
  detail: string;
  arch: string;
  suffix: string;
}[] = [
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
    <main className="download-page">
      <DownloadNav />
      <section className="section download-hero">
        <div className="container narrow" style={{ textAlign: "center" }}>
          <h1 className="t-display-l balanced" style={{ marginBottom: 16 }}>
            Get Delfi for your machine
          </h1>
          <p className="newhere-body" style={{ maxWidth: 580, margin: "0 auto 32px" }}>
            One purchase. Every future release. Pick your platform below
            and your download starts after checkout.
          </p>
          <div
            style={{
              display: "inline-flex",
              alignItems: "baseline",
              gap: 12,
              padding: "12px 24px",
              border: "1px solid var(--gold)",
              borderRadius: 999,
              background: "var(--obsidian-raise)",
            }}
          >
            <span className="sec-eyebrow" style={{ margin: 0 }}>
              Delfi Desktop
            </span>
            <span
              className="t-num"
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 28,
                fontWeight: 500,
                color: "var(--vellum)",
              }}
            >
              $250
            </span>
            <span style={{ color: "var(--vellum-60)", fontSize: 13 }}>
              one-time
            </span>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="container">
          <p
            className="sec-eyebrow"
            style={{ textAlign: "center", marginBottom: 32 }}
          >
            Choose your platform
          </p>
          <div className="platforms-grid">
            {PLATFORMS.map((p) => (
              <a
                key={p.id}
                href={CHECKOUT_URL}
                className="platform-card"
              >
                <div className="platform-body">
                  <div className="platform-name">{p.name}</div>
                  <div className="platform-detail">{p.detail}</div>
                  <div className="platform-arch">
                    {p.arch} · {p.suffix}
                  </div>
                </div>
                <span className="platform-cta">Buy and download</span>
              </a>
            ))}
          </div>
          <p className="platforms-foot">
            The same license key works on both platforms. Install Delfi
            on the machines you own; run wherever your laptop is awake.
          </p>
        </div>
      </section>

      <section className="section">
        <div className="container narrow">
          <div className="custody-grid" style={{ gridTemplateColumns: "1fr" }}>
            <div>
              <h2 className="t-display-l balanced" style={{ marginBottom: 24 }}>
                What you get
              </h2>
              <ul className="custody-list">
                <li>
                  <span className="custody-tick">✓</span>
                  The Delfi desktop app for your platform, plus every
                  future release.
                </li>
                <li>
                  <span className="custody-tick">✓</span>
                  A license key by email immediately after checkout.
                  Activate the app once; it verifies online once a year
                  and runs fully offline in between.
                </li>
                <li>
                  <span className="custody-tick">✓</span>
                  Source-of-truth dashboard: every market scan, every
                  forecast, every fill, every settled outcome.
                </li>
                <li>
                  <span className="custody-tick">✓</span>
                  14-day refund window, provided you have not yet placed
                  a live trade through the app.
                </li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="container narrow">
          <h2 className="t-display-l balanced" style={{ marginBottom: 24 }}>
            What you bring
          </h2>
          <ul className="custody-list">
            <li>
              <span className="custody-tick">→</span>
              A funded Polymarket account and the private key for its
              wallet. Delfi reads the key from your OS keychain only on
              your machine.
            </li>
            <li>
              <span className="custody-tick">→</span>
              A model API key from any major provider. You pay your
              provider directly for forecast usage; Delfi caps how often
              it scans each market to keep that spend predictable.
            </li>
            <li>
              <span className="custody-tick">→</span>
              A reasonable starting bankroll. Delfi sizes every trade as
              a small fraction of bankroll; the math still works at $200,
              just at a slower learning rate.
            </li>
          </ul>
        </div>
      </section>

      <DownloadFooter />
    </main>
  );
}

function DownloadNav() {
  return (
    <nav className="top-nav scrolled past-hero">
      <div className="nav-inner">
        <div className="nav-left">
          <Link href="/" className="wordmark">
            <img src="/brand/mark.svg" alt="" className="wordmark-mark" />
            <span className="wordmark-text">DELFI</span>
          </Link>
          <ul className="nav-links">
            <li>
              <Link href="/#how">How It Works</Link>
            </li>
            <li>
              <Link href="/#faq">FAQ</Link>
            </li>
          </ul>
        </div>
        <div className="nav-right">
          <Link className="nav-login" href="/">
            ← Back to home
          </Link>
        </div>
      </div>
    </nav>
  );
}

function DownloadFooter() {
  return (
    <footer className="site-footer">
      <div className="container">
        <hr className="foot-divider" />
        <div className="foot-bottom">
          <span className="foot-copy">© 2026 Delfi · All rights reserved.</span>
          <span className="foot-risk">
            Prediction market trading involves real financial risk. Past
            performance does not guarantee future results.
          </span>
        </div>
      </div>
    </footer>
  );
}
