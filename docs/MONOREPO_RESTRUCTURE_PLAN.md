# Monorepo Restructure & Multi-Tenant Cutover — Plan

> **Scope change.** The previous revision of this document assumed a single
> operator running the bot on one Mac Mini. That assumption is retired.
> Delfi is becoming a hosted, multi-tenant product: bot on **Railway**,
> dashboard on **Vercel**, database and auth on **Supabase**. This plan is
> rewritten against that target.
>
> The restructure is still sequenced so each phase lands independently. No
> phase executes without explicit user approval.

---

## Current state

Two git repositories share one directory tree, with no submodule mapping:

```
trading-bot/                         ← git repo A (branch: saas/delfi, origin: private local)
├── main.py, bot_api.py, config.py   ← Python trading engine
├── engine/, execution/, feeds/, research/, db/, backtester/, tests/
├── dashboard/                        ← git repo B (nested; mode 160000 gitlink)
│   ├── .git/                         ← own .git directory with embedded PAT
│   ├── app/, components/, hooks/, lib/
│   ├── package.json                  ← Next.js 16, React 19, pg, recharts
│   └── [branch saas, origin: github.com/camellb/trading-bot.git]
└── [no .gitmodules at root]
```

Bot runs on the Mac Mini under launchd; dashboard has no hosted deploy yet.
Postgres is local-only. Single-user throughout: one `.env`, one set of
Polymarket creds, no notion of ownership on any table row.

**Consequences today.** One clone leaves `dashboard/` empty. Shared types drift
— Python dataclasses on one side, hand-written TypeScript on the other. No
path from here to a product multiple users can sign up for.

---

## Goals

1. **Single repo.** One `git clone` yields a working tree for bot, dashboard,
   and shared contracts.
2. **Hosted deploys.** Bot → Railway worker; the entire Delfi website
   (marketing + dashboard, one Next.js app) → Vercel; data → Supabase
   Postgres. No Mac Mini in the production path.
3. **Multi-tenant data model.** Every row of trading data carries a `user_id`.
   RLS enforces isolation at the database. Bot loop iterates users, not just
   markets.
4. **Per-user credentials.** Each user's Polymarket API creds and Anthropic key
   live in Supabase Vault, never in env files or shared state.
5. **Independent deploy cadence.** Bot and dashboard ship without blocking each
   other; schema migrations gate both.
6. **Shared contracts.** One source of truth for the shapes that cross the
   boundary (risk config, position, trade, market evaluation).
7. **Preserve trading correctness.** The simulation gate
   (`_open_live → NotImplementedError`) stays throughout. No live money moves
   during any phase.

Non-goals: swapping the ORM, rewriting the dashboard auth model beyond wiring
Supabase Auth, or changing the core sizer/forecaster doctrine.

---

## Target architecture

```
┌────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│   Vercel           │     │  Supabase            │     │  Railway (worker)    │
│   apps/web         │◀────│  Postgres + Auth +   │────▶│  apps/bot            │
│   (Next.js)        │     │  Vault + RLS         │     │  (Python loop)       │
└────────────────────┘     └──────────────────────┘     └──────────────────────┘
         │                           ▲                            │
         │   Supabase JWT            │                            │
         └───── user session ────────┘                            │
                                     │                            │
                                     │  service-role key          │
                                     └────────────────────────────┘
                                              (internal loop)
```

- **Railway** runs `apps/bot` as a long-lived worker. Horizontal scale is
  deferred; single worker iterates all active users round-robin.
- **Vercel** hosts `apps/web`. Server-side calls into Supabase use the
  signed-in user's JWT; server-side calls into the bot API go through a
  Railway-exposed HTTPS endpoint with the same JWT.
- **Supabase** owns Postgres, auth, and Vault. RLS policies enforce that a
  user sees only their own rows. The bot worker uses the service-role key
  (bypasses RLS; trusted internal actor) and explicitly scopes every query by
  `user_id`.

