export const metadata = { title: "Privacy Policy - Delfi" };

export default function PrivacyPage() {
  return (
    <main className="content-main">
      <div className="content-eyebrow">Legal</div>
      <h1 className="content-h1">Privacy Policy</h1>
      <p className="content-lede">
        We take privacy seriously because you are trusting us with sensitive financial data. This policy
        explains what we collect, why we collect it, and what choices you have.
      </p>
      <div className="content-meta">Effective 2026-04-01 · Last updated 2026-04-21</div>

      <div className="content-body">
        <h2>1. Information we collect</h2>
        <p>We collect three categories of information:</p>
        <ul>
          <li>
            <strong>Account information:</strong> your email address, hashed password, and optional profile
            name.
          </li>
          <li>
            <strong>Trading data:</strong> positions opened and closed, probability estimates, realized and
            unrealized profit and loss, and risk configuration values.
          </li>
          <li>
            <strong>Technical data:</strong> IP address, browser user agent, device fingerprint, and
            authentication events. We use this to detect fraud and improve reliability.
          </li>
        </ul>

        <h2>2. How we use your data</h2>
        <p>
          We use your data to operate the service, execute trades you have authorized, compute performance
          metrics, send you the email notifications you opt into, and diagnose problems. We do not sell your
          personal data and we do not use it for advertising.
        </p>

        <h2>3. Wallets and keys</h2>
        <p>
          Delfi never stores the private keys that control your funds. When you authorize live trading, you
          grant a scoped trading delegation using standard smart-wallet tooling. You can revoke that
          delegation at any time from your wallet. We store only the public address and transaction hashes
          needed to reconcile your account.
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
          We retain your account and trading records for as long as your account is active, and for up to
          seven years after closure to meet financial record-keeping obligations. You can request an export of
          your data at any time.
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
          We encrypt data in transit with TLS and at rest with provider-managed encryption. Passwords are
          hashed with Argon2. Two-factor authentication is available on every account and required for live
          trading.
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
