# Delfi — Project Doctrine

## What Delfi is

Delfi is an autonomous prediction market trader. It watches Polymarket, forecasts which side of every tradeable market will resolve true, and backs that forecast with a small, confidence-scaled stake. It manages positions dynamically and learns from every resolution.

The product is both the trader and the experience of watching it trade. Users connect their Polymarket account. Delfi goes to work. They see its reasoning on a live dashboard, watch positions move in real time, and witness the calibrated intelligence of a system that treats every market as a solvable problem.

## The goal

**Make money.** Maximize ROI on bankroll across all trades. That is the only metric that matters. Win rate, calibration, Brier score — diagnostics, not targets. If a proposed change improves expected ROI with evidence, ship it. If it doesn't, don't.

The forecaster is the product. Every dollar of ROI depends on the model being right more often than wrong on its own picks. Engineering effort goes into making the forecaster better — richer research, stronger prompts, ensemble construction, calibration analysis, learning from resolved markets. The sizer stays narrow and dumb on purpose.

## The product experience

Delfi is a character as much as a system. The dashboard feels alive because it is doing real work in real time. Reasoning is visible because transparency is both ethical and retention-positive. Losses are narrated honestly because hiding them destroys trust.

- ROI is the most prominent metric on every surface. P&L shown in dollars.
- The Delfi persona — oracle, prophecy, ethereal visuals — is marketing. Belongs in hero copy and brand assets.
- Product surfaces use clinical precision: *"Estimated probability 0.62. Resolved NO. P&L -$4.12."*

## Tech stack & where things live

Monorepo, two deployables:

- **`apps/bot`** — Python 3.11, the trading engine. Long-running process on **Railway**. Key deps: `anthropic`, `google-genai`, `aiohttp` (HTTP API), `sqlalchemy` + `psycopg2-binary` (Postgres), `APScheduler`, `ccxt`. Layout:
  - `engine/` — forecasting, ensemble, calibration, self-improvement
  - `execution/` — sizing + risk manager
  - `feeds/` — data sources (Polymarket, news, macro calendar, Telegram notifier)
  - `research/` — web fetchers for market research
  - `db/` — SQLAlchemy models
  - Entrypoints: `main.py` (scheduler), `polymarket_runner.py` (scan worker), `bot_api.py` (aiohttp HTTP server)
- **`apps/web`** — Next.js 16 + React 19 + TypeScript + Tailwind v4. Deployed on **Vercel**. Supabase SSR for auth. The dashboard talks to the bot over HTTP via `lib/bot-proxy.ts` with an `X-Bot-Secret` header — the bot is never exposed to the browser directly.

**Database** — Supabase Postgres. Schema owned by `apps/bot/db/models.py`. Migrations in `ops/supabase/migrations/`. The `trading_bot.db` file at repo root is legacy/unused — ignore it. Every user-facing row is keyed by `user_id`; the bot is multi-tenant.

**Auth** — Supabase Auth. Google OAuth + email/password. Callback at `/auth/callback`. Session refresh in `apps/web/proxy.ts`.

**Next.js 16 is not the Next.js in your training data.** See `apps/web/AGENTS.md`. Notable: `proxy.ts` not `middleware.ts`, `await cookies()` / `await headers()`, `useSearchParams()` must be inside `<Suspense>`.

## How code reaches production

`git push origin main` triggers Vercel (web) and Railway (bot) auto-deploys. **There is no local verification step — the user tests on Vercel, not localhost.** Commit and push every change immediately; don't hold local-only work. If a web change doesn't appear in prod, the cause is usually that the code never left the laptop.

## Multi-tenancy invariants

- Every user-facing row has a `user_id` column — positions, trades, configs, evaluations.
- Per-user credentials (Polymarket keys, Telegram bot token + chat ID) live in `user_config`, not process env.
- No process-global API keys for anything user-scoped. Env vars are for shared infrastructure only (DB URL, anthropic key, bot secret).
- User-editable config (risk params, volume floors, skip lists) applies immediately from the dashboard. Only AI self-improvement suggestions require explicit approval.

## Banned terminology

These are not style preferences — violations will be rejected.

- **"edge" / "edge-hunting"** → use "forecast", "prediction", or "calibration".
- **"shadow"** → use "simulation" everywhere (code constants, DB values, env var values, UI, docs).

## The core principle — simple on purpose

**Forecast the outcome. Back the forecast.**

Delfi bets the side its ensemble thinks will win. Price does not enter the side-selection decision. There is no "bet the cheaper side relative to our probability" filter — that structure selects for cases where the forecaster is wrong and loses money.

Operational gates (direction agreement, minimum chosen-side probability, minimum expected return after cost, confidence softener) live in code and in `memory/doctrine_back_the_forecast.md`. The numbers in that memory file are authoritative; never invent or reset them from this document.

Sizing is flat and scaled by model confidence only. Not by disagreement size, not by price, not by anything else. Simple sizing keeps variance per trade low so the portfolio learns fast.

## Risk management

Circuit breakers protect the bankroll from catastrophic loss. They run identically in simulation and live — simulation actually simulates live. Parameters (daily/weekly loss limits, drawdown halt, streak cooldown, dry powder reserve, max stake) are per-user editable in the dashboard within system bounds. Bounds exist so users cannot configure obviously catastrophic settings. Within the bounds the user is in control.

Current defaults and bounds are defined in `apps/bot/execution/risk_manager.py` — that file is authoritative.

## Learning and iteration

Delfi learns continuously and proposes config changes autonomously, but does not apply them autonomously. Every config change is a deliberate user decision with evidence.

Learning cadence is trade-volume-based, not calendar-based. Every 50 settled trades Delfi runs a full analysis pass: ROI and calibration by category, proposed skip-list or prompt changes with backtest evidence. User reviews and applies via `/apply` or `/skip` in Telegram.

## How we decide

For every proposed change:

1. Does this improve expected ROI?
2. Is there evidence — backtest, historical data, live performance — supporting the improvement?
3. What is the smallest version that tests the hypothesis?

If the answers are yes, yes, and clear — ship the small version. Measure. Expand if it works, revert if it doesn't.

## Settled lessons that must not be re-litigated

- Filtering for disagreement with the market is a losing strategy. Delfi backs the model's pick directly.
- Kelly sizing amplifies estimator errors on noisy forecasters — produces win-small-lose-big. Flat, confidence-scaled sizing until calibration is proven per category.
- Autonomous config changes on small samples drift harmfully. All config changes require user approval.
- Simulation with disabled risk brakes does not simulate live. Identical risk parameters across both modes.
- Brier score is not profit. A well-calibrated bot can still lose money. Brier is diagnostic, not target.
- Short-horizon sports (tennis, qualifiers, low-tier matches) have cost us money — default skip list until category evidence says otherwise.

## Closing

Delfi exists to make money for its users. It does that by forecasting outcomes as accurately as possible and backing those forecasts with small, confidence-scaled stakes. Everything else is a safety gate, a risk brake, or an optimization of the forecaster itself.

Forecast the outcome. Back the forecast. Measure the result. Improve.
