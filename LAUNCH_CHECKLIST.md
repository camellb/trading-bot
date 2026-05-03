# Delfi Launch Checklist

What you (the human) need to do to take Delfi from "code on
GitHub" to "people can buy and use it." Everything Claude can do
without you is already done; this list is the rest.

Items are grouped by who blocks who. Do them in order: the
stripe + license items unblock checkout, the email items unblock
delivery, the analytics items can land any time before you flip
ads on. Each item has a status box, the exact place to do the
thing, and what to copy-paste back into Vercel when you're done.

---

## 1. Generate the license signing keypair

The desktop app verifies licenses offline against an Ed25519
public key embedded in the binary; the matching private key
lives only in Vercel.

- [ ] **Generate the pair locally.** From this repo:

  ```bash
  cd apps/web
  npm install                       # if you haven't already
  node scripts/generate-license-keypair.mjs
  ```

  This writes three files into `apps/web/.keys/` (gitignored):

  - `license-private.pem` (PEM PKCS8) -> Vercel.
  - `license-public.pem` (PEM SPKI) -> reference copy.
  - `license-public.b64` (raw 32-byte public key, base64) -> the
    desktop verifier.

- [ ] **Copy the public key into the Python verifier.** Open
  `Delfibot/bot/engine/license.py`, find the line:

  ```python
  EMBEDDED_PUBLIC_KEY_B64 = ""
  ```

  Replace the empty string with the contents of
  `apps/web/.keys/license-public.b64` (one line, ~44 chars). Commit.

- [ ] **Set the private key in Vercel.** Project Settings ->
  Environment Variables -> `LICENSE_SIGNING_KEY` -> paste the
  contents of `apps/web/.keys/license-private.pem` verbatim
  (literal newlines OK). Scope: Production AND Preview.

- [ ] **Backup the private key.** A 1Password secure note works.
  If this leaks, every license ever issued is forgeable until
  you rotate; if you lose it without backup, you can't issue any
  more licenses.

---

## 2. Stripe

Checkout runs ON delfibot.com via Stripe's embedded mode at
/checkout (Delfi-themed wrapper, Stripe-iframed card field).
The post-purchase webhook signs a license + sends the buyer
email. There is no Payment Link in the default flow; the env
override lets you route CTAs to one if needed.

You already have:
  Product:  prod_URwEtsVQ0sSWmF
  Price:    price_1TT2LeIB1LZX4WOxHO98wshJ ($249 one-time)
  Live publishable key (in your hand)
  Identity verified
  Stripe Tax configured

What's left:

- [ ] **Get the live secret key.** Stripe dashboard ->
  Developers -> API keys -> click "Reveal live key" next to
  the **Secret key** row. This is the ONLY key you need to
  treat as a secret (the publishable key is safe to share).
  Copy it. Vercel -> Project Settings -> Environment Variables
  -> add `STRIPE_SECRET_KEY` (Production scope) -> paste.

- [ ] **Set the publishable key.** Vercel ->
  `NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY` (Production) -> paste
  your `pk_live_...` value.

- [ ] **Set the price id.** Vercel -> `STRIPE_PRICE_ID`
  (Production) -> paste `price_1TT2LeIB1LZX4WOxHO98wshJ`.

- [ ] **Add the webhook endpoint.** Stripe -> Developers ->
  Webhooks -> Add endpoint:
  - Endpoint URL: `https://delfibot.com/api/webhooks/stripe`
  - Listen to: `Events on your account`
  - Events (5):
    - `checkout.session.completed`        (issues the license + sends email)
    - `charge.refunded`                   (revokes on refund)
    - `charge.dispute.created`            (revokes on chargeback; logs loudly)
    - `checkout.session.async_payment_failed` (revokes on delayed-clear payment failure)
    - `checkout.session.expired`          (funnel signal only, no DB write)
  - API version: leave at default
  - Click "Add endpoint"

- [ ] **Copy the webhook signing secret.** On the endpoint
  detail page click "Signing secret" -> Reveal. Vercel ->
  `STRIPE_WEBHOOK_SECRET` (Production) -> paste.

- [ ] **(Optional) Test mode for Vercel Preview deploys.** In
  the Stripe dashboard, top-right of the sidebar, flip the
  "Test mode" toggle. Repeat the steps above to grab the
  **test** versions of:
  - `pk_test_...`  (publishable)
  - `sk_test_...`  (secret)
  - `price_test_...` (the test-mode equivalent of your price -
    Stripe creates a separate copy of the product in test mode;
    you have to recreate the $199 SKU there)
  - A separate webhook endpoint listening at the same URL
  Set them in Vercel under the **Preview** environment scope
  rather than Production. This lets you push a branch and try
  the whole flow end-to-end with Stripe's test cards
  (4242 4242 4242 4242 etc.) without touching real money.

  Skip this if you want to launch faster - the alternative is a
  $1 real charge in production followed by an immediate refund.