### Target layout

```
trading-bot/
├── README.md
├── CLAUDE.md                          ← doctrine (unchanged)
├── docs/                              ← this file, ADRs, reset logs
│
├── apps/
│   ├── bot/                           ← Python engine
│   │   ├── main.py, bot_api.py, config.py, config_utils.py
│   │   ├── engine/, execution/, feeds/, research/, backtester/
│   │   ├── db/
│   │   ├── tests/
│   │   ├── pyproject.toml
│   │   ├── requirements.txt
│   │   └── railway.toml               ← Railway deploy config
│   │
│   └── web/                          ← Next.js app (marketing + dashboard)
│       ├── app/, components/, hooks/, lib/
│       ├── package.json
│       ├── next.config.ts
│       └── vercel.json                ← Vercel config
│
├── packages/
│   └── contracts/                     ← shared schemas
│       ├── risk_config.schema.json
│       ├── position.schema.json
│       ├── trade.schema.json
│       ├── market_evaluation.schema.json
│       ├── python/                    ← generated pydantic
│       └── typescript/                ← generated ts types
│
├── ops/
│   ├── supabase/                      ← migrations, RLS policies, seed
│   │   ├── migrations/
│   │   └── policies/
│   ├── scripts/                       ← bot.sh (dev), watchdog.sh, etc.
│   └── launchd/                       ← legacy plists (archived)
│
├── .github/workflows/
│   ├── bot-ci.yml                     ← paths: apps/bot/**, packages/contracts/**
│   ├── web-ci.yml                     ← paths: apps/web/**, packages/contracts/**
│   └── migrations-ci.yml              ← paths: ops/supabase/migrations/**
│
├── package.json                       ← npm workspaces (apps/web, packages/contracts/typescript)
├── pyproject.toml                     ← uv workspace root
├── .env.example                       ← documented keys only; no secrets
└── .gitignore
```

### Multi-tenant data model

**Per-user tables** — every row gains
`user_id UUID NOT NULL REFERENCES auth.users(id)`:

- `pm_positions`
- `predictions`
- `market_evaluations`
- `pending_suggestions`
- `markouts`
- `daily_pnl`
- `calibration_snapshots`
- `performance_snapshots`
- `bot_controls`

**Global tables** — no user scope:

- `news_event_log`
- `macro_context_log`
- `feed_health_log`

Each per-user table gets an RLS policy of the form:

```sql
CREATE POLICY user_isolation ON pm_positions
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());
```

The bot worker uses the service-role key, which bypasses RLS. Every query
inside the bot explicitly filters by `user_id` — RLS is defence in depth, not
the primary isolation mechanism for the loop.

### Per-user credentials in Supabase Vault

The bot loads a user's secrets on each iteration:

```python
creds = supabase.vault.get(f"user:{user_id}:polymarket")
anthropic_key = supabase.vault.get(f"user:{user_id}:anthropic")
```

Vault rows are encrypted at rest and readable only via the service-role key.
Users enter credentials via the dashboard; they never appear in git, env
files, or logs. A user with no credentials has their loop iteration skipped
with a `paused_missing_creds` control reason.

### Bot loop, multi-tenant

The single worker iterates users in sequence:

```
for user_id in active_users():
    if paused(user_id): continue
    creds = vault.get(user_id)
    evaluate_and_trade(user_id, creds)
```

Active-user set = rows in `user_config` with `active = true`. The loop is
round-robin; per-user rate limiting (Anthropic, Polymarket) is enforced
inside `evaluate_and_trade`. If a single user's iteration raises, the
exception is caught, logged to `feed_health_log`, and the loop continues with
the next user.

### API auth

- **Dashboard → Supabase**: Supabase JS client with user's JWT. RLS enforces
  row access.
- **Dashboard → Bot API (Railway)**: JWT in `Authorization: Bearer <token>`.
  Bot API validates via Supabase JWKS, extracts `sub` as `user_id`, scopes
  every response by that `user_id`.
