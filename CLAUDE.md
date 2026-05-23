# Delfi. Project Doctrine

## What Delfi is

Delfi is an autonomous prediction market trader. It watches Polymarket, follows the market favourite on every tradeable market its forecaster also points at, and stakes a flat fraction of bankroll scaled only by per-archetype multipliers. It manages positions dynamically and learns from every resolution.

The product is both the trader and the experience of watching it trade. Users connect their Polymarket account. Delfi goes to work. They see its reasoning on a live dashboard, watch positions move in real time, and witness a system that respects the market's pricing on each trade while filtering out the trades its own research disagrees with.

## The goal

**Make money.** Maximize ROI on bankroll across all trades. That is the only metric that matters. Win rate, calibration, Brier score are diagnostics, not targets. If a proposed change improves expected ROI with evidence, ship it. If it doesn't, don't.

The forecaster is the filter, not the picker. The market picks the side. The forecaster decides whether Delfi takes the trade at all (skip if the forecast disagrees with the market favourite) and informs per-archetype tuning over time. Engineering effort goes into making the forecaster better calibrated against the market price (so its disagreements are signal, not noise), into the per-archetype skip and multiplier defaults, and into the learning loop that proposes those defaults from settled-trade data. The sizer stays narrow and dumb on purpose.

## The product experience

Delfi is a character as much as a system. The dashboard feels alive because it is doing real work in real time. Reasoning is visible because transparency is both ethical and retention-positive. Losses are narrated honestly because hiding them destroys trust.

- ROI is the most prominent metric on every surface. P&L shown in dollars.
- The Delfi persona (oracle, prophecy, ethereal visuals) is marketing. Belongs in hero copy and brand assets.
- Product surfaces use clinical precision: *"Estimated probability 0.62. Resolved NO. P&L -$4.12."*

## Metrics. What counts as performance

Win rate, ROI, and Brier score are computed **only on predictions the bot actually entered** (i.e. resolved positions with a real or simulated fill). Skipped evaluations never enter these metrics. A skip is not a loss and not a win; it is a non-event. Every dashboard number, Telegram report, learning-cycle input, and go-live gate must use this "resolved-positions-only" definition so the numbers reflect the bot's real performance.

## Tech stack & where things live

Single deployable: a Tauri 2 desktop app that bundles a Python sidecar. Everything runs on the user's machine. No website, no server-side bot, no Supabase. The repo root holds `Delfibot/` (active) + `docs/` + `SITEMAP.md` + `LAUNCH_CHECKLIST.md` + `.github/workflows/build.yml`. Pre-pivot SaaS scaffolding (apps/, ops/, packages/) was deleted 2026-05-19.

- **`Delfibot/bot/`** — the active product.
  - `engine/` — forecasting (`polymarket_evaluator.py`), V1 sizer-side gates (`pm_sizer.py` reads `MarketEvaluation.force_skip` + direction-agreement), calibration, learning cadence, license verification, live data adapters (live_crypto, live_equity).
  - `execution/` — `pm_executor.py` (open/close orders via py-clob-client-v2; uses `_extract_filled_size` to record actual fills, not intent), `pm_sizer.py` (flat archetype-multiplied stake; `max_stake_pct_enabled` opt-in cap), `pm_redeemer.py` (gasless CTF redeem via RELAYER_API_KEY + USDC.e→pUSD activator).
  - `feeds/` — Polymarket gamma/CLOB, polymarket_wallet probe (pUSD + USDC.e summed), Telegram notifier (optional).
  - `research/` — DuckDuckGo search + trafilatura article extraction.
  - `db/` — SQLAlchemy models against local SQLite (`<app-data>/delfi.db`). Schema migrates on boot.
  - `local_api.py` — aiohttp HTTP server with a dedicated `_api_executor` (8 workers) isolated from the default loop executor so analyst LLM work can't starve dashboard endpoints.
  - `main.py` — daemon entrypoint. APScheduler jobs: `pm_scan` (5 min), `pm_resolve` (15 min), `pm_resolve_fast` (60 s), `pm_evaluate_exits` (60 s), `pm_resolve_skipped` (15 min), `pm_balance_refresh` (60 s), `pm_redeem_sweep` (10 min), `pm_activate_legacy` (10 min), `markout_check` (1 h).
  - `src/` — React 19 + Vite frontend (`Dashboard.tsx`, `Positions.tsx`, `Performance.tsx`, `Intelligence.tsx`, `Risk.tsx`, `Settings.tsx`).
  - `src-tauri/` — Rust shell. `main.rs` reads the daemon's port file with a 15 s retry budget; `restart_sidecar` Tauri command bounds every shelled call with a hard timeout. Compiled into Delfi.app.

