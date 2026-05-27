// apps/web/app/install/mac/route.ts
//
// Returns a self-contained bash installer for macOS. The buyer pastes:
//
//   curl -fsSL https://delfibot.com/install/mac | bash
//
// in Terminal and the script:
//   1. Stops any running Delfi instance (launchd daemon + GUI + sidecar)
//   2. Trashes the old /Applications/Delfi.app
//   3. Downloads the current DMG via the existing download proxy
//      (which the user rule says must front for GitHub - the script
//      hits delfibot.com, never github.com)
//   4. Mounts, copies the .app to /Applications, detaches
//   5. Strips `com.apple.quarantine` so Gatekeeper doesn't reject
//      the unsigned bundle (`xattr -cr` is the universal workaround
//      until we ship an Apple Developer ID + notarized build)
//   6. Clears any stale singleton-lock / sidecar.port from a previous
//      broken install so the new sidecar acquires a clean lock
//   7. `open /Applications/Delfi.app`
//
// The script is idempotent: running it twice just re-installs and
// re-launches. Buyer secrets at
//   ~/Library/Application Support/Delfi/data/secrets.json
// and the SQLite DB at
//   ~/Library/Application Support/Delfi/delfi.db
// are never touched.
//
// The downside vs a DMG: the buyer has to open Terminal. The upside:
// they have to open Terminal anyway (for the xattr step), so this
// folds the install + xattr + launch into one paste and removes the
// "drag to Applications" UX entirely.

import { NextResponse } from "next/server";

export const runtime = "nodejs";

export async function GET(): Promise<NextResponse> {
  const script = `#!/usr/bin/env bash
# Delfi macOS installer. Paste:
#   curl -fsSL https://delfibot.com/install/mac | bash
# in Terminal.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[delfi] This installer is macOS only. Use the Windows download from your email." >&2
  exit 1
fi

DOWNLOAD_URL="https://delfibot.com/api/download/mac"
APP_PATH="/Applications/Delfi.app"
LAUNCHAGENT="$HOME/Library/LaunchAgents/com.delfi.bot.plist"
APPDATA_DIR="$HOME/Library/Application Support/Delfi"
DESKTOP_ID_DIR="$HOME/Library/Application Support/com.delfi.desktop"

say() { printf "\\033[36m[delfi]\\033[0m %s\\n" "$*"; }

# 1. Stop anything that's still running from a prior install
say "Stopping any running Delfi processes..."
launchctl unload "$LAUNCHAGENT" >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)/com.delfi.bot" >/dev/null 2>&1 || true
pkill -9 -f delfi-sidecar >/dev/null 2>&1 || true
pkill -9 -f "Delfi.app" >/dev/null 2>&1 || true
sleep 1

# 2. Trash the old install (your license + DB at $APPDATA_DIR stay intact)
if [[ -e "$APP_PATH" ]]; then
  say "Removing old install at $APP_PATH..."
  rm -rf "$APP_PATH"
fi

# 3. Download the latest DMG via the delfibot.com proxy
TMPDIR_INST="$(mktemp -d -t delfi-install)"
trap 'rm -rf "$TMPDIR_INST"' EXIT
DMG="$TMPDIR_INST/delfi.dmg"
say "Downloading Delfi from delfibot.com..."
if ! curl -fL --progress-bar "$DOWNLOAD_URL" -o "$DMG"; then
  echo "[delfi] Download failed. Check your internet connection and try again." >&2
  exit 1
fi
SIZE=$(stat -f%z "$DMG" 2>/dev/null || echo 0)
if (( SIZE < 10000000 )); then
  echo "[delfi] Download is suspiciously small ($SIZE bytes). Aborting." >&2
  exit 1
fi

# 4. Mount, copy, detach
MOUNT_DIR="$TMPDIR_INST/mnt"
mkdir -p "$MOUNT_DIR"
say "Mounting installer..."
hdiutil attach "$DMG" -nobrowse -mountpoint "$MOUNT_DIR" -quiet
trap 'hdiutil detach "$MOUNT_DIR" -quiet >/dev/null 2>&1 || true; rm -rf "$TMPDIR_INST"' EXIT

if [[ ! -d "$MOUNT_DIR/Delfi.app" ]]; then
  echo "[delfi] Delfi.app not found inside the DMG. Aborting." >&2
  exit 1
fi

say "Installing to $APP_PATH..."
cp -R "$MOUNT_DIR/Delfi.app" /Applications/
hdiutil detach "$MOUNT_DIR" -quiet >/dev/null 2>&1 || true
trap 'rm -rf "$TMPDIR_INST"' EXIT

# 5. Strip the macOS download-quarantine flag. Without this, Gatekeeper
#    rejects the unsigned bundle on first launch with "Delfi is damaged
#    and can't be opened". xattr -cr removes the flag recursively from
#    the bundle and everything inside it.
say "Clearing the macOS quarantine flag..."
xattr -cr "$APP_PATH"

# 6. Clear stale runtime state from any earlier broken install. Keeps
#    license + DB intact; only resets the singleton lock and port file.
say "Resetting runtime state..."
rm -f "$DESKTOP_ID_DIR/sidecar.lock"
rm -f "$APPDATA_DIR/sidecar.port"

# 7. Launch
say "Launching Delfi..."
open "$APP_PATH"

say "Done. Delfi should be opening now. Your license email has the key for the first-launch screen."
`;
  return new NextResponse(script, {
    status: 200,
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      // Curl piping into bash means each request goes through fresh.
      // No edge cache for install scripts; we want the latest one to
      // run, especially when we're patching the install logic itself.
      "Cache-Control": "no-store",
    },
  });
}
