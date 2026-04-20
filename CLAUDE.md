# Trading Bot — Project Context for Claude Code

## Read this first, every time

This file is the single source of truth. It overrides everything else.
Read it fully before touching any code.

---

## What this is

A personal autonomous prediction-market bot on a Mac Mini M4.
Trades binary outcomes on Polymarket using calibrated probability estimates.
Python 3.12 backend. Next.js dashboard.
Runs 24/7 as a launchd service. Control: ./bot.sh start|stop|restart|status|logs|errors

Core philosophy: Claude evaluates markets, estimates probabilities, and the
system sizes positions using quarter-Kelly when there's sufficient edge.
No manual rules, no hardcoded thresholds beyond safety guardrails.
Learns from every prediction via Brier score calibration feedback loop.

Current state: shadow mode (simulated fills), $1000 virtual bankroll.
Go-live gates: Brier < 0.22, >= 150 resolved predictions, synthetic P&L > $0.

---

## Architecture

### Pipeline

1. **Market Discovery** — feeds/polymarket_feed.py
   Fetches candidate markets from Polymarket Gamma API (public, read-only).
   Filters: volume > PM_MIN_VOLUME_24H_USD, days to end PM_MIN/MAX_DAYS_TO_END,
   price band 0.08–0.92. Short-horizon markets (≤7d) prioritized.
   Scans every PM_SCAN_INTERVAL_MINUTES (default 30).

2. **Research** — research/fetcher.py
   Category-aware multi-source research pipeline:
   - LLM keyword extraction (Gemini Flash preferred, Claude fallback)
   - DuckDuckGo web search with category-specific queries
   - Full page extraction via trafilatura for top results
   - Wikipedia lead sections, ESPN/CoinGecko structured data
   - RSS headlines from news_event_log, NewsAPI (optional)
   - Historical base rates from calibration DB

3. **Evaluation** — engine/polymarket_evaluator.py
   Claude Sonnet estimates p(YES) + confidence for each market.
   Market price IS shown as a prior — Claude is asked to evaluate whether
   the crowd price is too high, too low, or fair, and must justify large
   deviations (>10pp). Days to resolution and volume are provided as context.
   Retry with exponential backoff on API failures (3 attempts).

4. **Sizing** — execution/pm_sizer.py
   Quarter-Kelly with guardrails:
   - Max edge ceiling: PM_MAX_EDGE_BPS (2500 = 25pp). Refuse when Claude
     claims extreme disagreement with the market — almost always wrong.
   - Cheap NO protection: refuse NO entries below 12c (88%+ favorites).
   - Min edge gate: PM_MIN_EDGE_BPS (300 shadow, 500 live)
   - Lockup penalty: min edge *= sqrt(days_to_end / 7). A 30-day lockup
     needs 2.1x the edge of a 7-day trade. 45 days needs 2.5x.
   - Min confidence gate: PM_MIN_CONFIDENCE (0.30 shadow, 0.45 live)
   - Confidence scaling: stake *= confidence
   - Disagreement penalty: when edge > 15pp, linearly shrink the Kelly
     fraction. Kelly is anti-robust to edge errors: the biggest bets
     are where Claude is most likely wrong (data: -0.46 correlation).
   - Max position: PM_MAX_POSITION_PCT (5% of bankroll)
   - Absolute limits: PM_MIN_TRADE_USD ($2) to PM_MAX_TRADE_USD ($25)
   - Price sanity: refuse entry prices outside [0.02, 0.98]
   - Per-archetype overrides: sports_match needs 800bps edge

   After sizing, every trade passes through the portfolio-level risk manager:

   **Risk Manager** — execution/risk_manager.py
   - Drawdown circuit breaker: halt ALL trading at 40% drawdown from peak
   - Daily loss limit: 10% of starting bankroll per day
   - Weekly loss limit: 20% of starting bankroll per week
   - Portfolio heat: max 30% of bankroll in open positions
   - Consecutive loss cooldown: 3 losses → 50% size for next 3 trades
   - Event concentration: max 3 positions per event group
   - Archetype concentration: max 10 positions per archetype
   API: GET /api/risk returns full risk state for dashboard.