**Database.** Local SQLite at `<app-data>/delfi.db`. Schema owned by `Delfibot/bot/db/models.py`; migrations run on boot via PRAGMA-probed ALTER TABLE branches. Single-user by construction (`user_id` always `"local"`).

**Auth.** None. License gating is Ed25519-signed offline blobs verified in `engine/license.py`. Keygen tool lives in `Delfibot/scripts/generate-license-keypair.mjs`.

**Secrets.** `<app-data>/data/secrets.json` (file-backed, migrated from legacy keychain on first read). Fields: `polymarket_private_key`, `polymarket_relayer_api_key`, `anthropic_api_key`, `gemini_api_key`, `newsapi_key`, `telegram_bot_token`, `license_key`.

## How code reaches production

There is no remote infrastructure. To ship a new version:

1. `cd Delfibot/bot && ./scripts/build_sidecar.sh` — PyInstaller bundles the sidecar.
2. `npm run tauri build -- --bundles app` — Tauri builds the macOS `.app`.
3. `bash Delfibot/install.sh` — rsyncs into `/Applications/Delfi.app`, reloads the LaunchAgent. The daemon restarts under launchd's KeepAlive; the GUI relaunches.

**Step 1 is mandatory whenever ANY file under `Delfibot/bot/**/*.py` changed.** `npm run tauri build` only rebuilds the Rust shell + React frontend; it does NOT re-run PyInstaller. The PyInstaller-bundled `delfi-sidecar` is what launchd actually executes, so skipping step 1 leaves the daemon running pre-fix Python even after the install completes. This has burned us at least once (2026-05-23: a "removed" `position_untracked` alert kept firing for 3 hours after the commit because only steps 2 + 3 ran). When in doubt, run step 1 anyway, it costs ~60 seconds. See `Obsidian/Delfi/50_Feedback/log_every_major_bug.md` (entry: "banned message keeps showing up... hours after the fix commit") for the post-mortem.

**When killing a user-visible alert, also delete persisted instances.** The dashboard reads from `event_log` (and other tables); removing the writer in code leaves the old rows visible until they age out. Standard cleanup after such a commit: `sqlite3 ~/Library/Application\ Support/com.delfi.desktop/delfi.db "DELETE FROM event_log WHERE event_type = '<the_event_type>' AND timestamp < datetime('now');"` (or filtered by description LIKE if the event_type is too broad). Same incident as above.

CI cross-compiles for macOS + Windows on every push to `main` via `.github/workflows/build.yml`. Tagged pushes (`v*`) auto-publish to GitHub Releases. No code signing yet.

## Single-user invariants

- One user per install. No tenant routing.
- Credentials live in `<app-data>/data/secrets.json`, never in process env.
- User-editable config lives in the `user_config` SQLite table; in-app Risk page mutations apply immediately. AI self-improvement suggestions still require explicit approval via the Intelligence page.

## Banned terminology and punctuation

These are not style preferences. Violations will be rejected.

