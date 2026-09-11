export const metadata = { title: "Privacy Policy - Delfi" };

export default function PrivacyPage() {
  return (
    <main className="content-main">
      <div className="content-eyebrow">Legal</div>
      <h1 className="content-h1">Privacy Policy</h1>
      <p className="content-lede">
        WLDK Limited (&ldquo;we&rdquo;, &ldquo;us&rdquo;) operates Delfi and delfibot.com. This policy
        explains what personal data we collect, why we use it, and the choices available to you.
        Delfi&apos;s trading and forecasting functions run locally on your computer.
      </p>
      <div className="content-meta">Effective 2026-04-01 · Last updated 2026-09-11</div>

      <div className="content-body">
        <h2>1. Information we collect</h2>
        <ul>
          <li>
            <strong>Website and campaign data:</strong> IP address, approximate country, browser and device
            information, pages viewed, actions taken, referring site, campaign parameters, and advertising
            click identifiers such as <code>fbclid</code> when they are present.
          </li>
          <li>
            <strong>Purchase information:</strong> your email address, name, billing country, order amount,
            currency, transaction references, license key, and limited payment details made available to us by
            Stripe. We do not receive your full card number.
          </li>
          <li>
            <strong>License activation information:</strong> license identifier, a one-way hash of the device
            identifier, an optional device label, activation time, and last-seen time. The raw device identifier
            does not leave your computer.
          </li>
          <li>
            <strong>Support correspondence:</strong> messages you send to{" "}
            <a href="mailto:info@delfibot.com">info@delfibot.com</a> and our replies.
          </li>
        </ul>
        <p>
          We do <strong>not</strong> receive your wallet&apos;s private key, wallet address, trading history,
          forecasts, positions, or P&amp;L. Those remain on your computer or within services you connect to
          directly, such as Polymarket and your selected forecasting provider.
        </p>

        <h2>2. How we use information</h2>
        <ul>
          <li>Process purchases, issue licenses, deliver receipts, and provide support.</li>
          <li>Activate one licensed device at a time and enforce refunds, revocations, and fraud controls.</li>
          <li>Measure website performance, checkout completion, and advertising results.</li>
          <li>Maintain accounting, tax, security, and legal records.</li>
        </ul>
        <p>
          We process purchase and license data to perform our contract with you. We process accounting,
          security, and fraud data to meet legal obligations and our legitimate interests. We use analytics and
          advertising technologies with consent where applicable, or under another lawful basis available in
          your jurisdiction. We do not sell your personal data.
        </p>

        <h2>3. Wallets and private keys</h2>
        <p>
          The desktop app stores your Polymarket private key in a local file restricted to your operating-system
          user account. Delfi reads the key inside the desktop process when it needs to sign a trade. The key is
          not transmitted to us, and we cannot access your wallet or funds.
        </p>

        <h2>4. Analytics and advertising measurement</h2>
        <p>
          We use Google Analytics 4, Meta Pixel, Microsoft Clarity, and Vercel Speed Insights to understand use
          of the website and measure the path from an advertisement to a purchase. Where prior consent is
          required, these tools load only after you accept analytics and advertising cookies. Elsewhere they may
          load by default, but you can disable them at any time on our Cookies Policy page.
        </p>
        <p>
          When Meta measurement is allowed, a completed purchase may also be sent to Meta through its
          Conversions API. The event can include the order value and currency, campaign identifiers, browser
          identifiers, IP address, user agent, and a cryptographic hash of the purchase email. We use the same
          order identifier for browser and server events so Meta can count the purchase once.
        </p>

        <h2>5. Cookies and local storage</h2>
        <p>
          The marketing site uses browser storage to remember your consent choice and retain campaign
          attribution within the current browser tab until checkout. Analytics and advertising providers may set
          cookies when their tools are enabled. Stripe may use cookies and similar technologies in the embedded
          checkout for payment processing, security, and fraud prevention. Details and controls are in our{" "}
          <a href="/legal/cookies">Cookies Policy</a>.
        </p>

        <h2>6. Service providers</h2>
        <p>We share only the information needed for each provider to perform its role:</p>
        <ul>
          <li><strong>Stripe:</strong> payment processing, checkout, and transaction records.</li>
          <li><strong>Supabase:</strong> hosted database for purchase, license, and activation records.</li>
          <li><strong>Resend:</strong> delivery of license and transactional emails.</li>
          <li><strong>Zoho Books:</strong> invoices, refunds, and accounting records.</li>
          <li><strong>Vercel:</strong> website hosting, request logs, and performance measurement.</li>
          <li><strong>Google, Meta, and Microsoft:</strong> analytics, advertising measurement, and site-use insights when enabled.</li>
        </ul>
        <p>
          These providers process information under their own terms and privacy notices as well as their
          agreements with us.
        </p>

        <h2>7. Data retention</h2>
        <p>
          We retain purchase, invoice, license, and transaction records while the license is active and for up to
          seven years where needed for financial and legal obligations. Device activation data is retained while
          needed to operate the license. Support messages are retained while relevant to the request and our
          legal obligations. Website attribution kept in session storage is removed when the browser tab session
          ends. Analytics providers retain data according to the settings in our provider accounts.
        </p>

        <h2>8. International transfers</h2>
        <p>
          Some providers process information outside the United Kingdom or your country. Where required, we rely
          on recognised transfer safeguards, including adequacy regulations and approved contractual clauses.
        </p>

        <h2>9. Your rights</h2>
        <p>Depending on your jurisdiction, you may have the right to:</p>
        <ul>
          <li>Request access to or a copy of your personal data.</li>
          <li>Request correction or deletion of your data.</li>
          <li>Restrict or object to certain processing.</li>
          <li>Request data portability.</li>
          <li>Withdraw consent at any time without affecting earlier lawful processing.</li>
          <li>Complain to your local data protection authority.</li>
        </ul>
        <p>
          Send requests to <a href="mailto:info@delfibot.com">info@delfibot.com</a>. We normally respond within
          one month.
        </p>

        <h2>10. Security</h2>
        <p>
          Website and purchase information is transmitted over TLS. Our hosting, payment, email, accounting, and
          database providers apply their own access controls and encryption. On your computer, Delfi&apos;s local
          secrets file is restricted to your operating-system user account. Anyone who can access that account or
          device may be able to access the file, so you must keep the device and account secure.
        </p>

        <h2>11. Changes</h2>
        <p>
          We may update this policy when our product, providers, or legal obligations change. Material changes
          will be communicated where required by law. The latest revision date appears at the top of this page.
        </p>

        <h2>12. Data controller and contact</h2>
        <p>
          The data controller is <strong>WLDK Limited</strong>, company number 16182403, with its registered
          office at Lytchett House, 13 Freeland Park, Wareham Road, Poole, Dorset, BH16 6FA, United Kingdom.
        </p>
        <p>
          Privacy questions can be sent to{" "}
          <a href="mailto:info@delfibot.com">info@delfibot.com</a>. UK users may complain to the Information
          Commissioner&apos;s Office. EU users may complain to their local supervisory authority.
        </p>
      </div>
    </main>
  );
}