5. **Execution** — execution/pm_executor.py
   Shadow mode: simulates fill at market price, writes to DB.
   Live mode: NOT YET IMPLEMENTED — raises NotImplementedError.
   Will use py-clob-client when Polymarket CLOB credentials are provided.

6. **Resolution** — polymarket_runner.py
   Checks every PM_RESOLVE_INTERVAL_HOURS (1h) for settled markets.
   Updates P&L, feeds calibration system, sends Telegram notifications.

7. **Calibration** — calibration.py
   Tracks Brier score, reliability diagram, per-category breakdown.
   All predictions scored (traded AND skipped) — measures Claude's
   forecasting accuracy, not just trading accuracy.

8. **Self-improvement** — engine/self_improvement.py
   Weekly analysis: per-category Brier, P&L attribution, sizing review.
   Updates Obsidian memory files. Telegram sends suggestions.
   /apply or /skip via Telegram to accept/reject config changes.

### Dashboard — dashboard/

Next.js app at localhost:65002 (dev) or via launchd.
Proxies all data through bot_api.py (aiohttp on localhost:8765).
Auto-refreshes every 30s with JSON-stringify dedup to prevent no-op renders.

Components:
  HeaderBar        — mode, bankroll, equity, uptime
  StatsStrip       — 4 KPIs: open positions, markets analysed, settled, skipped
  ActionsStrip     — scan now, resolve now, refresh
  PositionsTable   — open + settled tabs, resolution countdown
  CalibrationPanel — scatter plot + Brier + trend + per-category breakdown
  GoLiveGate       — 3 gates with progress bars (Brier, resolved count, P&L)
  EvaluationsTable — all evaluations with expandable reasoning (click row)
  ConfigPanel      — edit PM config with Telegram confirmation

### Backtester — backtester/

Parameter-sensitivity backtester. Replays historical evaluations from DB
through the sizer with configurable params. Does NOT call Claude.
  python -m backtester.pm_backtest              # single config
  python -m backtester.pm_backtest --sweep      # 144-config grid search

---

## Key files

config.py                      All PM_* sizing/discovery/scheduling params
main.py                        Startup: scheduler, feeds, bot_api, Telegram
bot_api.py                     HTTP API for dashboard (aiohttp, localhost:8765)
calibration.py                 Brier scoring, prediction logging, reliability
polymarket_runner.py            Resolution loop, settlement detection

feeds/polymarket_feed.py       Gamma API client, market filtering
engine/pm_analyst.py           Main evaluation loop, orchestrates pipeline
engine/polymarket_evaluator.py Claude prompt, JSON parsing, retry logic
execution/pm_sizer.py          Quarter-Kelly + lockup penalty + guardrails
execution/pm_executor.py       Shadow fills, live stub, settlement logic
research/fetcher.py            Wikipedia + news research for context

engine/self_improvement.py     Weekly analysis + Obsidian memory update
engine/memory.py               Obsidian vault read/write
feeds/telegram_notifier.py     Telegram notifications + command handler

dashboard/hooks/use-dashboard-data.ts   Central data hook (30s poll)
dashboard/lib/format.ts                 Shared formatters
dashboard/lib/bot-proxy.ts              Next.js → bot_api proxy
dashboard/components/PmDashboard.tsx    Main layout

backtester/pm_backtest.py      Parameter sensitivity backtester

---

## Database schema

predictions          — every Claude evaluation (for Brier scoring)
pm_positions         — open/settled positions (mode: shadow|live)
market_evaluations   — audit trail: question, prices, recommendation, reasoning
config_change_history — audit trail for config changes

---

## Telegram commands

/status          Balance, open bets, win rate, accuracy
/scan            Trigger a market scan now
/resolve         Check for settled bets now
/apply           Accept pending self-improvement suggestion
/skip            Reject pending self-improvement suggestion
/confirm         Approve a pending mode switch (shadow→live)
/reject          Cancel a pending mode switch
/help            List all commands

