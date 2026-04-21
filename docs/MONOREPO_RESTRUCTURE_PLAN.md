# Monorepo Restructure — Plan

## Current state

Two git repositories share a directory tree, with no submodule mapping:

```
trading-bot/                         ← git repo A (branch: saas, origin: private local)
├── main.py, bot_api.py, config.py   ← Python trading engine
├── engine/, execution/, feeds/, research/, db/, backtester/, tests/
├── dashboard/                        ← git repo B (nested; mode 160000 gitlink)
│   ├── .git/                         ← own .git directory
│   ├── app/, components/, hooks/, lib/
│   ├── package.json                  ← Next.js 16, React 19, pg, recharts
│   └── [branch saas, origin: github.com/camellb/trading-bot.git]
└── [no .gitmodules at root]          ← "orphan gitlink" — `git submodule update --init` fails
```

**Consequences today.**
- Clone of the root repo leaves `dashboard/` empty and broken until you also clone the dashboard repo by hand.
- Two separate PR flows, two separate CI configs (neither exists yet, but will differ when added).
- Shared types (risk config shape, position records, trade schema) drift — Python dataclasses on one side, hand-written TypeScript interfaces on the other.
- Nested `.git/config` in dashboard has a hardcoded GitHub PAT in the remote URL (remediate separately — see *Credentials*).

## Goals

1. **Single clone, single `git status`.** One repo, both trees, one history.
2. **Independent deploy cadence.** Backend (launchd on the Mac, eventually a server) and frontend (Vercel) ship without blocking each other.
3. **Shared contracts.** One source of truth for the types that cross the boundary — risk config, position, trade, market evaluation.
4. **Per-tree test isolation.** Backend tests don't need Node; frontend tests don't need a Postgres connection.
5. **No forced rewrites of either git history** — preserve blame.

Non-goals for this restructure: changing deploy target, swapping ORM, rewriting the dashboard's auth model.

## Target layout

```
trading-bot/
├── README.md
├── CLAUDE.md                          ← project doctrine (keep as-is)
├── docs/                              ← this file, reset logs, future ADRs
│
├── apps/
│   ├── bot/                           ← Python engine (moved from repo root)
│   │   ├── main.py, bot_api.py, config.py, config_utils.py
│   │   ├── engine/, execution/, feeds/, research/, backtester/
│   │   ├── db/
│   │   ├── tests/
│   │   ├── pyproject.toml
│   │   └── requirements.txt
│   │
│   └── dashboard/                     ← Next.js app (moved from dashboard/)
│       ├── app/, components/, hooks/, lib/
│       ├── package.json
│       └── next.config.ts
│
├── packages/
│   └── contracts/                     ← shared schemas (NEW)
│       ├── risk_config.schema.json    ← single source of truth
│       ├── position.schema.json
│       ├── trade.schema.json
│       ├── python/                    ← generated pydantic models
│       └── typescript/                ← generated ts types
│
├── ops/
│   ├── launchd/                       ← com.tradingbot*.plist (moved from ~/Library)
│   ├── sql/                           ← migrations, schema snapshots
│   └── scripts/                       ← bot.sh, watchdog.sh, operational tooling
│
├── .github/workflows/
│   ├── bot-ci.yml                     ← paths: apps/bot/**, packages/contracts/**
│   └── dashboard-ci.yml               ← paths: apps/dashboard/**, packages/contracts/**
│
├── package.json                       ← workspaces: apps/dashboard, packages/contracts/typescript
├── pyproject.toml                     ← uv workspace: apps/bot
└── .gitignore
```

Why this shape:
- `apps/` is the shipping code. Flat, two entries, obvious.
- `packages/` is anything cross-cutting. One entry today (`contracts`), more if we extract a brand-neutral PM client or a shared telemetry helper later.
- `ops/` gets infra out of the root. Launchd plists, SQL, scripts — things that aren't application code.
- `docs/` is unchanged.

