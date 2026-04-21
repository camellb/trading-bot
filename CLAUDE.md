# Delfi — Project Doctrine

## What Delfi is

Delfi is an autonomous prediction market trader. It watches Polymarket continuously, evaluates the probability of every tradeable market using an ensemble of language models, and takes positions wherever the expected value is positive after costs. It manages those positions dynamically — entering, adding, taking profit, cutting losses — and learns from every resolution. Over time, the bankroll grows.

The product is both the trader and the experience of watching it trade. Users connect their Polymarket account. Delfi goes to work. They watch its reasoning on a live dashboard, see its positions in real time, and witness the calibrated intelligence of a system that treats every market as a solvable problem.

## The goal

Make money. Specifically: maximize ROI on bankroll across all trades.

This is the only metric that matters. Every design decision, every code change, every feature exists to serve this goal. Win rate, calibration, Brier score, framework elegance — these are diagnostics. They help us understand whether we're making money and why. They are not themselves the point.

If a proposed change improves expected ROI, ship it. If it doesn't, don't. That's the decision rule.

## The vision

Delfi is not a wrapper around an LLM. It is a trading intelligence that happens to use LLMs as its primary forecaster. The distinction matters.

A wrapper asks Claude "what's the probability of this market?" and bets accordingly. That is what the first version of this project did. It lost money.

Delfi is more ambitious. It is a synthesis — language model forecasting, market microstructure awareness, empirical base rates from its own trading history, dynamic position management, and a portfolio of distinct trading strategies running in parallel. The LLM is the brain that estimates probabilities. Everything else is how those estimates are turned into consistent profit.

The long-term vision is a system that:

- **Forecasts with genuine calibration.** Its probability estimates are trustworthy across archetypes, time horizons, and market conditions. When it says 70%, it happens 70% of the time.
- **Trades multiple strategies simultaneously.** Expected-value betting on LLM forecasts. Longshot-NO plays exploiting favorite-longshot bias. Cross-market arbitrage when related contracts are mispriced against each other. Microstructure mean-reversion after sharp moves without news. Each strategy is a distinct source of ROI, uncorrelated with the others.
- **Manages positions as a living portfolio.** It doesn't just enter and hold. It takes profit when the thesis is realized. It cuts losses when the thesis is invalidated. It scales in when edge widens. It thinks about correlation across open positions and sizes accordingly.
- **Learns continuously.** Every resolved market adds to the empirical base rate library. Every losing trade is diagnosed against a feature vector to find patterns it has been blind to. Its calibration improves with data.
- **Is genuinely transparent.** Every trade shows the reasoning. Every loss has an honest post-mortem. Users see the full decision logic, not a black box, because transparency is both the ethical stance and the retention mechanism.

This is the product. Delfi is a trader that gets smarter over time, runs multiple strategies, manages its book dynamically, and shows its work.

## The core principle

**Every trade has positive expected value after costs.**

For any market, Delfi estimates probabilities and considers both sides. It computes expected value for each side at its current ask price. If any side clears a minimum EV threshold after realistic costs, it takes that side. If neither side does, it skips.

EV_side = p_win × (1 / ask_price) − 1 − cost_assumption

Whether the estimate agrees with the market or disagrees with it is irrelevant. What matters is the payoff math. This is the framework bookmakers use, the framework professional bettors use, the framework that extracts value regardless of whether the edge comes from agreeing with informed flow or correcting uninformed flow.

This principle is the foundation. Strategies layered on top may exploit additional sources of edge — arbitrage, longshot bias, microstructure — but all of them reduce to the same underlying criterion: take the trade if the expected payoff exceeds the cost.

## Claude as the brain

The ensemble of language models is the primary intelligence. Claude estimates probability and confidence. The system's job is to translate those estimates into positive-EV positions as faithfully as possible. It is not to second-guess the model with layers of mechanical filters.

If the forecaster is wrong in systematic ways, the fix is to improve the forecasting — better prompts, multi-stage reasoning, richer research, stronger ensembles. The fix is not to bolt heuristics on top that try to compensate for bad forecasts. Heuristic compensation creates compounding distortions.

Trust the model. Improve the model when it needs improving. Keep the sizer narrow.

## Sizing philosophy

Flat or near-flat sizing. Scale by confidence, not by any other factor. Hard cap every trade at a small percentage of bankroll.

Starting defaults:
- 2% of bankroll per trade at baseline
- 1% when confidence is below 0.5
- 3% when confidence is above 0.8
- 5% hard ceiling per trade regardless

Simple sizing produces lower variance per trade, which produces faster learning about what actually works. Aggressive compounding can come later if and when there is a proven strategy to compound. Until then, the priority is signal, not size.

## Strategy pluralism

Delfi is not one strategy. It is a portfolio of approaches, each evaluated on its own ROI contribution.

The base strategy is positive-EV betting on ensemble forecasts. Others may be added:

- **Longshot-NO systematic.** Bet NO on markets trading at extreme lows (3-8%) with decent liquidity, exploiting favorite-longshot bias documented across decades of betting market research.
- **Cross-market arbitrage.** Detect mutually exclusive outcome sets that sum to less than 1.00 and lock in guaranteed returns by buying all sides at the discount.
- **Microstructure reversion.** Bet on partial reversion after sharp price moves that occur without corresponding news.
- **Others as discovered.** The system is open to any strategy that demonstrably adds ROI.

Each strategy is measured independently. Profitable strategies get more allocation. Unprofitable strategies are cut. The portfolio is whatever mix of approaches historically makes money across a meaningful sample.

## Risk management

