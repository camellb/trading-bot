# Delfi (desktop app)

The local trader. Single-user. Runs on the user's machine.

## Three pieces in one binary

1. **Python trader** (`delfi/`). Forecasting, sizing, risk, learning.
   The transferred core from the old `apps/bot`, with multi-tenant
   plumbing removed.
2. **Tauri shell** (`src-tauri/`). Rust wrapper. Boots the Python
   sidecar, hosts the UI in a native webview.
3. **React UI** (`src/`). The dashboard. Stripped version of the old
   `apps/web` dashboard.

## Communication

Tauri launches the Python sidecar at startup. The sidecar serves an
aiohttp API on `127.0.0.1:<port>`. The UI calls that local port. No
authentication is needed: the only thing on the loopback interface is
the user's own UI.

## Toolchain

- Rust 1.74+ (`rustup` install)
- Node 20+
- Python 3.11+
- PyInstaller (for bundling the Python sidecar into a single binary)

## Status

Empty. Scaffolding pending Rust toolchain install on the developer
machine.