---

## Scheduled jobs (MYT = UTC+8)

Market scan              every PM_SCAN_INTERVAL_MINUTES (60)
Resolution check         every PM_RESOLVE_INTERVAL_HOURS (1)
Daily summary            08:30 MYT
Weekly summary           Monday 08:30 MYT
Self-improvement         Sunday 08:30 MYT

---

## Non-negotiable rules

1.  Claude evaluates; the sizer decides size. No manual overrides.
2.  Never deploy more than PM_MAX_POSITION_PCT per position.
3.  Max PM_MAX_CONCURRENT_POSITIONS open at once.
4.  Lockup penalty: longer markets need proportionally more edge.
5.  Market price IS shown to Claude as a prior during evaluation.
    Claude must justify deviations >10pp from the crowd price.
6.  All predictions logged for calibration — traded AND skipped.
7.  Resolution confirmed from Gamma API before marking settled.
8.  Dashboard config changes apply immediately (no confirmation needed).
    Mode switches (shadow→live) require Telegram confirmation (/confirm or /reject).
    Self-improvement suggestions use /apply or /skip.
9.  Shadow mode and live mode use identical logic except order execution.
10. Go-live gates are advisory — manual PM_MODE flip required.
11. Never modify backtester/ for live bot changes.
12. Live execution stub must not be filled without CLOB credentials.

---

## Go-live checklist

Gates (dashboard shows progress):
  Brier score < 0.22
  >= 150 resolved predictions
  Synthetic P&L > $0

Steps:
1.  Run shadow for 2–4 weeks, monitor dashboard daily
2.  Verify Brier trend is stable or improving
3.  Review per-category calibration — look for systematic biases
4.  Get Polymarket CLOB credentials (API key, proxy address, private key)
5.  Add to .env: POLYMARKET_API_KEY, POLYMARKET_API_SECRET, PROXY_ADDRESS, PRIVATE_KEY
6.  pip install py-clob-client
7.  Implement _open_live() in execution/pm_executor.py
8.  Set PM_MODE = "live" in config.py
9.  Set PM_LIVE_STARTING_CASH to match actual USDC deposit
10. Restart bot: ./bot.sh restart
11. Monitor first 5 live trades manually

---

## Module map

trading-bot/
├── config.py
├── main.py
├── bot_api.py
├── calibration.py
├── polymarket_runner.py
├── .env
├── bot.sh
├── logs/
├── feeds/
│   ├── polymarket_feed.py       Gamma API, market filtering
│   ├── news_feed.py             RSS + Gemini filter
│   ├── macro_calendar.py        FOMC + BLS events
│   └── telegram_notifier.py     notifications + commands
├── engine/
│   ├── pm_analyst.py            main evaluation loop
│   ├── polymarket_evaluator.py  Claude prompt + retry
│   ├── memory.py                Obsidian vault
│   ├── self_improvement.py      weekly analysis
│   └── portfolio_advisor.py     capital advisory
├── execution/
│   ├── pm_executor.py           shadow fills, live stub
│   └── pm_sizer.py              quarter-Kelly + lockup penalty
├── research/
│   └── fetcher.py               Wikipedia + news
├── db/
│   └── models.py                schema definitions
├── backtester/
│   └── pm_backtest.py           parameter sensitivity
└── dashboard/
    ├── app/
    │   ├── page.tsx
    │   ├── globals.css
    │   └── api/                 proxy routes to bot_api
    ├── components/              React components
    ├── hooks/                   data fetching
    └── lib/                     formatters, proxy helpers

---

## Legacy (archived)

The _archive/ directory contains the original OKX crypto futures engine
(Gemini scanner + Claude strategist, Layer A-F, risk engine, etc).
This code is NOT active. The project pivoted to Polymarket prediction
markets. Do not reference archived code for current functionality.
