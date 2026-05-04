export const metadata = { title: "Terms of Service - Delfi" };

export default function TermsPage() {
  return (
    <main className="content-main">
      <div className="content-eyebrow">Legal</div>
      <h1 className="content-h1">Terms of Service</h1>
      <p className="content-lede">
        These terms form a binding agreement between you and WLDK Limited. By installing the Delfi
        desktop application, activating a license key, or using the delfibot.com website, you accept these
        terms. If you do not accept them, do not use Delfi.
      </p>
      <div className="content-meta">Effective 2026-04-01 · Last updated 2026-05-04</div>

      <div className="content-body">
        <h2>1. Who we are</h2>
        <p>
          Delfi is an autonomous trading system that evaluates prediction markets and executes positions on
          Polymarket and similar venues on behalf of its users. The Delfi software and the delfibot.com
          website are operated by <strong>WLDK Limited</strong> (the &ldquo;Company&rdquo;, &ldquo;we&rdquo;,
          &ldquo;us&rdquo;, &ldquo;our&rdquo;). Throughout these terms <strong>Delfi</strong> refers to the
          software and the website operated by us under that brand.
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

        <h2>9. Termination and refunds</h2>
        <p>
          You may stop using Delfi at any time by uninstalling the desktop app from your computer. Open
          positions remain in your Polymarket wallet under your sole control. We may revoke a license key
          if you breach these terms or if we are required to do so by law.
        </p>
        <p>
          <strong>Refund policy.</strong> You may request a full refund of the one-time purchase price
          within fourteen (14) days of your order, provided you have not activated your license key on any
          machine. Activation occurs the moment a license key is accepted by the desktop application on any
          device. By activating the license you confirm that the digital good has been delivered and accept
          that the purchase is final and non-refundable from that point forward. To request a refund within
          the eligibility window, email <a href="mailto:info@delfibot.com">info@delfibot.com</a> from the
          address used at purchase.
        </p>
        <p>
          We may decline a refund request where we have reasonable grounds to believe the license has been
          activated, shared, or used. We may also refuse refund requests that we determine in good faith are
          fraudulent or abusive.
        </p>

        <h2>10. Indemnification</h2>
        <p>
          You agree to defend, indemnify, and hold harmless the Company, its officers, directors,
          shareholders, employees, agents, and affiliates from and against any and all claims, damages,
          obligations, losses, liabilities, costs, and expenses (including reasonable attorneys&rsquo; fees)
          arising out of or related to (a) your use of Delfi, (b) your violation of these terms, (c) your
          violation of any law or regulation, (d) your trading activity on any prediction market venue, or
          (e) any tax, withholding, or reporting obligation triggered by your use of Delfi.
        </p>

        <h2>11. Disclaimers</h2>
        <p>
          DELFI IS PROVIDED &ldquo;AS IS&rdquo; AND &ldquo;AS AVAILABLE&rdquo; WITHOUT WARRANTIES OF ANY
          KIND, EITHER EXPRESS OR IMPLIED, INCLUDING WITHOUT LIMITATION ANY IMPLIED WARRANTIES OF
          MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, NON-INFRINGEMENT, OR ARISING FROM A COURSE OF
          DEALING OR TRADE USAGE. We make no representation or warranty that Delfi will be uninterrupted,
          error-free, secure, or free of harmful components, or that any forecast, simulation, or trading
          strategy will be profitable. Some jurisdictions do not allow the exclusion of certain warranties;
          to the extent any such exclusion is unenforceable, the warranties in question are limited to the
          shortest period and the smallest scope permitted by applicable law.
        </p>

        <h2>12. Limitation of liability (cap)</h2>
        <p>
          TO THE MAXIMUM EXTENT PERMITTED BY LAW, IN NO EVENT WILL THE COMPANY OR ITS OFFICERS, DIRECTORS,
          SHAREHOLDERS, EMPLOYEES, AGENTS, OR AFFILIATES BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL,
          CONSEQUENTIAL, EXEMPLARY, OR PUNITIVE DAMAGES, INCLUDING WITHOUT LIMITATION DAMAGES FOR LOST
          PROFITS, LOST TRADING OPPORTUNITY, LOST GOODWILL, LOST DATA, LOST CRYPTOCURRENCY OR DIGITAL
          ASSETS, OR LOSS OF USE, ARISING OUT OF OR IN CONNECTION WITH YOUR USE OF DELFI, EVEN IF WE HAVE
          BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES. OUR AGGREGATE LIABILITY TO YOU FOR ALL CLAIMS
          ARISING FROM OR RELATED TO DELFI WILL NOT EXCEED THE LESSER OF (A) THE AMOUNT YOU ACTUALLY PAID
          US IN THE TWELVE (12) MONTHS PRECEDING THE EVENT GIVING RISE TO THE CLAIM, OR (B) ONE HUNDRED
          U.S. DOLLARS (US$100). The foregoing cap reflects an essential allocation of risk between you and
          us; without it, the Company would not provide Delfi at the price charged.
        </p>

        <h2>13. Governing law and dispute resolution</h2>
        <p>
          These terms are governed by, and construed in accordance with, the laws of the jurisdiction in
          which the Company is registered, without regard to its conflict-of-laws principles. The exclusive
          forum for any dispute arising out of or related to these terms is the competent courts of that
          jurisdiction, and you consent to the personal jurisdiction of those courts. The United Nations
          Convention on Contracts for the International Sale of Goods does not apply.
        </p>
        <p>
          <strong>Class-action and jury-trial waiver.</strong> You and the Company each agree that any
          dispute will be resolved on an individual basis only. Neither you nor we may bring or participate
          in a class action, collective action, or representative action. To the extent permitted by law,
          you and we waive any right to a jury trial.
        </p>
        <p>
          <strong>Time bar.</strong> Any cause of action arising out of or related to Delfi must be filed
          within one (1) year after the cause of action arose; otherwise it is permanently barred.
        </p>

        <h2>14. Compliance, sanctions, and tax</h2>
        <p>
          You represent and warrant that you are not located in, ordinarily resident in, or organised under
          the laws of a country or region subject to comprehensive economic sanctions administered by any
          competent authority including but not limited to the United Nations, the United States Office of
          Foreign Assets Control, the European Union, the United Kingdom, or the jurisdiction in which the
          Company is registered, and that you are not on any list of restricted persons published by such
          authorities. You are solely responsible for determining what taxes apply to your trading activity
          and for paying them. We do not provide tax advice and we will not produce tax forms on your
          behalf.
        </p>

        <h2>15. Severability and entire agreement</h2>
        <p>
          If any provision of these terms is held unenforceable, the remaining provisions remain in full
          force, and the unenforceable provision will be construed so as to give effect to the parties&rsquo;
          original intent to the maximum extent permitted by law. These terms, together with the Privacy
          Policy, Cookies Policy, and Risk Disclosure linked from delfibot.com, constitute the entire
          agreement between you and the Company with respect to Delfi and supersede any prior agreements.
        </p>

        <h2>16. Changes</h2>
        <p>
          We may update these terms to reflect changes to the service or to our legal obligations. Material
          changes will be communicated by email with at least 14 days notice. Continued use after the effective
          date of the change constitutes acceptance.
        </p>

        <h2>17. Contact</h2>
        <p>
          Questions about these terms can be sent to <a href="mailto:info@delfibot.com">info@delfibot.com</a>.
        </p>
      </div>
    </main>
  );
}