## Migration plan

Three-phase migration, each phase lands as its own commit with a green build in between.

### Phase A — Ingest the dashboard into the root repo (no code moves yet)

1. Inside `dashboard/`: ensure clean working tree, push any uncommitted work to its remote.
2. From root: `git rm --cached dashboard` to drop the gitlink (no files deleted).
3. `rm -rf dashboard/.git` — detach the nested repo.
4. `git add dashboard/ && git commit -m "Absorb dashboard into root repo"` — now one repo, one tree.
5. Optional but recommended: preserve dashboard history via `git subtree add --prefix=dashboard/ <dashboard-remote> saas --squash` before step 2 → this keeps blame for dashboard files pointing at the original commits. Decision point: if blame preservation matters, do the subtree merge; if not (fewer than ~100 meaningful commits on the dashboard side), a clean absorb is simpler.

**Risk:** credentials in the dashboard's `.git/config` go away with the `.git` directory (good), but anything committed into the dashboard repo's history that was secret stays in the subtree if we merge. Audit with `git log -p` on the dashboard side before Phase A if the subtree route is chosen.

**Backout:** one revert of the absorption commit; the dashboard repo on GitHub is untouched.

### Phase B — Reshape into `apps/`, `packages/`, `ops/`

Do this as one atomic commit (many file moves, no content changes). Use `git mv` so blame follows.

1. `git mv <py files at root> apps/bot/` — every Python file, every Python package directory, `tests/`, `requirements.txt`, `pyproject.toml`, `bot.sh`, `watchdog.sh.quarantined`.
2. `git mv dashboard/ apps/dashboard/`.
3. Create empty `packages/contracts/` — populated in Phase C.
4. `mkdir ops/launchd ops/sql ops/scripts`; move `bot.sh`, `watchdog.sh.quarantined` under `ops/scripts/`; move SQL migration files under `ops/sql/`; copy (not move) plist files from `~/Library/LaunchAgents/com.tradingbot*.plist` into `ops/launchd/`.
5. **Path-sensitive fixups** (all mechanical):
   - launchd plists: the `WorkingDirectory` entries currently point at `/Users/macmini/Desktop/trading-bot`. Update the bot plist to `/Users/macmini/Desktop/trading-bot/apps/bot`; update watchdog paths similarly.
   - `bot_api.py` — any relative path that assumes CWD is repo root needs review (search for `Path(".")`, `open("logs/`, `logs/.heartbeat`).
   - `main.py` — `Path("logs/.heartbeat")` at `_heartbeat_writer` assumes CWD is the bot directory. With new WorkingDirectory this stays correct.
   - Dashboard `package.json` — no path changes needed (next.config.ts references are relative inside `apps/dashboard/`).
   - `.env` loading — confirm the bot reads `.env` from its own CWD, not from repo root; if it expects repo root, either move `.env` into `apps/bot/` or update the loader.
   - pytest — add `pyproject.toml` testpaths or run from `apps/bot/`.
   - Dashboard DB connection — `apps/dashboard/lib/*` may hardcode postgres host/port; no path dependency, safe.

**Risk:** anything referencing files by absolute path or by assuming CWD = repo root. Mitigation: grep for `"/Users/macmini"`, `"logs/"`, `"db_backup"`, `os.path`, `Path(` before committing. The Phase B commit should include a top-level `Makefile` target `make verify` that runs pytest and `next build` to catch regressions.

**Backout:** single revert; all moves are `git mv` so revert cleanly reverses them.

### Phase C — Contracts package

