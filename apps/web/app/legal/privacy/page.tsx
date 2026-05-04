export const metadata = { title: "Privacy Policy - Delfi" };

export default function PrivacyPage() {
  return (
    <main className="content-main">
      <div className="content-eyebrow">Legal</div>
      <h1 className="content-h1">Privacy Policy</h1>
      <p className="content-lede">
        WLDK Limited (&ldquo;we&rdquo;, &ldquo;us&rdquo;) operates Delfi. This policy explains what we
        collect, why we collect it, and what choices you have. Our defining design constraint is that
        Delfi runs entirely on your computer, so most of what would normally be collected by a SaaS
        product never reaches us.
      </p>
      <div className="content-meta">Effective 2026-04-01 · Last updated 2026-05-04</div>

      <div className="content-body">
        <h2>1. Information we collect</h2>
        <p>Delfi runs entirely on your computer. The only data we collect is what is needed to deliver the
          purchase and answer your support questions:</p>
        <ul>
          <li>
            <strong>Purchase information:</strong> your email address, the name on the order, the license key
            issued to you, and the data your payment processor returns to us (last four digits, billing
            country, transaction reference).
          </li>
          <li>
            <strong>Support correspondence:</strong> any messages you send to{" "}
            <a href="mailto:info@delfibot.com">info@delfibot.com</a> and our replies.
          </li>
          <li>
            <strong>Marketing site analytics:</strong> standard request logs (IP, user agent) and aggregate
            performance metrics for visits to delfibot.com. We do not track you across other sites.
          </li>
        </ul>
        <p>
          We do <strong>not</strong> collect your trading data, your wallet address, your wallet&apos;s
          private key, your forecasts, or your P&amp;L. Those live on your computer and never leave it.
        </p>

        <h2>2. How we use your data</h2>
        <p>
          We use your data to operate the service, execute trades you have authorized, compute performance
          metrics, send you the email notifications you opt into, and diagnose problems. We do not sell your
          personal data and we do not use it for advertising.
        </p>

        <h2>3. Wallets and keys</h2>
        <p>
          The desktop app stores your Polymarket wallet&apos;s private key in your operating system&apos;s
          keychain (Apple Keychain on macOS, Windows Credential Locker on Windows). Delfi reads it only
          inside its own process, only when it needs to sign a trade. The key never leaves your computer and
          is never transmitted to us. We have no record of your wallet address, your funds, or any trade you
          place.
        </p>

        <h2>4. Cookies and local storage</h2>
        <p>
          We use first-party cookies for authentication and local storage for your dashboard preferences. We
          do not use third-party tracking cookies. Disabling cookies will prevent you from logging in.
        </p>

        <h2>5. Third-party processors</h2>
        <p>
          We use a small number of third-party processors to run the service. These include payment
          processors, email delivery, error tracking, and cloud infrastructure. Each processor is bound by a
          data processing agreement that mirrors the protections in this policy.
        </p>

        <h2>6. Data retention</h2>
        <p>
          We retain your purchase record (email, license key, transaction reference) for as long as the
          license is active and for up to seven years after issuance to meet financial record-keeping
          obligations. Support correspondence is retained while it remains relevant to an open question and
          deleted on a rolling basis after that. Your trading records are not retained by us at all because
          we never receive them.
        </p>

        <h2>7. Your rights</h2>
        <p>Depending on your jurisdiction you may have the right to:</p>
        <ul>
          <li>Request a copy of the personal data we hold about you</li>
          <li>Request correction of inaccurate data</li>
          <li>Request deletion, subject to legal record-keeping obligations</li>
          <li>Object to certain processing</li>
        </ul>
        <p>
          Send requests to <a href="mailto:info@delfibot.com">info@delfibot.com</a>. We respond within 30 days.
        </p>

        <h2>8. Security</h2>
        <p>
          Data we hold (purchase records, support correspondence) is transmitted over TLS and encrypted at
          rest with provider-managed encryption. Your wallet&apos;s private key is stored in your operating
          system&apos;s keychain, protected by your OS account credentials and the keychain&apos;s native
          encryption. Delfi never transmits the private key over the network.
        </p>

        <h2>9. Changes</h2>
        <p>
          Material changes to this policy will be communicated by email with at least 14 days notice. Minor
          clarifications are updated in place with a revised date at the top of this page.
        </p>

        <h2>10. Contact</h2>
        <p>
          Questions can be sent to <a href="mailto:info@delfibot.com">info@delfibot.com</a>.
        </p>
      </div>
    </main>
  );
}
