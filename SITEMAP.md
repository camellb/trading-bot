# Delfi repo sitemap

A map of what's used vs what's legacy after the local-first pivot
(2026-04-25). Use this when deciding what to delete.

The repo is post-pivot but the pre-pivot SaaS code (Next.js dashboard,
Railway-hosted Python bot, Supabase schema) is still on disk. The
active product is `Delfibot/` — a Tauri desktop app shipping a Python
sidecar. Everything in `apps/*`, `ops/*`, and `packages/contracts/*`
is legacy SaaS scaffolding.

---

## TL;DR — what to keep, what to delete

| Path | Status | Size | Decision |
|---|---|---|---|
| `Delfibot/bot/` | **ACTIVE** | ~250 MB source + 1.7 GB build artifacts | KEEP |
| `Delfibot/install.sh`, `dock-clean.sh` | ACTIVE | <100 KB | KEEP |
| `Delfibot/docs/`, `Delfibot/README.md` | ACTIVE | small | KEEP |
| `Delfibot/scripts/owner-activate.py` | ACTIVE-ish | small | KEEP for now |
| `Delfibot/research/skip_audit`, `wallet_spike` | ad-hoc research | small | KEEP for now |
| `.github/workflows/build.yml` | ACTIVE | small | KEEP |
| `CLAUDE.md` | doctrine, partially stale | small | KEEP, update later |
| `LAUNCH_CHECKLIST.md` | needs review | small | review then keep or move under Delfibot/docs/ |
| **`apps/web/`** | **LEGACY SaaS Next.js dashboard** | 612 MB | **DELETE** |
| **`apps/bot/`** | **LEGACY SaaS Railway Python bot** | small (mostly diverged code) | **DELETE** |
| **`ops/supabase/`** | **LEGACY Supabase migrations** | small | **DELETE** (after preserving migration 022 history note in CLAUDE.md if relevant) |
| **`ops/scripts/bot.sh`** | **LEGACY launchd wrapper for old paths** | small | **DELETE** |
| **`ops/reset-2026-04-23.sql`** | **LEGACY Supabase reset script** | small | **DELETE** |
| **`packages/contracts/`** | **LEGACY shared schemas, never used by Delfibot** | small | **DELETE** |
| **Root `package.json` (workspaces apps/web + packages/contracts/typescript)** | LEGACY | tiny | **REWRITE** (drop workspaces, root won't build anything) |
| **Root `Makefile` (drives apps/bot + apps/web)** | LEGACY | tiny | **DELETE** or rewrite for Delfibot |
| `trading_bot_prd_v1_2.docx` | original PRD doc | 26 KB | KEEP for archive, or move to `docs/` |
| `.env.template` | LEGACY (env vars for SaaS) | <1 KB | DELETE after verifying nothing in Delfibot reads it |

Net reclaimable disk: roughly 600 MB once `apps/web/node_modules` +
`apps/web/.next` are gone (the actual source is small).

---

## ACTIVE: `Delfibot/` (the local-first desktop app)

The whole product lives here. It's a Tauri 2 shell wrapping a
PyInstaller-bundled Python sidecar. The user installs it as a `.app`
on macOS (or `.exe`/`.msi` on Windows).

### `Delfibot/bot/` — Python sidecar + React frontend

```
Delfibot/bot/
├── main.py                       # daemon entrypoint (APScheduler + aiohttp server)
├── polymarket_runner.py          # scheduled scan/resolve worker
├── local_api.py                  # aiohttp HTTP server (replaces SaaS bot_api.py)
├── calibration.py                # Brier-score tracker
├── config.py / config_utils.py   # process-level constants + helpers
├── process_health.py             # /api/health snapshot + job-ok tracker
├── pyproject.toml                # Python deps
│
├── engine/
│   ├── pm_analyst.py             # per-market analyzer; calls evaluator + sizer
│   ├── polymarket_evaluator.py   # LLM forecaster (Anthropic + Gemini fallback)
│   ├── user_config.py            # config dataclass + secrets.json
│   ├── archetype_classifier.py   # flat archetype labels (sport, crypto, etc.)
│   ├── learning_cadence.py       # 50-trade review cycle
│   ├── review_report.py          # composes the per-cycle thesis
│   ├── risk_manager.py           # circuit breakers + size limits
│   ├── llm_client.py             # provider-agnostic Anthropic/Gemini glue
│   ├── markout_tracker.py        # post-trade price movement audit
│   ├── loop_watchdog.py          # self-probe; SIGKILLs on wedge
│   ├── live_crypto.py            # ccxt-backed live BTC/ETH/SOL prices
│   ├── live_equity.py            # yfinance-backed live S&P/NASDAQ
│   ├── position_exit.py          # exit-policy rules (TP/SL/time-decay)
│   ├── license.py                # Ed25519 license verification
│   └── ...
│
├── execution/
│   ├── pm_executor.py            # opens/closes Polymarket positions via py-clob-client-v2
│   ├── pm_sizer.py               # stake-pct, archetype mult, direction-agreement gate
│   └── pm_redeemer.py            # gasless redeem + USDC.e -> pUSD activator
│
├── feeds/
│   ├── polymarket_feed.py        # gamma + CLOB market fetch
│   ├── polymarket_wallet.py      # signer/balance probe (pUSD + USDC.e + USDC)
│   ├── news_feed.py              # RSS + NewsAPI ingestion
│   ├── macro_calendar.py         # Trading Economics scraper
│   ├── telegram_notifier.py      # Telegram bot (optional; user-configurable)
│   └── telegram_messages.py      # Telegram message templates
│
├── research/
│   ├── fetcher.py                # DuckDuckGo search + trafilatura article extract
│   └── ...
│
├── db/
│   ├── models.py                 # SQLAlchemy schema (SQLite)
│   ├── engine.py                 # connection + migration on boot
│   └── logger.py                 # event_log writes
│
├── src/                          # React 19 + Vite frontend
│   ├── App.tsx                   # router, boot screen, connection banner
│   ├── api.ts                    # typed HTTP client to local_api
│   ├── Onboarding.tsx
│   ├── pages/
│   │   ├── Dashboard.tsx
│   │   ├── Positions.tsx
│   │   ├── Performance.tsx
│   │   ├── Intelligence.tsx
│   │   ├── Risk.tsx
│   │   ├── Settings.tsx
│   │   └── ...
│   ├── components/
│   └── styles.css
│
├── src-tauri/                    # Rust shell that wraps + ships the binary
│   ├── src/main.rs               # Tauri entry: spawns sidecar, exposes commands
│   ├── tauri.conf.json           # Tauri bundle config (icon, identifier, etc.)
│   ├── com.delfi.bot.plist.template  # macOS LaunchAgent template
│   ├── binaries/delfi-sidecar-<triple>  # PyInstaller output (gitignored)
│   └── target/                   # Rust build cache (gitignored, ~1.3 GB)
│
├── scripts/
│   ├── build_sidecar.sh          # PyInstaller wrapper
│   ├── delfi_sidecar.spec        # PyInstaller spec
│   └── smoke_*.py                # CI smoke tests
│
├── tools/                        # one-off devtools
├── public/                       # static frontend assets
├── index.html, vite.config.ts    # Vite frontend config
├── package.json                  # frontend deps (Vite, React, Tauri CLI)
├── .pyinstaller-dist/            # gitignored build artifact (~161 MB)
├── build/                        # gitignored build artifact (~233 MB)
├── dist/                         # gitignored Vite output (~420 KB)
└── node_modules/                 # gitignored (~82 MB)
```

### `Delfibot/install.sh`

Installer script. Stops the running daemon, rsyncs the Tauri bundle into
`/Applications/Delfi.app`, wraps the sidecar in a UI-element-only
sub-bundle, dedupes Dock entries, registers the LaunchAgent for 24/7
operation. **Active.**

### `Delfibot/dock-clean.sh`

Standalone Dock dedupe utility. **Active, occasionally useful.**

### `Delfibot/docs/`

Just contains `lemonsqueezy-email-template.md` (post-purchase email
copy). Keep.

### `Delfibot/scripts/owner-activate.py`

Manual license-activation helper. Keep.

### `Delfibot/research/`

Two folders (`skip_audit/`, `wallet_spike/`) with ad-hoc analysis
scripts. Keep for archive — nothing critical.

---

## LEGACY: `apps/` — SaaS Next.js dashboard + Railway bot (DELETE)

Pre-pivot scaffolding. Local-first means there's no website and no
server-side bot anymore — everything runs in the user's installed
Delfi.app. Nothing in `Delfibot/` references anything under `apps/`
except a few comment-only mentions in `engine/license.py` and
`engine/review_report.py` (those are stale doc refs that should be
edited out when the parent dir is deleted).

### `apps/web/` — Next.js 16 + Supabase SSR dashboard

```
apps/web/
├── app/                      # Next.js App Router
│   ├── page.tsx              # landing page
│   ├── login/                # email/password + Google OAuth
│   ├── auth/                 # OAuth callback
│   ├── onboarding/           # SaaS onboarding flow
│   ├── subscribe/            # Stripe subscribe
│   ├── checkout/             # Stripe checkout return
│   ├── dashboard/            # SaaS dashboard
│   │   ├── page.tsx
│   │   ├── positions/
│   │   ├── performance/
│   │   ├── intelligence/
│   │   ├── risk/
│   │   ├── support/
│   │   └── settings/{account,connections,notifications,risk}/
│   ├── admin/                # admin console
│   │   ├── users/
│   │   ├── trades/
│   │   ├── forecaster/
│   │   ├── learning/
│   │   ├── scanner/
│   │   ├── geoblock/
│   │   └── audit-log/
│   ├── api/                  # Next.js API routes (bot proxy + auth glue)
│   ├── geoblocked/, legal/, components/, styles/
├── lib/                      # Supabase SSR client, bot-proxy.ts, license.ts
├── proxy.ts                  # Next 16 proxy (replaces middleware.ts)
├── public/, scripts/
├── next.config.ts, tsconfig.json, postcss.config.mjs, vercel.json
└── package.json
```

**Size:** 612 MB total (481 MB node_modules + 129 MB .next build).

**Decision: DELETE** — replaced by `Delfibot/bot/src/` (React inside Tauri).

### `apps/bot/` — Railway-hosted Python bot

```
apps/bot/
├── main.py                   # SaaS entrypoint (multi-tenant; APScheduler)
├── bot_api.py                # aiohttp server exposed to Vercel via bot-proxy
├── polymarket_runner.py
├── calibration.py, config.py, process_health.py
├── engine/                   # diverged copy of Delfibot/bot/engine/
├── execution/                # diverged copy of Delfibot/bot/execution/
├── feeds/                    # diverged copy
├── research/                 # diverged copy
├── db/                       # SQLAlchemy models + Postgres
├── backtester/               # historical replay
├── tests/                    # pytest
├── requirements.txt          # Railway deps
└── railway.toml              # Railway deploy config
```

**Decision: DELETE.** Today's evaluator/sizer fixes live in
`Delfibot/bot/`, not here. The diverged copies are stale.

---

## LEGACY: `ops/` — Supabase + launchd ops (DELETE)

```
ops/
├── supabase/
│   ├── migrations/           # 27 SQL files (001..026 + others)
│   └── reset-2026-04-23.sql  # reset script
└── scripts/bot.sh            # launchd wrapper, refers to ~/Desktop/trading-bot/logs/ (pre-Delfibot paths)
```

**Decision: DELETE.** Migration history is preserved in git anyway.
A single Delfibot code comment references `migration 022` — that's a
doc breadcrumb, not a live dependency.

---

## LEGACY: `packages/contracts/` (DELETE)

```
packages/contracts/
├── README.md
├── schemas/                  # JSON schemas (market_evaluation, position, etc.)
└── typescript/               # (workspace member of root package.json)
```

Not referenced anywhere in `Delfibot/bot/`. **Decision: DELETE.**

---

## ROOT CONFIG: needs cleanup

| File | Purpose | Action |
|---|---|---|
| `package.json` | npm workspaces for apps/web + packages/contracts/typescript | **REWRITE** — drop workspaces; root no longer builds anything. Or delete entirely. |
| `Makefile` | targets for apps/bot (pytest) + apps/web (next build) | **DELETE** — `make verify` etc. no longer apply |
| `.env.template` | Supabase + Stripe + Telegram env vars | **DELETE** — Delfibot stores secrets in `<app-data>/data/secrets.json` |
| `package-lock.json` | lockfile for the root workspace | **DELETE** after rewriting `package.json` |
| `pyproject.toml` | top-level Python project metadata | review — Delfibot has its own at `Delfibot/bot/pyproject.toml` |
| `node_modules/` | root workspace node_modules | **DELETE** after dropping workspaces |
| `.gitignore` | covers .next, target, etc. | KEEP but maybe slim after deletions |
| `CLAUDE.md` | project doctrine — mixed local-first + SaaS references | **UPDATE** — strip SaaS sections, point at Delfibot/ |
| `LAUNCH_CHECKLIST.md` | review for currency | review |
| `trading_bot_prd_v1_2.docx` | original PRD | move to `docs/` for archive |
| `.github/workflows/build.yml` | builds Delfibot (Tauri + PyInstaller) | KEEP |

---

## Routes you specifically asked about

- **`/dashboard/*`** → `apps/web/app/dashboard/`. All gone in local-first; the dashboard is now `Delfibot/bot/src/pages/Dashboard.tsx`. **DELETE.**
- **`/auth/*`** → `apps/web/app/auth/`. There IS no auth in local-first (license key only). **DELETE.**
- **`/admin/*`** → SaaS admin console, never used by users. **DELETE.**
- **`/onboarding`** → SaaS onboarding (Stripe + account creation). The local-first onboarding lives in `Delfibot/bot/src/Onboarding.tsx`. **DELETE.**
- **`/subscribe`, `/checkout`** → Stripe subscription. Replaced by Lemon Squeezy one-time purchase + Ed25519 license; license verification lives in `Delfibot/bot/engine/license.py`. **DELETE.**
- **`/legal`, `/geoblocked`, `/login`** → SaaS pages. **DELETE.**

---

## Suggested cleanup order

1. `rm -rf apps/` (reclaims most of the 600 MB)
2. `rm -rf ops/`
3. `rm -rf packages/`
4. `rm Makefile .env.template`
5. Rewrite or delete root `package.json` + `package-lock.json` + root `node_modules/`
6. Strip SaaS sections from `CLAUDE.md`
7. (Optional) Update `Delfibot/bot/engine/license.py` + `review_report.py` comment refs that point at `apps/web/lib/license.ts` and `/dashboard/performance`

I'd recommend doing each step in a separate git commit so a single
`git revert` can restore any piece you want back.

---

## Notes / caveats

- **Telegram code is still active** in `Delfibot/bot/feeds/telegram_notifier.py` and `telegram_messages.py`. It's optional (user-configurable). Don't delete it.
- **Migration 022 reference:** `engine/archetype_classifier.py` mentions `ops/supabase/migrations/022_archetype_consolidation.sql` in a comment. After deletion, update the comment to point at the historical commit instead.
- **Active license verification key:** `engine/license.py` carries the public Ed25519 key. The matching private key + key-generation script lived under `apps/web/scripts/`. Move that to `Delfibot/scripts/` before deleting `apps/`.
- **`.github/workflows/build.yml`** builds Delfibot only; safe to keep as-is after the cleanup.