- [ ] **Pre-launch test.** With the live keys set:
  - Visit `https://delfibot.com/checkout` directly.
  - Use a real card; pay $1 (set the price to $1 in Stripe
    temporarily, or just pay $199 and refund yourself).
  - Watch Vercel function logs for
    `[stripe-webhook] license issued`.
  - Confirm the license email lands in the inbox you typed
    into the Stripe form.
  - Refund in Stripe -> Payments. Confirm Vercel logs show
    `[stripe-webhook] license revoked on refund`.

---

## 3. Resend

Transactional mail (license delivery, support replies). The web
app uses `noreply@delfibot.com` as the From address; that
domain has to be verified in Resend or sends bounce.

- [ ] **Add the domain.** Resend dashboard -> Domains -> Add
  domain -> `delfibot.com`. Resend gives you 3 DNS records
  (SPF, DKIM, return-path).

- [ ] **Add the DNS records.** Wherever you host
  `delfibot.com` DNS (likely Vercel domains or your registrar).
  Wait for "Verified" in Resend (~5 min usually, sometimes
  longer).

- [ ] **Get the API key.** Resend -> API Keys -> Create -> full
  access. Vercel -> `RESEND_API_KEY` -> paste. Both Production
  and Preview.

- [ ] **Test by triggering a license email.** After Stripe is
  wired (above), do the $1 test purchase. The post-purchase
  email is the canonical proof Resend works.

---

## 4. Supabase

The webhook persists licenses; needs the table to exist.

- [ ] **Apply migration 026.** Open
  `ops/supabase/migrations/026_licenses.sql` in this repo. In
  the Supabase SQL editor for your project, paste + run.
  Verify the table exists:

  ```sql
  SELECT count(*) FROM licenses;
  ```

  Should return `0`.

- [ ] **Confirm `DATABASE_URL` is set in Vercel.** Already
  documented in `apps/web/.env.example`. The webhook uses pg
  with this connection string to bypass RLS for inserts.

---

## 5. Analytics

All three loaders are env-gated. Without the env vars they
no-op silently; with them, they load on the marketing site.
None ship to the dashboard or the desktop app.

- [ ] **GA4.** Google Analytics admin -> Data streams -> Add
  stream -> Web. Copy the Measurement ID (`G-XXXXXXXXXX`).
  Vercel -> `NEXT_PUBLIC_GA_ID` -> paste.

- [ ] **Meta Pixel.** Meta Events Manager -> Pixels -> Create.
  Copy the numeric ID. Vercel -> `NEXT_PUBLIC_META_PIXEL_ID` ->
  paste.

- [ ] **Microsoft Clarity** (heatmaps + session replay, free).
  clarity.microsoft.com -> Add project -> name: Delfi.
  Settings -> copy Project ID (10-char string). Vercel ->
  `NEXT_PUBLIC_CLARITY_ID` -> paste.

After the next deploy, visit the live site and confirm:

- GA4 Realtime shows your visit.
- Meta Events Manager -> Test events fires "PageView".
- Clarity shows a recording within ~20 minutes.

---

## 6. Desktop binaries

The download links in the post-purchase email default to the
GitHub Releases page; once you have signed builds you can swap
in direct download URLs.

- [ ] **Decide whether to code-sign.** Pre-launch you can ship
  unsigned -- macOS users will need to right-click -> Open the
  first time, Windows users will see SmartScreen. Both are
  surmountable but cost conversion. Long-term, pay for an
  Apple Developer ID + Windows EV cert.

- [ ] **Cut a tagged release on GitHub.** The CI workflow at
  `.github/workflows/build.yml` already produces a `.dmg` and a
  `.msi`; pushing a tag like `v1.0.0` triggers the
  auto-publish-to-Releases job (`gh release create
  --generate-notes`).

  ```bash
  git tag v1.0.0
  git push origin v1.0.0
  ```

- [ ] **(Optional) Set direct download URLs.** Once the assets
  are on a CDN (or you decide GH Releases is fine), Vercel ->
  `DOWNLOAD_URL_MAC`, `DOWNLOAD_URL_WIN`. The post-purchase
  email picks them up.

- [ ] **Rebuild the desktop bundle locally** so the new
  `EMBEDDED_PUBLIC_KEY_B64` ships in your binary.

  ```bash
  cd Delfibot
  bash install.sh
  ```

  Then run the smoke test from
  `Obsidian Vault/Delfi/50_Feedback/test_after_ship.md`.

---

## 7. Domain + Vercel deploy

