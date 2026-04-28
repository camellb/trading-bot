export const metadata = { title: "Terms of Service - Delfi" };

export default function TermsPage() {
  return (
    <main className="content-main">
      <div className="content-eyebrow">Legal</div>
      <h1 className="content-h1">Terms of Service</h1>
      <p className="content-lede">
        These terms govern your use of Delfi. By installing the desktop app, activating a license key, or
        connecting a wallet, you agree to the terms below. Please read them carefully.
      </p>
      <div className="content-meta">Effective 2026-04-01 · Last updated 2026-04-21</div>

      <div className="content-body">
        <h2>1. Who we are</h2>
        <p>
          Delfi is an autonomous trading system that evaluates prediction markets and executes positions on
          Polymarket and similar venues on behalf of its users. Delfi is operated by the entity identified in
          our corporate filings. Throughout these terms we use <strong>Delfi</strong>, <strong>we</strong>,
          <strong> us</strong>, and <strong>our</strong> interchangeably to refer to that entity.
        </p>

        <h2>2. Eligibility</h2>
        <p>
          You must be at least 18 years old and legally eligible to trade prediction market contracts in your
          jurisdiction. You are responsible for ensuring that your use of Delfi complies with local law. Delfi is
          not available in jurisdictions where prediction market trading is prohibited.
        </p>

        <h2>3. License and credentials</h2>
        <p>
          You are responsible for maintaining the confidentiality of your license key and any private keys
          used to authorize trades. Delfi runs entirely on your computer; we never receive your wallet
          private key or seed phrase, and we will never ask for them. If you suspect a license key has been
          compromised, contact support so we can reissue it.
        </p>
        <ul>
          <li>Keep your license key and wallet keys private.</li>
          <li>You may not share or resell your license key.</li>
          <li>You must not use Delfi to evade sanctions or conduct illegal trading.</li>
        </ul>

        <h2>4. Simulation vs. live trading</h2>
        <p>
          Delfi offers two modes. In <strong>Simulation</strong>, Delfi evaluates markets and records paper
          positions without touching real capital. In <strong>Live</strong>, Delfi executes real trades using
          the wallet you connect. Simulation is not a guarantee of live performance. Market conditions,
          slippage, and venue behavior can cause live results to diverge from simulated results.
        </p>

        <div className="content-callout">
          <div className="callout-label">Important</div>
          <div>
            Prediction market trading involves real financial risk. You can lose all of the capital you deploy.
            Past performance does not guarantee future results. Delfi does not provide financial advice.
          </div>
        </div>

        <h2>5. Fees</h2>
        <p>
          Delfi is sold as a one-time purchase. The price at time of order is the price you pay; there is no
          recurring subscription and no performance fee. Forecasting API usage is billed by your model provider
          directly, and Polymarket trading fees are paid on-chain. We may change the one-time purchase price
          for new orders with notice on the homepage; existing orders are unaffected.
        </p>

        <h2>6. Autonomous execution</h2>
        <p>
          When live trading is enabled, Delfi will open and close positions without requiring your explicit
          approval of each trade. You may pause the agent or adjust risk parameters at any time. You are
          responsible for reviewing and configuring risk controls that reflect your tolerance.
        </p>

        <h2>7. No warranty</h2>
        <p>
          Delfi is provided on an as-is basis. We do not warrant that the service will be uninterrupted, that
          its probability estimates will be accurate, or that any particular trading strategy will be
          profitable. Venue outages, API failures, and unforeseen market events can produce adverse outcomes.
        </p>

        <h2>8. Limitation of liability</h2>
        <p>
          To the fullest extent permitted by law, Delfi is not liable for any trading losses, lost profits, or
          consequential damages arising from your use of the service. Our aggregate liability to you will not
          exceed the fees you paid us during the 12 months preceding the claim.
        </p>

        <h2>9. Termination</h2>
        <p>
          You may stop using Delfi at any time by uninstalling the desktop app from your computer. Open
          positions remain in your Polymarket wallet under your sole control. We may revoke a license key
          if you breach these terms or if we are required to do so by law. Refunds for the one-time purchase
          are available within 14 days of order, provided no live trade has been placed through the app.
        </p>

        <h2>10. Changes</h2>
        <p>
          We may update these terms to reflect changes to the service or to our legal obligations. Material
          changes will be communicated by email with at least 14 days notice. Continued use after the effective
          date of the change constitutes acceptance.
        </p>

        <h2>11. Contact</h2>
        <p>
          Questions about these terms can be sent to <a href="mailto:info@delfibot.com">info@delfibot.com</a>.
        </p>
      </div>
    </main>
  );
}
