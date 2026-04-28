# Lemon Squeezy post-purchase email template

Paste this into Lemon Squeezy's "Order email" composer
(LS dashboard → Store → Settings → Emails → Order confirmation).

Lemon Squeezy substitutes `{{variable}}` placeholders at send-time.
The variables used here are the ones LS exposes by default:
`{{customer_first_name}}`, `{{license_key.key}}`,
`{{order.order_number}}`, `{{order.subtotal_formatted}}`.

---

## Subject

```
Your Delfi license — install instructions inside
```

## Body (HTML or rich-text)

```
Hi {{customer_first_name}},

Thanks for buying Delfi. Below is everything you need to install
it on your computer and start running it. Save this email — your
license key is in here.

────────────────────────────────────
YOUR LICENSE KEY
{{license_key.key}}
────────────────────────────────────

DOWNLOAD

  macOS (Apple Silicon):
    https://github.com/camellb/trading-bot/releases/latest/download/Delfi.dmg

  Windows 10 / 11 (x64):
    https://github.com/camellb/trading-bot/releases/latest/download/Delfi-setup.exe

(Replace these URLs with the GH Release asset URLs once your first
v1 tag is live. The /latest/ pattern auto-redirects to the most
recent release, so the link in this email never goes stale.)

INSTALL

  1. Download the installer for your computer
  2. Open it. macOS will warn that the app is from an
     "unidentified developer" (we're not yet code-signed by Apple);
     right-click the .app and choose Open to proceed. Windows will
     show a SmartScreen warning; click "More info" → "Run anyway".
  3. The first launch takes ~30 seconds. Delfi unpacks its
     bundled engine.
  4. Paste the license key above into the activation field.
  5. Delfi opens in Simulation mode by default. It evaluates real
     Polymarket markets and records paper trades against a synthetic
     bankroll. Watch it for a day or a week before flipping to Live.
  6. To trade with real money, go to Settings → Connections, paste
     your Polymarket private key + wallet address, and toggle to
     Live. The dashboard's emergency stop is one click away.

WHAT YOU GET

  ✓ The desktop app for the platform you bought, plus every future
    release. Updates arrive automatically as a "New version
    available" prompt inside the app.
  ✓ Full dashboard: every market scan, every forecast, every fill,
    every settled outcome.
  ✓ 14-day refund window if you have not yet placed a live trade
    through the app.

WHAT YOU NEED ON YOUR SIDE

  • A funded Polymarket account + its wallet private key (Live
    mode only; Simulation runs without it).
  • An LLM API key from any major provider (Delfi reads each
    market and forecasts the outcome; you pay your provider
    directly for usage).

QUESTIONS

  Reply to this email or write to info@delfibot.com.

Order #: {{order.order_number}}
Total:   {{order.subtotal_formatted}}

— Delfi
```

---

## Notes for editing in LS

- LS's email composer is rich-text. The plain-text rendering will
  collapse the box-drawing characters around the license key; replace
  with your preferred emphasis (bold the key, or use a plain
  `License key: XXXX`).
- The license-key placeholder is `{{license_key.key}}` — the
  `.key` access is intentional because LS exposes the wider
  `license_key` object too (`.id`, `.created_at`, etc.).
- The download URLs use the GitHub Releases `/latest/` redirect
  pattern; once your first signed `v1.0.0` tag ships those links
  resolve automatically. Do NOT hardcode a versioned URL in the
  email — every customer would still get the URL that was current
  on the day they bought.
- If you change the bullet list of what's included, mirror the
  change in the homepage Pricing section's FAQ answer
  (`apps/web/app/page.tsx`, "How much does it cost?") so the two
  surfaces tell the same story.