- **"edge" / "edge-hunting"**: use "forecast", "prediction", or "calibration".
- **"shadow"**: use "simulation" everywhere (code constants, DB values, env var values, UI, docs).
- **Em dashes (—)** are banned in all copy (user-facing UI, Telegram messages, marketing copy, docs, READMEs, code comments, docstrings). Use a hyphen, a period, a comma, a colon, or parentheses instead. No exceptions.

## The core principle. Simple on purpose

**Follow the market. Use the forecast as a filter.**

Delfi bets the side the market favours (the side with implied probability >= 0.50). The forecaster's job is the veto: if it disagrees with the market's pick, skip the trade. The forecaster does not pick the side, does not size the stake, and does not override the market's price.

This is the V1 doctrine, locked 2026-04-27. It replaces the prior "back the forecast" doctrine after a 250-trade simulation-mode counterfactual showed the prior architecture was systematically losing money to a market-default baseline (V0 actual: -3.53% ROI; V1 selected: +14.47% ROI on the same trades). Authoritative numbers and the rejected alternatives live in `memory/doctrine_back_the_forecast.md`; never invent or reset them from this document.

The single operational gate that lives in code:

1. **Delfi direction agreement** — `claude_p_yes` and `market_p_yes` must be on the same side of 0.50; otherwise skip.

There are no other gates. `min_p_win`, the confidence softener, and the historical "minimum expected return" gate are all retired. The market favourite is by definition >= 0.50, so a `min_p_win` filter would just clip the most-profitable narrow-favourite band; the confidence softener was empirically anti-signal (high-confidence Delfi picks won 52.9%, low-confidence won 67.6%); the EV gate was retired in V0 and stays banned in V1 because it silenced positive-EV heavy favourites.

Sizing is flat and scaled only by per-archetype multipliers (default 1.0 for unknown archetypes; specific defaults `basketball: 1.5`, `tennis: 0.5`). Skip list is a hard skip (default `sports_other`, `hockey`, `cricket`). Both lists are user-editable in the dashboard. Simple sizing keeps variance per trade low so the portfolio learns fast.

## Risk management

Circuit breakers protect the bankroll from catastrophic loss. They run identically in simulation and live; simulation actually simulates live. Parameters (daily/weekly loss limits, drawdown halt, streak cooldown, dry powder reserve, max stake) are per-user editable in the dashboard within system bounds. Bounds exist so users cannot configure obviously catastrophic settings. Within the bounds the user is in control.

Current defaults and bounds are defined in `Delfibot/bot/engine/risk_manager.py`. That file is authoritative.

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

Source of truth: the `ARCHETYPES` tuple in `Delfibot/bot/engine/archetype_classifier.py`. When adding a classifier branch:

1. If the new pattern fits an existing canonical label, return that label. Do not invent a sub-tier.
2. If the new pattern genuinely does not fit, add a single new canonical label to `ARCHETYPES` **and** to the archetype catalogue in `Delfibot/bot/local_api.py` (the `_get_archetypes` route's label/description dict) **and** to the group-ordering array in `Delfibot/bot/src/pages/Risk.tsx`. Keep the three places in sync.
3. Any label ever emitted by an older classifier goes into `LEGACY_ARCHETYPE_MAP` (runtime collapse) and into a migration under `ops/supabase/migrations/` (historical row rewrite). Migration 022 is the canonical example.

Never reintroduce sub-tier labels to work around a specific pattern. If a branch of the taxonomy grows, split it at the canonical level or keep it flat. This rule was settled because sub-tier labels caused per-category analytics to double-bucket the same sport and the dashboard to show chips that did not match what the classifier emitted.

## Settled lessons that must not be re-litigated

- **Backing the forecast against the market is a losing strategy on this dataset.** Reversed 2026-04-27 from the V0 lesson "filtering for disagreement with the market is a losing strategy". The 250-trade counterfactual is unambiguous: market beats Delfi on the disagreement subset 65.7% to 34.3% (non-overlapping 95% CIs), Delfi-as-picker delivered -3.53% ROI, market-default-with-Delfi-veto delivered +14.47%. The forecaster has signal in aggregate but is anti-signal exactly where it has an opinion that differs from the price. V1 follows the market and uses the forecast only as a skip filter.
- Kelly sizing amplifies estimator errors on noisy forecasters (produces win-small-lose-big). Flat, archetype-multiplier-scaled sizing under V1 (no confidence input).
- Autonomous config changes on small samples drift harmfully. All config changes require user approval.
- Simulation with disabled risk brakes does not simulate live. Identical risk parameters across both modes.
- Brier score is not profit. A well-calibrated bot can still lose money. Brier is diagnostic, not target.
- Short-horizon sports (tennis, qualifiers, low-tier matches) have cost us money. V1 default skip list is `sports_other`, `hockey`, `cricket`; tennis is half-staked rather than skipped because the market itself is reasonably accurate on tennis.
- Metrics count only entered predictions. Skipped evaluations never enter ROI, win rate, or Brier. Anything else misreports the bot's real performance.
- The V0 confidence softener is retired. High-confidence Delfi picks lost more often than low-confidence picks on this dataset. Do not reintroduce confidence-scaled sizing without per-archetype evidence that calibration has improved.
- The V0 `min_p_win` floor is retired. Under V1, side selection is the market favourite (>= 0.50 by definition); a min_p_win at 0.55 would just clip the profitable 0.50-0.55 band.
- **Polymarket is the source of truth for every money number.** Bankroll, locked capital, equity, P&L all read from Polymarket's APIs (wallet probe + data-api `/positions` + `user-pnl-api`). Local recomputation is a fallback only, and only when no Polymarket signal exists (sim mode). The dashboard, the Telegram messages, and the risk gates must all source the same primitive; ANY divergence from polymarket.com is a bug. Fixed across 2026-05-23 commits; see Obsidian/Delfi/50_Feedback/data_must_match_polymarket.md.
- **`pending_payout` projection is a 10-minute window only.** The settled-but-not-yet-redeemed bridge in `pm_executor.get_portfolio_stats` MUST exclude `status='invalid'` (Polymarket auto-refunds those directly, no relayer redeem) AND MUST cap `settled_at > datetime('now', '-10 minutes')`. Any older row that lingers in the projection inflates Balance forever (incident 2026-05-23: a 3-day-old invalid row added phantom $10.88 to every Balance reading for 72 hours). The build-time regression test at `Delfibot/bot/tools/test_pending_payout_guards.py` enforces both guards; the sidecar build script fails if either is removed.
- **The bot owns the entire trade lifecycle. The user never touches Polymarket directly.** If the bot opens a position, it must be able to track, value, and close that position end-to-end. User-facing language like "untracked position", "manage on polymarket.com", "manual review required" is BANNED — it exposes bot failure as user homework. When such a situation is detected (a real bug, not a known market shape), log to stderr for the engineer; do NOT push to Telegram or dashboard. Fix the underlying tracking gap; don't paper over the leak with user paperwork. Set 2026-05-23 after the Bayern Munich neg-risk leak.
- **Negative-risk markets ARE tradable; we trade them.** A `negRisk=true` market is just one member of a multi-outcome group (e.g. soccer match: Bayern win / Draw / Other team win — three separate YES/NO questions linked at the group level). Each individual market is a clean binary YES/NO from the trading perspective: same CLOB order endpoint, same pm_positions.side mapping, same data-api P&L. The only on-chain difference is the redemption contract (NegRiskAdapter vs CTFExchange), which the V2 relayer handles automatically for DepositWallet users — the bot does not call redeem manually. `polymarket_feed._as_market` filters `negRiskOther` (the "everything else" rollup outcome of a group, thin liquidity) but NOT `negRisk` itself. Earlier broad-filter rev (3df8377) excluded both and cost the bot soccer / tournament coverage for no actual technical reason; reverted in the same session. If you see `if m.get("negRisk")` in a feed filter, that's a regression — delete it.

## Closing

Delfi exists to make money for its users. It does that by following the market favourite on every tradeable market its forecaster also points at, with a flat per-archetype-scaled stake. Everything else is a safety gate, a risk brake, or an optimization of the forecaster's filter quality.

Follow the market. Use the forecast as a filter. Measure the result. Improve.

## Anti-Compression Memory

### The problem you must solve

As this conversation grows, older context gets compressed and you lose details. Counteract this by externalising everything important to Obsidian so you can re-read it whenever you feel uncertain about context.

The vault lives at `/Users/macmini/Documents/Obsidian Vault/Delfi/`. Read `START_HERE.md` first on any new session - it indexes the rest. The vault is your operational brain. The repo `CLAUDE.md` (this file) is the doctrine source-of-truth in git. The auto-memory at `~/.claude/projects/.../memory/MEMORY.md` is the cross-session rule index. These three should agree; if they conflict, the most recent date wins and you update the other two.

### Folder routing (where things go)

- `00_Core/` - rules that don't move. Doctrine, tech stack. Edit cautiously.
- `10_State/current.md` - single source of truth for "what am I doing right now". Always current.
- `20_Sessions/YYYY-MM-DD.md` - append-only daily log. One file per day. Every decision and shipped change.
- `30_Todos/open.md` - prioritised todo list with P0/P1/P2/tech-debt/parked.
- `40_Decisions/` - one file per architecture decision. Captures: what, why, alternatives, date.
- `50_Feedback/` - one file per user-enforced rule or correction. The rules that, when violated, cost trust.
- `60_Playbook/` - repeatable how-tos. SQLite query templates, gamma-API quirks, build commands.
- `99_Scratch/` - WIP, half-formed ideas, raw outputs. No discipline expected here.

### WRITE to Obsidian when

- You make a decision (any decision) -> append to today's `20_Sessions/YYYY-MM-DD.md`. Architectural decisions also get a standalone file in `40_Decisions/`.
- You discover something about the codebase -> session log + (if reusable) `60_Playbook/`.
- The user tells you something about their preferences, constraints, or goals -> new file in `50_Feedback/` and one-line link in `00_Core/doctrine.md`.
- Something doesn't work and you figure out why -> session log + (if recurring failure mode) `60_Playbook/`.
- You complete a task -> log line in today's session, mark item done in `30_Todos/open.md`.
- You learn what files do what -> `00_Core/tech_stack.md`.
- Active focus shifts -> update `10_State/current.md`.

### READ from Obsidian when

- You're about to ask the user something - search `50_Feedback/` and `40_Decisions/` first.
- You feel uncertain about context ("wait, why are we doing this?") - check `10_State/current.md` and the most recent session log.
- Starting a new task - read `00_Core/doctrine.md` if it touches the sizer/forecaster/risk/copy.
- Something feels like it might conflict with an earlier decision - search `40_Decisions/`.
- About to write user-facing copy - read `50_Feedback/no_filler_subheadlines.md`.
- About to recommend architecture that touches funds/keys/credentials - read `50_Feedback/custody_question_always_ask.md`.
- About to write or modify a migration / sizer gate / axis-sensitive transform - read `50_Feedback/be_critical_and_intentional.md` and write the invariant block FIRST.
- Noticed a bug, typo, stale reference, or other fixable issue mid-task - DO NOT park it. Read `50_Feedback/fix_dont_park.md`. Fix it the same turn and report the commit.
- After commit + rebuild + install, ALWAYS smoke-test the running daemon (port file fresh, /api/state responds, key endpoint returns expected shape). Read `50_Feedback/test_after_ship.md`. Don't declare a change live until the smoke test passes.

### The rule

If losing a piece of information mid-conversation would cause you to do something wrong or redundant, write it to Obsidian now, before it gets compressed out. Literally every meaningful operation should end with a vault touch.