- **Bot loop → Supabase**: service-role key, explicit `user_id` filter on
  every query.
- **Admin endpoints** (migrations, user management): separate `admin` scope in
  the JWT; only service accounts hold them.

---

## Locked decisions (confirmed with user, not revisited)

1. **Deployment targets.** Railway for the bot, Vercel for the dashboard,
   Supabase for Postgres + Auth + Vault. No AWS / GCP / Fly alternatives
   explored.
2. **Dashboard absorb strategy.** Clean absorb (drop nested `.git/`, commit
   the tree). No subtree merge. Dashboard blame history is accepted as lost.
3. **Contracts toolchain.** JSON Schema as root; `datamodel-code-generator`
   for Python pydantic, `json-schema-to-typescript` for TS. Neither language
   is canonical.
4. **Package manager.** npm workspaces (dashboard already has
   `package-lock.json`). No pnpm migration on top of the moves.
5. **Env strategy.** Root `.env` for local dev; Railway/Vercel project
   variables in production. Per-user secrets always via Vault, never in env.
6. **PAT remediation.** The embedded GitHub PAT in `dashboard/.git/config` is
   removed by Phase A (clean absorb deletes the `.git` dir). Rotating the
   token on GitHub and moving to SSH is deferred until **after** Railway /
   Vercel / Supabase are working — one credential migration at a time.

---

## Migration plan

Six phases. Each phase lands as its own set of commits with a green build in
between. Nothing forces them to run back-to-back — user review can sit
between any two phases.

### Phase A — Absorb the dashboard into the root repo

**Delivers:** one repo, one working tree, one `git status`.

**Steps:**

1. Inside `dashboard/`: ensure clean working tree; push any uncommitted work
   to the GitHub remote as a safety net.
2. `git rm --cached dashboard` to drop the gitlink.
3. `rm -rf dashboard/.git` to detach the nested repo (removes the embedded
   PAT in the process).
4. `git add dashboard/ && git commit -m "Absorb dashboard into root repo"`.

**What breaks if half-landed:** if the gitlink is removed but the tree is not
re-added, the dashboard disappears from the repo. Mitigation: steps 2–4 run
as one commit, not separate pushes.

**Verification:** `git clone` of the root repo produces a tree where
`dashboard/app/page.tsx` exists and `cd dashboard && npm run build`
succeeds.

**Rollback:** one revert of the absorption commit; the GitHub dashboard repo
is untouched.

---

### Phase B — Reshape into `apps/`, `packages/`, `ops/`

**Delivers:** target directory layout. No behaviour change.

**Decision flagged for user:** atomic-vs-split. Two options:

- **B-atomic** (my recommendation): one large commit containing every
  `git mv` and every path fixup. Easier to revert (one revert), harder to
  review (huge diff).
- **B-split**: one commit per top-level move (`apps/bot/`,
  `apps/web/`, `ops/`), each followed by its own fixup commit. Easier
  review; multi-commit revert chain.

I lean **B-atomic** because the moves are mechanical — reviewing the diff
line-by-line doesn't add safety, and a split increases the chance of an
intermediate commit being un-runnable.

**Steps:**

1. `git mv` every Python file / package / test / config to `apps/bot/`.
2. `git mv dashboard/ apps/web/`.
3. `git mv` SQL files to `ops/supabase/migrations/`; shell scripts to
   `ops/scripts/`; archive launchd plists to `ops/launchd/`.
