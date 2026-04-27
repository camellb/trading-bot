# Delfi

Local desktop app for autonomous Polymarket trading. User runs it on
their own machine, holds their own keys, pays a one-time licence fee.

## Layout

- `bot/`. The desktop app (Python trader + Tauri shell + React UI).
  Shipped as a signed installer for macOS / Windows / Linux.
- `web/`. The marketing site, hosted on Vercel. Landing page, pricing,
  download links, license-verify endpoint.

These two share a brand and a license-verify contract. They do not
share code. This whole `Delfibot/` directory is intended to be lifted
into its own repository once the migration from `apps/bot` and `apps/web`
is complete.
