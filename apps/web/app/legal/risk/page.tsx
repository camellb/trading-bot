export const metadata = { title: "Risk Disclosure — Delfi" };

export default function RiskPage() {
  return (
    <main className="content-main">
      <div className="content-eyebrow">Legal</div>
      <h1 className="content-h1">Risk Disclosure</h1>
      <p className="content-lede">
        Prediction market trading involves real financial risk. Before you enable live trading, please read
        this disclosure carefully.
      </p>
      <div className="content-meta">Effective 2026-04-01 · Last updated 2026-04-21</div>

      <div className="content-body">
        <div className="content-callout">
          <div className="callout-label">Principal risk</div>
          <div>
            You can lose all of the capital you deploy. Delfi does not guarantee any particular outcome and
            past performance of any strategy does not guarantee future results.
          </div>
        </div>

        <h2>What you are doing when you trade prediction markets</h2>
        <p>
          Prediction market contracts are binary outcome contracts. Each contract resolves to 1 if the event
          occurs and 0 if it does not. Between creation and resolution, contracts trade at a price reflecting
          the market's consensus probability of the event. You make money when your trades are on the side
          that the market later agrees with, or when you buy into a market where your assessment of the true
          probability differs favorably from the traded price.
        </p>

        <h2>Sources of loss</h2>
        <ul>
          <li><strong>Resolution risk:</strong> contracts can resolve against you. In any single trade you can lose the full stake.</li>
          <li><strong>Estimation error:</strong> Delfi's probability estimates are not guaranteed to be accurate. A systematically miscalibrated model can produce a pattern of losses even when each trade looked positive-expected-value at the time of entry.</li>
          <li><strong>Execution risk:</strong> slippage, thin liquidity, and venue outages can cause fills at worse prices than expected or can prevent exits when you want them.</li>
          <li><strong>Correlation risk:</strong> open positions can be more correlated than they appear. A single news event can move many markets at once.</li>
          <li><strong>Regulatory risk:</strong> prediction market venues operate under evolving regulation. A venue may be shut down, delisted, or restricted in your jurisdiction with short notice.</li>
        </ul>

        <h2>Delfi does not provide financial advice</h2>
        <p>
          Delfi is a tool. The probability estimates, risk configuration suggestions, and weekly summaries
          shown in the dashboard are informational. They are not recommendations to buy, sell, or hold any
          contract. You are solely responsible for the trading and risk decisions taken on your account.
        </p>

        <h2>Suitability</h2>
        <p>Delfi is not suitable for you if any of the following apply:</p>
        <ul>
          <li>You cannot afford to lose the capital you deploy.</li>
          <li>You are borrowing to trade.</li>
          <li>You rely on trading returns to meet essential financial obligations.</li>
          <li>Prediction market trading is prohibited in your jurisdiction.</li>
        </ul>

        <h2>Risk controls</h2>
        <p>
          Delfi includes configurable risk controls including daily and weekly loss caps, drawdown halts,
          streak cooldowns, and a dry-powder reserve. These reduce but do not eliminate the risk of loss.
          Even with conservative settings, meaningful drawdowns can occur.
        </p>

        <h2>Simulation is not live</h2>
        <p>
          Performance observed in Simulation mode is not a reliable indicator of live performance.
          Differences between simulation and live trading can arise from slippage, latency, fill rates,
          venue-specific rules, and market impact. We encourage you to run Simulation for a meaningful period
          before enabling live trading.
        </p>

        <h2>Acknowledgement</h2>
        <p>
          By enabling live trading, you acknowledge that you have read this disclosure and that you
          understand the risks involved. If you have questions, contact{" "}
          <a href="mailto:support@delfi.app">support@delfi.app</a> before enabling live mode.
        </p>
      </div>
    </main>
  );
}