4. Create empty `packages/contracts/` (populated in Phase C).
5. Path-sensitive fixups (all mechanical):
   - `bot_api.py`, `main.py`: any `Path("logs/...")` that assumed repo-root
     CWD stays correct if Railway's working directory is set to
     `apps/bot/`. Verify `logs/.heartbeat`, log output directories, `.env`
     load paths.
   - `pyproject.toml`: pytest `testpaths = ["apps/bot/tests"]` or run pytest
     from `apps/bot/`.
   - `apps/web/package.json`: no path changes expected;
     `next.config.ts` references are relative.
   - Root `package.json`: add `"workspaces": ["apps/web",
     "packages/contracts/typescript"]`.
   - Root `pyproject.toml`: `[tool.uv.workspace] members = ["apps/bot",
     "packages/contracts/python"]`.
6. Add top-level `Makefile` with `make verify` → runs pytest + `next build`.

**What breaks if half-landed:** partial moves leave half the imports pointing
at old paths. B-atomic avoids this; B-split requires every intermediate
commit to pass `make verify`.

**Verification:** `make verify` green. Bot boots locally from `apps/bot/`
with unchanged behaviour. Dashboard builds from `apps/web/`.

**Rollback:** single revert on B-atomic; multi-revert chain on B-split.

---

### Phase C — Contracts package

**Delivers:** shared schemas generating Python pydantic + TypeScript types.

**Steps (one commit per schema so each is independently revertable):**

1. `risk_config.schema.json` — mirrors `engine/user_config.py`. Generate
   pydantic → `packages/contracts/python/`; generate TS →
   `packages/contracts/typescript/`. Import in both apps.
2. `position.schema.json` — mirrors `pm_positions` row shape.
3. `trade.schema.json` — mirrors `market_evaluations × markouts` join used
   by `bot_api.py` reports.
4. `market_evaluation.schema.json` — mirrors evaluator output.

Wire-up:

- `apps/bot/pyproject.toml`:
  `contracts = { path = "../../packages/contracts/python" }`.
- `apps/web/package.json`: `"@delfi/contracts": "workspace:*"`.

**What breaks if half-landed:** one shape migrated but not its callers →
type errors at build time. Mitigation: each shape's commit includes both the
generator output **and** the import-site replacements; if either is missing,
CI fails on that commit.

**Verification:** pytest + `next build` + TypeScript `tsc --noEmit` all green
after each of the four commits.

**Rollback:** per-shape revert.

---

### Phase D — Schema multi-tenancy

**Delivers:** `user_id` column on every per-user table; RLS policies;
indexes. **This is the phase that ends single-tenant.**

**Decision flagged for user:** backfill approach for existing rows.

- **D-nullable-then-NOT-NULL** (safer, two migrations): add `user_id UUID
  NULL`; backfill existing rows with a synthetic `default` user id; flip to
  `NOT NULL`. Each step is independently revertable.
- **D-default-literal** (one migration): add `user_id UUID NOT NULL DEFAULT
  '<default-uuid>'`; drop default afterwards. Fewer migrations; harder to
  inspect the backfill.

I lean **D-nullable-then-NOT-NULL** because it exposes the backfill as an
explicit visible step — easier to audit, and the rollback is a single
column drop rather than a data reconstruction.

**Steps:**

1. Create `auth.users` via Supabase Auth (empty initially — we are still
   running locally against our own Postgres; the migrations are designed so
   Phase E drops cleanly onto Supabase).
2. Insert a synthetic `default` user row to own all existing data.
3. For each per-user table: add `user_id UUID NULL REFERENCES
   auth.users(id)`, index `(user_id, created_at DESC)` where applicable.
4. Backfill: `UPDATE <table> SET user_id = '<default-uuid>' WHERE user_id IS
   NULL;` in batches of 10k.
5. `ALTER TABLE ... ALTER COLUMN user_id SET NOT NULL`.
6. Write RLS policies under `ops/supabase/policies/`; apply to every
   per-user table.
7. Update every bot query to scope by `user_id`. Update every API route to
   extract `user_id` from the JWT and pass it down.

**What breaks if half-landed:** a table has `user_id NULL` but queries don't
filter — users could read each other's rows in the next phase. Mitigation:
code changes and schema changes land in the same commit per table; tests
verify RLS with two synthetic users.