1. Author JSON Schema files for the four shared shapes:
   - `risk_config.schema.json` — mirrors `engine/user_config.py` (all fields currently read/written from the dashboard's risk page)
   - `position.schema.json` — mirrors `db/models.py` `pm_positions` row
   - `trade.schema.json` — mirrors `market_evaluations` + `markouts` joined shape used by `bot_api.py`
   - `market_evaluation.schema.json`
2. Generate Python pydantic models → `packages/contracts/python/`; import into `engine/user_config.py` and replace the hand-written dataclass with a generated one (decision point: if the generated model loses ergonomic defaults, wrap it rather than replace).
3. Generate TypeScript types → `packages/contracts/typescript/`; replace `apps/dashboard/lib/types.ts` (or equivalent).
4. Wire both into their package manager:
   - `apps/bot/pyproject.toml` — add `contracts = { path = "../../packages/contracts/python" }`.
   - root `package.json` — declare npm workspace `"packages/contracts/typescript"` and reference it in `apps/dashboard/package.json` as `"@delfi/contracts": "workspace:*"`.

**Risk:** generated-model mismatches with existing runtime shapes. Mitigation: Phase C is the only phase where tests could regress silently — run both the pytest suite and the dashboard's TypeScript compile as the gate, and land this as multiple smaller commits per shape rather than one big switch.

**Backout:** each shape is independently revertable.

## What gets easier

- `git clone` produces a working tree.
- One PR can touch bot + dashboard + contracts together when a schema changes; reviewer sees the full change.
- CI splits by path filter — dashboard PRs don't run the python suite, bot PRs don't build Next.
- Launchd plist source is version-controlled next to the code it runs; no drift between `~/Library/LaunchAgents/*.plist` and what's in the repo.
- Onboarding doc can say "clone, run `make verify`" instead of the current "clone, then clone again into `dashboard/`, then hope the gitlink matches."

## What stays the same

- Deploy targets: bot on launchd (mac mini), dashboard on Vercel. Monorepo doesn't change either.
- Database: same Postgres instance, same schemas. Contracts package describes the shapes, doesn't own the data.
- CLAUDE.md doctrine is unchanged — this restructure moves files, not rules.
- Simulation-only gate at `apps/bot/execution/pm_executor.py` (`_open_live` raises NotImplementedError) is preserved.

## Sequencing

Order matters only between phases. Inside a phase, steps can reshuffle.

| Order | Phase | Commits | Revertable |
|-------|-------|---------|------------|
| 1     | A — absorb dashboard | 1 (or 2 with subtree) | yes |
| 2     | B — reshape | 1 (atomic mv) + fixups | yes |
| 3     | C — contracts | 4 (one per schema) | per-schema |

Phases can sit between user reviews. Nothing forces them to run back-to-back.

## Credentials — must remediate regardless of restructure

While inspecting the dashboard git directory, a GitHub Personal Access Token was found embedded in `dashboard/.git/config` as part of the remote URL:

```
url = https://camellb:<REDACTED_PAT>@github.com/camellb/trading-bot.git
```

**Action needed (independent of this plan):** revoke the PAT at https://github.com/settings/tokens, then reconfigure the dashboard remote to use SSH or a credential helper (`git remote set-url origin git@github.com:camellb/trading-bot.git`). The restructure's Phase A removes the `.git` directory containing this config, but the token is already exposed and must be rotated.

## Decision points flagged for user

1. **Phase A blame preservation.** Subtree merge vs. clean absorb. Default: clean absorb (simpler, loses dashboard commit blame). Flag if blame matters.
2. **Contracts generation toolchain.** `datamodel-code-generator` for Python, `json-schema-to-typescript` for TS. Alternatives exist (Zod-first, Pydantic-first). Default: JSON Schema as the root source because both sides are generated, neither is canonical.
3. **Npm workspace vs. pnpm workspace.** Dashboard currently uses `npm` (has `package-lock.json`). Default: keep npm workspaces to avoid a package-manager migration on top of the file moves.
4. **`.env` location post-move.** Either move `.env` into `apps/bot/` and symlink to `apps/dashboard/.env` if the dashboard needs any of the same keys, or keep a root `.env` and update both apps' loaders. Default: root `.env` (avoids duplication, matches common monorepo practice).