- [ ] **Point delfibot.com at Vercel.** Vercel -> Project ->
  Settings -> Domains -> Add `delfibot.com` and `www.delfibot.com`.
  Add the A / CNAME records Vercel suggests.

- [ ] **Confirm production URL is HTTPS.** Visit
  `https://delfibot.com`. The TopNav "Get Delfi" should link
  to your Stripe Payment Link.

- [ ] **Re-deploy after every env-var add.** Env vars only
  apply to subsequent builds. Trigger a manual redeploy from
  Vercel -> Deployments -> Redeploy.

---

## 8. Pre-launch end-to-end test

Before announcing, do this loop with **Stripe test mode**:

- [ ] Visit `https://delfibot.com`, click "Get Delfi".
- [ ] Pay $1 (test card `4242 4242 4242 4242`).
- [ ] Watch Vercel function logs for
  `[stripe-webhook] license issued`.
- [ ] Receive the license email at the address you put in
  Checkout.
- [ ] Download Delfi for your platform.
- [ ] Open the app. Paste the license blob into the gate.
- [ ] Confirm the LicenseGate disappears and the dashboard
  loads.
- [ ] Run the smoke test:

  ```bash
  PORT=$(cat ~/Library/Application\ Support/com.delfi.desktop/sidecar.port)
  curl -s "http://127.0.0.1:$PORT/api/state" | python3 -m json.tool
  ```

  Should return JSON with `bot_enabled`, `mode`, `ready_to_trade`.

- [ ] Refund the $1 in Stripe -> Payments. Confirm the Vercel
  log shows `[stripe-webhook] license revoked on refund` and
  the row in `licenses` has a `revoked_at` set.

When all 9 boxes above are ticked you can flip Stripe to live
mode and announce.

---

## 9. Legal copy (footer links)

The footer links to four pages that 404 today:

- [ ] `/legal/terms` - Terms of Service
- [ ] `/legal/privacy` - Privacy Policy
- [ ] `/legal/cookies` - Cookies Policy
- [ ] `/legal/risk` - Risk Disclosure

Use a generator (e.g. Termly, GetTerms) keyed on:

- One-time-fee desktop app, no SaaS account.
- Buyer's email is the only PII collected.
- Stripe handles payment data; we never see card numbers.
- Trading involves real financial risk and prediction markets
  are regulated differently per jurisdiction.

Drop the resulting Markdown into
`apps/web/app/legal/{terms,privacy,cookies,risk}/page.tsx` as
small Server Components.

---

## 10. (Post-launch, optional) Enable live trading

The bot ships with `_open_live` and the new `redeemPositions`
path both gated by `DELFI_LIVE_KILLSWITCH_OFF`. Default = OFF
= falls through to simulation fills.

This switch flips for YOU first, on YOUR machine, before any
buyer's machine. The current sequence:

- [ ] Edit the LaunchAgent plist
  `~/Library/LaunchAgents/com.delfi.bot.plist`. Add to the
  `EnvironmentVariables` dict:

  ```xml
  <key>DELFI_LIVE_KILLSWITCH_OFF</key>
  <string>1</string>
  ```

- [ ] `launchctl bootout gui/$(id -u)/com.delfi.bot`
- [ ] `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.delfi.bot.plist`
- [ ] In the dashboard, switch mode to Live and start with the
  smallest possible bankroll. Watch the first few real fills
  and confirm `tx_hash` populates on each `pm_positions` row.

For buyer machines, the install flow does NOT set this env var.
Buyers default to simulation; they (or you, eventually, via a
confirmed-flow toggle in Settings) enable live mode explicitly.

---

## What is already done

For reference, here's what Claude already shipped so you don't
need to repeat the work:

- Homepage with per-CTA-location analytics, varied button copy,
  active platform cards, hardened "is my money safe" FAQ.
- Stripe webhook (`apps/web/app/api/webhooks/stripe/route.ts`)
  with HMAC verification + idempotent inserts on session id +
  refund handling.
- Ed25519 license signer (`apps/web/lib/license.ts`).
- Keypair generator script
  (`apps/web/scripts/generate-license-keypair.mjs`).
- License-issued email template
  (`apps/web/lib/email/license-issued.ts`).
- Supabase migration 026 (`licenses` table).
- Desktop offline verifier
  (`Delfibot/bot/engine/license.py`) and `local_api.py` wiring.
- On-chain redemption helper (`pm_redeemer.py`) and
  settle_position auto-redeem hook.
- `pm_positions.redeem_tx_hash` schema column + in-place
  migration for existing local SQLite installs.
- `apps/web/.env.example` documenting every new env var.
- Analytics loaders (GA4, Meta Pixel, Clarity) -- env-gated,
  no-op until you set the IDs.