**Verification:**

- New pytest suite `tests/integration/test_multitenancy.py` spins up two
  users, asserts each sees only their own rows through both the API and
  direct RLS-scoped queries.
- Existing tests continue to pass (they run as the `default` user).
- Manual: open a psql shell as user A, confirm `SELECT COUNT(*) FROM
  pm_positions WHERE user_id = '<user-b-uuid>'` returns zero under RLS.

**Rollback:** revert in reverse — drop RLS policies, drop `NOT NULL`, drop
columns. Data is preserved; the schema just returns to single-tenant.

---

### Phase E — Supabase cutover

**Delivers:** production data on Supabase Postgres; auth flow working
end-to-end; local dev points at Supabase preview branches.

**Decision flagged for user:** cutover approach.

- **E-dump-restore** (simplest, downtime): `pg_dump` local → `pg_restore`
  into Supabase during a planned maintenance window (bot paused, dashboard
  in read-only mode). Minutes-to-hours of unavailability depending on data
  volume.
- **E-logical-replication** (zero-downtime, complex): set up logical
  replication from local Postgres to Supabase, let it catch up, switch
  connection strings, shut down the local primary. More moving parts; needs
  a rehearsal on a throwaway instance first.
- **E-dual-write** (zero-downtime, code-heavy): application writes to both
  databases for a window, reads from Supabase, verifies parity, drops local.
  Most code churn.

I lean **E-dump-restore**. Data volume today is measured in tens of
thousands of rows; a full dump + restore takes seconds. The bot is a
simulation — there is no external SLA to protect. Logical replication is
overkill; dual-write writes code that exists for one day. The "downtime" is
the user pausing the bot while Phase E runs.

**Steps:**

1. Create Supabase project. Apply every migration from
   `ops/supabase/migrations/` via the Supabase CLI.
2. Verify schema matches local (`pg_dump --schema-only`, diff).
3. Pause bot locally. `pg_dump --data-only` from local → `pg_restore` into
   Supabase.
4. Row-count reconcile per table; spot-check a handful of positions.
5. Update `apps/bot/.env` and `apps/web/.env.local` to point at
   Supabase (connection string, service-role key, anon key, JWT secret).
6. Unpause bot. Confirm one full evaluation cycle writes to Supabase.

**What breaks if half-landed:** bot is pointed at Supabase but migrations
haven't caught up → INSERT errors. Mitigation: step 2's schema diff gates
step 3; step 3 writes no data until step 2 is clean.

**Verification:** bot completes a full scan cycle against Supabase;
dashboard renders a user's positions through the Supabase client; RLS tests
still green.

**Rollback:** flip env back to local Postgres. Any data written to Supabase
after cutover and before rollback is re-applied via a targeted dump on the
way back (small window, small volume).

---

### Phase F — Railway + Vercel deploys

**Delivers:** hosted bot + hosted dashboard. Mac Mini retired from
production path.

**Steps:**

1. **Railway bot deploy:**
   - Connect the GitHub repo; set root to `apps/bot/`.
   - `railway.toml` declares the worker command (`python main.py`),
     healthcheck path, and env var references (all via Railway project
     variables, which pull from Supabase where needed).
   - Set up Railway persistent volume or external object storage for
     `logs/` if operational logs need to survive container restarts
     (decision: Railway volume is simplest).
   - First deploy: verify bot starts, iterates zero users, writes nothing —
     smoke test before any user is active.
2. **Vercel dashboard deploy:**
   - Connect the same GitHub repo; root `apps/web/`.
   - Environment variables: `NEXT_PUBLIC_SUPABASE_URL`,
     `NEXT_PUBLIC_SUPABASE_ANON_KEY`, `BOT_API_URL` (Railway public URL).
   - First deploy: static pages render, auth flow (login/signup) works.