Circuit breakers protect the bankroll from catastrophic loss. They run identically in shadow and live modes so shadow actually simulates live.

All risk parameters below are **per-user editable** in the dashboard settings. Each user configures their own risk tolerance within system-defined bounds. Defaults shown are sensible starting values for a new account.

- Daily loss limit: 10% of bankroll (user-editable, bounded 5-25%)
- Weekly loss limit: 20% of bankroll (user-editable, bounded 10-40%)
- Drawdown halt: 40% from peak triggers manual review (user-editable, bounded 20-60%)
- Streak cooldown: 3 consecutive losses halves position sizes for 5 trades (user-editable, bounded 2-10 losses)
- Dry powder reserve: 20% of bankroll never deployed (user-editable, bounded 10-40%)
- Maximum stake per trade: 5% of bankroll (user-editable, bounded 1-10%)
- Baseline stake: 2% of bankroll (user-editable, bounded 0.5-5%)
- Minimum EV threshold: 3% after costs (user-editable, bounded 1-10%)

Bounds exist so users cannot configure obviously catastrophic settings. Within the bounds, the user is in control. The UI explains what each parameter means and shows expected impact on trade frequency and drawdown risk.

These protections apply without exception once configured. They exist so a bad run cannot end a user's participation in the product.

## Learning and iteration

Delfi learns continuously and suggests config changes autonomously — but it does not apply them autonomously. Every config change is a deliberate user decision, presented with evidence, acknowledged explicitly.

The learning cadence is **trade-volume-based, not calendar-based**. Every 50 settled trades, Delfi runs a full analysis pass:

- Computes ROI, win rate, calibration, and P&L attribution by strategy and archetype
- Identifies patterns in wins and losses
- Proposes specific config adjustments when the data supports them (minimum sample size gates still apply — no suggestions on bucket-level patterns below n=20)
- Runs proposed changes through the backtester against historical predictions
- Surfaces suggestions with backtest evidence attached

Suggestions are presented to the user in the dashboard with:
- What change is proposed
- What data supports it
- Backtester delta (hypothetical ROI improvement)
- Apply / Skip / Snooze buttons

The user decides. Nothing changes runtime behavior without approval.

Learning cadence rationale: calendar-based tuning (weekly, monthly) suggests changes on whatever sample has accumulated in that window, which is often too small and noisy. Trade-volume gating ensures every suggestion is backed by a meaningful sample. An active bot might hit 50 trades in days; a quiet one might take weeks. Either way, suggestions arrive when the data justifies them.

The empirical base rate library — the accumulated resolution data from every market Delfi has ever evaluated — is a durable asset. It grows with every trade and every skipped-but-evaluated market. It is the foundation of long-term calibration edge over competitors who start from scratch.

## The product experience

Delfi is a character as much as a system. Its dashboard feels alive because it is doing real work in real time, not because of decorative animations. Its reasoning is visible because transparency is both ethical and retention-positive. Its losses are narrated honestly because hiding them destroys trust and users will find out anyway.

The UI reflects reality. Animations are bound to real pipeline events, not to scripted theater. Win celebrations are proportional to the win. Loss post-mortems are precise about what happened and honest about whether there's a lesson.

ROI is the most prominent metric on every surface. P&L is shown in dollars. Win rate is secondary. Brier is a diagnostic visible to users who care but not presented as the scoreboard.

The Delfi persona — the oracle, the prophecy metaphor, the ethereal visual language — is marketing. It belongs in hero copy and brand assets. Product surfaces use clinical precision: "Estimated probability 0.62. Resolved NO. P&L −$4.12."

## What we optimize for

1. ROI across the portfolio of strategies
2. ROI per strategy (to decide allocation)
3. Calibration quality (as a diagnostic of forecaster health)
4. Sample size per strategy (to distinguish signal from variance)

Not: win rate alone, Brier alone, framework fidelity, engineering elegance, or feature count.

## How we decide

For every proposed change:

1. Does this improve expected ROI?
2. Is there evidence — from backtest, from historical data, from live performance — supporting the improvement?
3. What is the smallest possible version of this change that tests the hypothesis?

If the answers are yes, yes, and clear — ship the small version. Measure it. If it works, expand. If it doesn't, revert.

Elegance, theoretical optimality, and framework consistency are not reasons to ship changes. Evidence of ROI improvement is.

## What we have learned so far

This project has accumulated several hard-won lessons. They should not be re-litigated. They should inform every future decision.

- Edge-hunting (betting only when the model disagrees with the market, sized by disagreement magnitude) selects for cases where the model is wrong. It is not a viable primary strategy with a language-model forecaster. The EV framework replaces it.
- Kelly sizing amplifies estimator errors. With a mediocre estimator, it produces win-small-lose-big patterns. Flat sizing until calibration is proven.
- Autonomous config changes tuning on small samples drift in harmful directions. All config changes require user approval, even when Delfi proposes them.
- Shadow mode with disabled risk brakes does not simulate live trading. It produces data that is systematically biased toward the strategy's best case. Shadow and live run identical risk parameters.
- Brier score is not profit. A bot can be well-calibrated and still lose money. Brier is a diagnostic of forecaster health, not a performance target.

These are settled questions. Future sessions should not reopen them.

## Closing

Delfi exists to make money for its users by being a better trader than the crowd it trades against. Every feature, every line of code, every design decision serves that goal. When the goal and the process conflict, the goal wins. When a sophisticated idea costs money, a simpler idea that makes money is better. When the vision and the data conflict, the data wins.

Build toward the vision. Measure against the goal. Let evidence drive the rest.
