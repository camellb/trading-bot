# Deployment setup — Supabase, Railway, Vercel

This is the click-through guide for Phases **E** (Supabase cutover) and
**F** (Railway + Vercel deploy) of the monorepo restructure.

You have all three accounts already and GitHub is connected. The remaining
work is wiring the new monorepo layout into each platform. Follow the
sections in order — each one depends on the previous.

---

## Prerequisites (once)

1. **Push the `main` branch** to GitHub so all three platforms can see the
   new `apps/` layout.

   ```bash
   git push origin main
   ```

2. **Generate a shared API secret** used by the dashboard to call the bot.
   Keep this value — you'll paste it in twice.

   ```bash
   openssl rand -base64 32
   ```

3. **Optional: install CLIs** (already done in an earlier phase).

   ```bash
   supabase --version   # should print a version
   vercel --version
   ```

---

## Phase E — Supabase

### E1. Create the project

1. Go to https://supabase.com/dashboard → **New project**.
2. Name: `delfi` (or any name). Region: pick the one closest to Railway's
   default region (`us-east-1` → Supabase `us-east-1` is a good match).
3. Save the **database password** — Supabase only shows it once.
4. Wait for the project to finish provisioning (~2 minutes).

### E2. Grab the connection strings

From the project dashboard → **Project Settings** → **Database**:

- **Connection string → URI** (direct, port 5432) — use this from your
  laptop and for migrations.
- **Connection pooling → URI** (pgbouncer, port 6543) — use this from
  Railway and Vercel in production.

Both look like `postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres`.

### E3. Apply the migrations

Six SQL files live in [ops/supabase/migrations/](../ops/supabase/migrations/).
Apply them **in numerical order**, using the direct (port 5432) URL.

```bash
export SUPABASE_DB_URL='postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres'

for f in ops/supabase/migrations/00*.sql; do
  echo "--- applying $f"
  psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f "$f"
done
```

What each migration does:

| # | Purpose |
|---|---------|
| 001 | Seed synthetic default user `00000000-0000-0000-0000-000000000001` in `auth.users` |
| 002 | Add nullable `user_id UUID` to every per-user table; rename legacy TEXT user_id |
| 003 | Backfill every legacy row to the default user, with a NULL sanity-check |
| 004 | Flip `user_id` to `NOT NULL`, drop legacy columns, add unique constraint on `user_config.user_id` |
| 005 | Composite `(user_id, created_at DESC)` indexes for list queries |
| 006 | Enable RLS + owner-scoped SELECT/INSERT/UPDATE/DELETE policies |

**But wait — migrations 002–006 assume the schema already exists** (tables
like `pm_positions`, `predictions`, etc.). You have two paths:

- **Path A — fresh project (recommended if you haven't run the bot long):**
  Let the bot create the schema on first boot. `apps/bot/db/models.py`
  contains `create_all_tables()` called at startup. After the bot has
  created tables, run migrations 001–006.

- **Path B — restore your local DB first:** `pg_dump` your current local
  Postgres, `pg_restore` into Supabase, then apply migrations 001–006.

  ```bash
  # From your laptop, dump local DB (no ownership/ACL):
  pg_dump --no-owner --no-acl --clean --if-exists \
      "postgresql://localhost/trading_bot" \
      > /tmp/delfi_local.sql

  # Restore into Supabase:
  psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f /tmp/delfi_local.sql

  # Then run the migrations loop from above.
  ```

### E4. Verify

```bash
psql "$SUPABASE_DB_URL" -c "
  SELECT tablename, rowsecurity
  FROM pg_tables
  WHERE schemaname = 'public'
  ORDER BY tablename;
"
```

Every per-user table should show `rowsecurity = t`. Global tables
(`feed_health_log`, `macro_context_log`, `sentiment_scores`) should show
`rowsecurity = f` — that's correct, they're shared reference data.

### E5. Grab the service-role key

**Project Settings** → **API** → copy **`service_role` key**. You'll paste
this into Railway. It bypasses RLS by design (the bot is a trusted
back-office process that writes rows for every user).

---

## Phase F — Railway (bot service)

### F1. Create the service

1. https://railway.app/dashboard → **New Project** → **Deploy from GitHub
   repo** → select the monorepo.
2. After the first import, open the newly created service → **Settings**.
3. Set **Root Directory** to `apps/bot`. Railway will then read
   [apps/bot/railway.toml](../apps/bot/railway.toml) and build only that
   subtree.

### F2. Configure variables

**Settings → Variables** → add each of these. Values that are secrets are
shown as `...` — paste the real values from your password manager or from
`.env`.

| Name | Value | Source |
|---|---|---|
| `DATABASE_URL` | Supabase **pooling** URI (port 6543) | E2 |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Your Anthropic dashboard |
| `GEMINI_API_KEY` | `...` | Your Google AI Studio account |
| `BOT_API_SECRET` | the value from prereq #2 | generated |
| `PM_MODE` | `simulation` | start in simulation |
| `LOG_LEVEL` | `INFO` | |
| `CRYPTOPANIC_API_KEY` | optional | |
| `NEWSAPI_KEY` | optional | |

Telegram alerts are configured per-user from the dashboard (stored in
`user_config.telegram_bot_token` and `user_config.telegram_chat_id`). No
Railway env vars are required for Telegram.

Polymarket live trading keys (`POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`,
`PROXY_ADDRESS`, `PRIVATE_KEY`) are only needed when you flip `PM_MODE=live`.

### F3. Known blocker — bot bind address

**Heads up:** `apps/bot/bot_api.py:64-65` hardcodes the listener to
`127.0.0.1:8765`. Railway routes traffic to `0.0.0.0:$PORT` and the deploy
will fail the healthcheck until this is fixed.

Tiny change needed (call it out when you're ready and I'll do it):

```python
# apps/bot/bot_api.py
BOT_API_HOST = os.environ.get("BOT_API_HOST", "127.0.0.1")
BOT_API_PORT = int(os.environ.get("PORT") or os.environ.get("BOT_API_PORT") or 8765)
```

Then set `BOT_API_HOST=0.0.0.0` in Railway variables. Local behavior is
unchanged because the defaults match the current hardcoded values.

### F4. Deploy and capture the public URL

Once the healthcheck passes, Railway assigns a public domain like
`https://delfi-bot-production.up.railway.app`. Copy it — Vercel needs it.

---

## Phase F — Vercel (web dashboard)

### V1. Create the project

1. https://vercel.com/dashboard → **Add New** → **Project** → pick the
   GitHub repo.
2. **Root Directory**: `apps/web`. Vercel will read
   [apps/web/vercel.json](../apps/web/vercel.json) and skip the rest of
   the monorepo.
3. Framework preset: Vercel auto-detects **Next.js** from the
   `vercel.json`. Leave it.

### V2. Configure environment variables

**Settings → Environment Variables** (apply to Production + Preview):

| Name | Value |
|---|---|
| `DATABASE_URL` | Supabase **pooling** URI (port 6543), same as Railway |
| `BOT_API_URL` | Railway public domain from F4 above |
| `BOT_API_SECRET` | same value you pasted into Railway |
| `NEXT_PUBLIC_PAPER_MODE` | `true` (optional — shows "Simulation" banner) |

### V3. Deploy

Trigger the first deploy. Vercel will install `apps/web/package.json`,
run `next build`, and publish to a `*.vercel.app` domain. Open it and log
in to confirm the dashboard can talk to the bot.

---

## What I still need from you

Before you act on this doc, flag any of these that aren't obvious:

1. **Which path for E3** — fresh project (A) or restore local DB (B)? I
   default-recommended A because your local DB has 35 settled trades and
   the learning cadence will rebuild stats quickly.
2. **Bot bind-address patch** — give me the OK to make that one-line change
   to `apps/bot/bot_api.py` so Railway's healthcheck passes.
3. **Custom domains** — skip for v1 unless you want to wire them today.
4. **Supabase Auth** — deferred to a later phase. Until then, the dashboard
   talks to the bot through `BOT_API_SECRET`, not user JWTs, and every
   query runs as the synthetic default user.

When the above answers are in, Phases E and F take roughly an hour of
click-through plus the one small code change in step F3.
