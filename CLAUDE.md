# Delfi. Project Doctrine

## What Delfi is

Delfi is an autonomous prediction market trader. It watches Polymarket, forecasts which side of every tradeable market will resolve true, and backs that forecast with a small, confidence-scaled stake. It manages positions dynamically and learns from every resolution.

The product is both the trader and the experience of watching it trade. Users connect their Polymarket account. Delfi goes to work. They see its reasoning on a live dashboard, watch positions move in real time, and witness the calibrated intelligence of a system that treats every market as a solvable problem.

## The goal

**Make money.** Maximize ROI on bankroll across all trades. That is the only metric that matters. Win rate, calibration, Brier score are diagnostics, not targets. If a proposed change improves expected ROI with evidence, ship it. If it doesn't, don't.

The forecaster is the product. Every dollar of ROI depends on the model being right more often than wrong on its own picks. Engineering effort goes into making the forecaster better: richer research, stronger prompts, ensemble construction, calibration analysis, learning from resolved markets. The sizer stays narrow and dumb on purpose.

## The product experience

Delfi is a character as much as a system. The dashboard feels alive because it is doing real work in real time. Reasoning is visible because transparency is both ethical and retention-positive. Losses are narrated honestly because hiding them destroys trust.

- ROI is the most prominent metric on every surface. P&L shown in dollars.
- The Delfi persona (oracle, prophecy, ethereal visuals) is marketing. Belongs in hero copy and brand assets.
- Product surfaces use clinical precision: *"Estimated probability 0.62. Resolved NO. P&L -$4.12."*

## Metrics. What counts as performance

Win rate, ROI, and Brier score are computed **only on predictions the bot actually entered** (i.e. resolved positions with a real or simulated fill). Skipped evaluations never enter these metrics. A skip is not a loss and not a win; it is a non-event. Every dashboard number, Telegram report, learning-cycle input, and go-live gate must use this "resolved-positions-only" definition so the numbers reflect the bot's real performance.

## Tech stack & where things live

Monorepo, two deployables:

- **`apps/bot`**. Python 3.11, the trading engine. Long-running process on **Railway**. Key deps: `anthropic`, `google-genai`, `aiohttp` (HTTP API), `sqlalchemy` + `psycopg2-binary` (Postgres), `APScheduler`, `ccxt`. Layout:
  - `engine/`. forecasting, ensemble, calibration, self-improvement
  - `execution/`. sizing + risk manager
  - `feeds/`. data sources (Polymarket, news, macro calendar, Telegram notifier)
  - `research/`. web fetchers for market research
  - `db/`. SQLAlchemy models
  - Entrypoints: `main.py` (scheduler), `polymarket_runner.py` (scan worker), `bot_api.py` (aiohttp HTTP server)
- **`apps/web`**. Next.js 16 + React 19 + TypeScript + Tailwind v4. Deployed on **Vercel**. Supabase SSR for auth. The dashboard talks to the bot over HTTP via `lib/bot-proxy.ts` with an `X-Bot-Secret` header. The bot is never exposed to the browser directly.

**Database.** Supabase Postgres. Schema owned by `apps/bot/db/models.py`. Migrations in `ops/supabase/migrations/`. The `trading_bot.db` file at repo root is legacy/unused; ignore it. Every user-facing row is keyed by `user_id`; the bot is multi-tenant.

**Auth.** Supabase Auth. Google OAuth + email/password. Callback at `/auth/callback`. Session refresh in `apps/web/proxy.ts`.

**Next.js 16 is not the Next.js in your training data.** See `apps/web/AGENTS.md`. Notable: `proxy.ts` not `middleware.ts`, `await cookies()` / `await headers()`, `useSearchParams()` must be inside `<Suspense>`.

## How code reaches production

`git push origin main` triggers Vercel (web) and Railway (bot) auto-deploys. **There is no local verification step. The user tests on Vercel, not localhost.** Commit and push every change immediately; don't hold local-only work. If a web change doesn't appear in prod, the cause is usually that the code never left the laptop.

## Multi-tenancy invariants

- Every user-facing row has a `user_id` column: positions, trades, configs, evaluations.
- Per-user credentials (Polymarket keys, Telegram bot token + chat ID) live in `user_config`, not process env.
- No process-global API keys for anything user-scoped. Env vars are for shared infrastructure only (DB URL, anthropic key, bot secret).
- User-editable config (risk params, volume floors, skip lists) applies immediately from the dashboard. Only AI self-improvement suggestions require explicit approval.

## Banned terminology and punctuation

These are not style preferences. Violations will be rejected.

- **"edge" / "edge-hunting"**: use "forecast", "prediction", or "calibration".
- **"shadow"**: use "simulation" everywhere (code constants, DB values, env var values, UI, docs).
- **Em dashes (—)** are banned in all copy (user-facing UI, Telegram messages, marketing copy, docs, READMEs, code comments, docstrings). Use a hyphen, a period, a comma, a colon, or parentheses instead. No exceptions.

## The core principle. Simple on purpose

**Forecast the outcome. Back the forecast.**

Delfi bets the side its ensemble thinks will win. Price does not enter the side-selection decision. There is no "bet the cheaper side relative to our probability" filter. That structure selects for cases where the forecaster is wrong and loses money.