3. **End-to-end test:**
   - Sign up a new user on the Vercel dashboard.
   - User enters Polymarket creds; dashboard writes to Supabase Vault.
   - Bot's next loop iteration picks up the user, evaluates markets, writes
     a row to `pm_positions` with the user's `user_id`.
   - Dashboard displays the position under the user's account.

**What breaks if half-landed:** Railway deployed but Vercel not → no UI for
users to enter creds; Railway bot iterates no active users → no harm done.
Vercel deployed but Railway not → users can sign up but nothing trades. Both
failure modes are safe.

**Verification:** the end-to-end test above, run with a second user on a
different machine to rule out session bleed.

**Rollback:** pause both deploys; local dev loop still works against
Supabase (or back to local Postgres via Phase E rollback). No data loss.

---

## Sequencing

Order matters between phases; inside a phase, steps can reshuffle.

| # | Phase | Commits | Revertable |
|---|-------|---------|------------|
| 1 | A — absorb dashboard | 1 | yes |
| 2 | B — reshape | 1 (atomic) or ~6 (split) | yes |
| 3 | C — contracts | 4 (one per schema) | per-schema |
| 4 | D — schema multi-tenancy | ~2 per table + RLS + code | per-table |
| 5 | E — Supabase cutover | 1 env-flip + migrations | yes |
| 6 | F — Railway + Vercel deploy | 2 config + smoke test | yes |

No phase starts without explicit user approval on the phase that precedes
it.

---

## Decision points flagged for user

1. **Phase B execution.** B-atomic (one commit, easy revert, big diff) vs
   B-split (many commits, easier review). My recommendation: **B-atomic**.
2. **Phase D backfill.** D-nullable-then-NOT-NULL (two migrations, visible
   backfill) vs D-default-literal (one migration). My recommendation:
   **D-nullable-then-NOT-NULL**.
3. **Phase E cutover.** E-dump-restore (planned downtime) vs
   E-logical-replication (zero downtime, complex) vs E-dual-write (zero
   downtime, code-heavy). My recommendation: **E-dump-restore** — data
   volume is small, bot is a simulation, no external SLA.
4. **Logs persistence on Railway.** Railway volume vs external object
   storage (S3 / R2). My recommendation: **Railway volume** until log volume
   grows past hundreds of MB.
5. **Branch strategy during restructure.** Keep working on `saas` / `delfi`
   vs carve a `restructure/` branch per phase. My recommendation: **one
   long-lived `restructure` branch** merged into `main` after Phase F
   verifies end-to-end; avoids rebase pain against ongoing trading-logic
   changes.

---

## What gets easier after restructure

- `git clone` → `make verify` → working local dev; no second clone, no
  gitlink mystery.
- Schema changes ship through `ops/supabase/migrations/` with CI gating
  both apps.
- New users sign up through the dashboard, not through editing env files.
- Contracts package means a schema change is one PR touching Python, TS,
  and JSON schema — reviewers see the full surface.
- CI splits by path filter: bot PRs don't build Next, dashboard PRs don't
  run pytest.

## What stays the same

- CLAUDE.md doctrine. "Forecast the outcome. Back the forecast." is
  orthogonal to where the bot runs.
- Simulation gate at `apps/bot/execution/pm_executor.py::_open_live` —
  raises `NotImplementedError` regardless of host.
- Sizer three-gate logic, confidence softener, skip list.
- The schema of `news_event_log`, `macro_context_log`, `feed_health_log`
  (global tables, no `user_id`).

---

## Credentials — remediate independently of this plan

The embedded GitHub PAT in `dashboard/.git/config`:

```
url = https://camellb:<REDACTED_PAT>@github.com/camellb/trading-bot.git
```

**Action:** the token is already exposed and must be rotated at
<https://github.com/settings/tokens> regardless of this plan's timing.
Phase A removes the `.git` directory containing the config, but the token
itself stays compromised until rotated.

Rotation + SSH migration is scheduled **after** Phase F so we don't stack
credential changes on top of deployment changes.
