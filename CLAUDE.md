# Delfi — Project Doctrine

## What Delfi is

Delfi is an autonomous prediction market trader. It watches Polymarket, forecasts which side of every tradeable market will resolve true, and backs that forecast with a flat, confidence-scaled stake. It manages those positions dynamically and learns from every resolution. Over time, the bankroll grows.

The product is both the trader and the experience of watching it trade. Users connect their Polymarket account. Delfi goes to work. They watch its reasoning on a live dashboard, see its positions in real time, and witness the calibrated intelligence of a system that treats every market as a solvable problem.

## The goal

Make money. Specifically: maximize ROI on bankroll across all trades.

This is the only metric that matters. Win rate, calibration, Brier score — these are diagnostics. They help us understand whether we're making money and why. They are not themselves the point.

If a proposed change improves expected ROI, ship it. If it doesn't, don't.

## The core principle — simple on purpose

**Forecast the outcome. Back the forecast.**

For every market Delfi evaluates, the ensemble produces a single probability that YES will resolve true. The rule is:

- If that probability is above 0.55 → buy YES.
- If it is below 0.45 → buy NO.
- Otherwise skip — the call is a coin flip and there is nothing to bet on.

That is the entire side-selection logic. There is no expected-value calculation, no "is this side cheap relative to my probability" test, no comparison of the bot's number against the market price. The side Delfi bets is the side Delfi thinks will win, full stop. Price does not enter the decision.

This is a deliberate reversal from the prior version. The prior version filtered for trades where the bot's probability disagreed with the market — it only bet when it thought one side was "mispriced." That rule structurally selected cases where the bot was wrong, and it lost money on roughly nine out of ten resolved trades. The fix is to stop filtering for disagreement and just back the model's pick.

## Sizing

Flat, small stakes, scaled by model confidence.

- Confidence ≥ 0.80 → 3% of bankroll.
- Confidence 0.50–0.80 → 2% of bankroll.
- Confidence < 0.50 → 1% of bankroll.
- Hard ceiling: 5% of bankroll per trade, regardless.
- Streak cooldown halves the stake after consecutive losses.

Sizing scales by model confidence only. Not by disagreement size, not by price, not by anything else. Simple sizing keeps variance per trade low so the portfolio learns fast about whether the model is any good.

## Skip conditions

Delfi skips a market when any of these hold. These are safety gates, not strategy filters.

- **Forecast is a coin flip.** Probability between 0.45 and 0.55.
- **Paused or risk-halted.** User has paused trading, or a risk brake is tripped.
- **Already positioned.** We already hold a position on this market.
- **Recently evaluated.** We already scored this market in the last few days (cost control).
- **Low volume.** Below the user's configured 24-hour volume floor — thin markets are noisy and illiquid.
- **Duplicate in event.** Too many positions already open on correlated outcomes within the same event.

There is no skip for "market disagrees with us." Disagreement with the market is not a signal either way under this doctrine.

## The forecaster is the product

Because side selection is entirely driven by the model's forecast, every dollar of ROI depends on the forecaster being right more often than it is wrong on its own picks. That means the engineering work worth doing is work that makes the forecaster better: richer research, stronger prompts, ensemble construction, calibration analysis by category, learning from resolved markets. The sizer stays narrow and dumb on purpose.

If the forecaster is miscalibrated in a specific category, the fix is to improve the forecasting in that category, or to stop trading that category, not to bolt mechanical filters onto the sizer to try to compensate.

## What we do with losing categories

When resolution data shows the model has been consistently wrong in a specific category, that category goes on the skip list. Short-horizon sports (tennis, qualifiers, low-tier matches) are efficient against a language model and have cost us money — they are on the default skip list until category-level evidence says otherwise.

The learning cadence is trade-volume-based. Every 50 settled trades, Delfi analyzes its record by category and proposes skip-list changes, prompt improvements, or research adjustments. The user reviews and applies them.

## Other strategies

The base strategy — back the forecast — is one source of ROI. Others may be added:

- **Arbitrage.** When mutually exclusive outcomes on related markets sum to less than 1.00 after costs, lock in the spread. Structural inefficiency, no forecasting required.
- **Longshot-NO systematic.** Well-documented favorite-longshot bias across betting markets. Selling NO on extreme longshots is a candidate strategy, gated behind its own evidence.
- **Others as discovered.**

Each strategy is measured on its own ROI. Profitable strategies get more allocation. Unprofitable ones are cut.

## Risk management

Circuit breakers protect the bankroll from catastrophic loss. They run identically in shadow (simulation) and live, so shadow actually simulates live.

All risk parameters are per-user editable in the dashboard within system bounds. Defaults:

- Daily loss limit: 10% of bankroll (bounded 5-25%)
- Weekly loss limit: 20% of bankroll (bounded 10-40%)
- Drawdown halt: 40% from peak triggers manual review (bounded 20-60%)
- Streak cooldown: 3 consecutive losses halves position sizes for 5 trades (bounded 2-10)
- Dry powder reserve: 20% of bankroll never deployed (bounded 10-40%)
- Maximum stake per trade: 5% of bankroll (bounded 1-10%)
- Baseline stake: 2% of bankroll (bounded 0.5-5%)

Bounds exist so users cannot configure obviously catastrophic settings. Within the bounds the user is in control.

## Learning and iteration

Delfi learns continuously and suggests config changes autonomously, but it does not apply them autonomously. Every config change is a deliberate user decision, presented with evidence, acknowledged explicitly.

Learning is trade-volume-based, not calendar-based. Every 50 settled trades Delfi runs a full analysis pass: ROI and calibration by category, which categories beat the market and which didn't, proposed skip-list or prompt changes with backtest evidence. The user decides. Nothing changes runtime behavior without approval.

## The product experience

Delfi is a character as much as a system. Its dashboard feels alive because it is doing real work in real time, not because of decorative animations. Its reasoning is visible because transparency is both ethical and retention-positive. Its losses are narrated honestly because hiding them destroys trust.

ROI is the most prominent metric on every surface. P&L is shown in dollars. Win rate is secondary. Brier is a diagnostic visible to users who care but not the scoreboard.

The Delfi persona — the oracle, the prophecy metaphor, the ethereal visual language — is marketing. It belongs in hero copy and brand assets. Product surfaces use clinical precision: "Estimated probability 0.62. Resolved NO. P&L -$4.12."

## What we optimize for

1. ROI across the portfolio of strategies.
2. ROI per strategy and per category (to decide allocation).
3. Calibration quality per category (as a diagnostic of forecaster health).
4. Sample size per category (to distinguish signal from variance).

Not: win rate alone, Brier alone, trade count, or framework elegance.

## How we decide

For every proposed change:

1. Does this improve expected ROI?
2. Is there evidence — from backtest, from historical data, from live performance — supporting the improvement?
3. What is the smallest version of this change that tests the hypothesis?

If the answers are yes, yes, and clear, ship the small version. Measure it. If it works, expand. If it doesn't, revert.

## Settled lessons that must not be re-litigated

- Betting the "cheaper" side relative to the model's probability — filtering for disagreement with the market — is a losing strategy regardless of what that rule is called. Delfi backs the model's pick directly.
- Kelly sizing amplifies estimator errors. With a noisy forecaster it produces win-small-lose-big patterns. Flat, confidence-scaled sizing until calibration is proven per category.
- Autonomous config changes on small samples drift in harmful directions. All config changes require user approval, even when Delfi proposes them.
- Shadow mode with disabled risk brakes does not simulate live. Shadow and live run identical risk parameters.
- Brier score is not profit. A well-calibrated bot can still lose money. Brier is a diagnostic, not a performance target.
- Short-horizon sports have cost us money and stay on the default skip list until category evidence says otherwise.

These are settled. Future sessions do not reopen them.

## Closing

Delfi exists to make money for its users. It does that by forecasting outcomes as accurately as possible and backing those forecasts with small, confidence-scaled stakes. Everything else is either a safety gate, a risk brake, or an optimization of the forecaster itself.

Forecast the outcome. Back the forecast. Measure the result. Improve.