Two operational gates plus a confidence softener live in code and in `memory/doctrine_back_the_forecast.md`:

1. **Direction agreement** (never skips on its own; determines side).
2. **Minimum chosen-side probability** (`min_p_win`).
3. **Confidence softener** (shrinks stake on low confidence; never skips alone).

An earlier third gate (minimum expected return after cost) was removed because it skipped heavy favourites where the math still favoured taking the bet. The numbers in the memory file are authoritative; never invent or reset them from this document.

Sizing is flat and scaled by model confidence only. Not by disagreement size, not by price, not by anything else. Simple sizing keeps variance per trade low so the portfolio learns fast.

## Risk management

Circuit breakers protect the bankroll from catastrophic loss. They run identically in simulation and live; simulation actually simulates live. Parameters (daily/weekly loss limits, drawdown halt, streak cooldown, dry powder reserve, max stake) are per-user editable in the dashboard within system bounds. Bounds exist so users cannot configure obviously catastrophic settings. Within the bounds the user is in control.

Current defaults and bounds are defined in `apps/bot/execution/risk_manager.py`. That file is authoritative.

## Learning and iteration

Delfi learns continuously and proposes config changes autonomously, but does not apply them autonomously. Every config change is a deliberate user decision with evidence.

Learning cadence is trade-volume-based, not calendar-based. Every 50 settled trades Delfi runs a full analysis pass: ROI and calibration by category, proposed skip-list or prompt changes with backtest evidence. User reviews and applies via `/apply` or `/skip` in Telegram.

## How we decide

For every proposed change:

1. Does this improve expected ROI?
2. Is there evidence (backtest, historical data, live performance) supporting the improvement?
3. What is the smallest version that tests the hypothesis?

If the answers are yes, yes, and clear, ship the small version. Measure. Expand if it works, revert if it doesn't.

## Taxonomy rules

Market archetypes are **flat**. One label per sport, no nesting. Use `tennis`, not `tennis_qualifier` / `tennis_main_draw` / `tennis_lower_tier`. Use `basketball`, not `basketball_game` / `basketball_prop`. Same rule for baseball, football, hockey, esports, soccer, cricket. Non-sport archetypes follow the same flat shape: `price_threshold`, `activity_count`, `geopolitical_event`, `binary_event`, `sports_other`.

Source of truth: the `ARCHETYPES` tuple in `apps/bot/engine/archetype_classifier.py`. When adding a classifier branch:

1. If the new pattern fits an existing canonical label, return that label. Do not invent a sub-tier.
2. If the new pattern genuinely does not fit, add a single new canonical label to `ARCHETYPES` **and** to `BUILTIN_ARCHETYPES` in `apps/web/app/dashboard/settings/risk/page.tsx`. Keep the two lists in sync.
3. Any label ever emitted by an older classifier goes into `LEGACY_ARCHETYPE_MAP` (runtime collapse) and into a migration under `ops/supabase/migrations/` (historical row rewrite). Migration 022 is the canonical example.

Never reintroduce sub-tier labels to work around a specific pattern. If a branch of the taxonomy grows, split it at the canonical level or keep it flat. This rule was settled because sub-tier labels caused per-category analytics to double-bucket the same sport and the dashboard to show chips that did not match what the classifier emitted.

## Settled lessons that must not be re-litigated

- Filtering for disagreement with the market is a losing strategy. Delfi backs the model's pick directly.
- Kelly sizing amplifies estimator errors on noisy forecasters (produces win-small-lose-big). Flat, confidence-scaled sizing until calibration is proven per category.
- Autonomous config changes on small samples drift harmfully. All config changes require user approval.
- Simulation with disabled risk brakes does not simulate live. Identical risk parameters across both modes.
- Brier score is not profit. A well-calibrated bot can still lose money. Brier is diagnostic, not target.
- Short-horizon sports (tennis, qualifiers, low-tier matches) have cost us money. Default skip list until category evidence says otherwise.
- Metrics count only entered predictions. Skipped evaluations never enter ROI, win rate, or Brier. Anything else misreports the bot's real performance.

## Closing

Delfi exists to make money for its users. It does that by forecasting outcomes as accurately as possible and backing those forecasts with small, confidence-scaled stakes. Everything else is a safety gate, a risk brake, or an optimization of the forecaster itself.

Forecast the outcome. Back the forecast. Measure the result. Improve.

## Anti-Compression Memory

### The problem you must solve

As this conversation grows, older context gets compressed and you lose details. Counteract this by externalising everything important to Obsidian so you can re-read it whenever you feel uncertain about context.

### WRITE to Obsidian when

- You make a decision (any decision).
- You discover something about the codebase.
- The user tells you something about their preferences, constraints, or goals.
- Something doesn't work and you figure out why.
- You complete a task.
- You learn what files do what.

### READ from Obsidian when

- You're about to ask the user something (check if you already know).
- You feel uncertain about context ("wait, why are we doing this?").
- Starting a new task.
- Something feels like it might conflict with an earlier decision.

### The rule

If losing a piece of information mid-conversation would cause you to do something wrong or redundant, write it to Obsidian now, before it gets compressed out.
