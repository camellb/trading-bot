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
//      the unsigned bundle
//   6. Creates the UI-less DelfiSidecar.app sub-bundle inside
//      /Applications/Delfi.app/Contents/Library/Daemon/ so the
//      launchd-managed daemon doesn't paint a duplicate Dock tile
//   7. Writes the LaunchAgent plist to
//      ~/Library/LaunchAgents/com.delfi.bot.plist and
//      bootstraps it. THIS IS THE STEP MISSING FROM THE DMG
//      DRAG-INSTALL FLOW. Without it, on macOS release builds the
//      Tauri GUI doesn't spawn the sidecar (release mode delegates
//      lifecycle to launchd) so the sidecar never starts and the
//      GUI shows "Delfi took too long to start". install.sh in
//      the repo did this step; the DMG never did. Folding it into
//      this curl|bash installer is what finally makes a clean
//      fresh install actually work end-to-end.
//   8. Clears any stale singleton-lock / sidecar.port from a
//      previous broken install so the new daemon acquires a clean
//      lock
//   9. `open /Applications/Delfi.app`
//
// The script is idempotent: running it twice just re-installs and
// re-launches. Buyer secrets at
//   ~/Library/Application Support/com.delfi.desktop/
// (the AppData dir the launchd plist points DELFI_DB_PATH at) and
// the legacy ~/Library/Application Support/Delfi/ are never touched.
//
// The downside vs a DMG: the buyer has to open Terminal. The upside:
// they have to open Terminal anyway (for the xattr step), so this
// folds the install + xattr + LaunchAgent + launch into one paste
// and removes the "drag to Applications" UX entirely.

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
LAUNCHAGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCHAGENT="$LAUNCHAGENT_DIR/com.delfi.bot.plist"
LOG_DIR="$HOME/Library/Logs/Delfi"
APPDATA_DIR="$HOME/Library/Application Support/Delfi"
DESKTOP_ID_DIR="$HOME/Library/Application Support/com.delfi.desktop"
SIDECAR_WRAPPER="$APP_PATH/Contents/Library/Daemon/DelfiSidecar.app"
SIDECAR_REAL="$APP_PATH/Contents/MacOS/delfi-sidecar"
USER_GUI="gui/$(id -u)"

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

# 5b. Re-sign the sidecar with PyInstaller-friendly entitlements.
#     Without this, Apple Silicon's library validation rejects
#     PyInstaller's extracted Python.framework on dlopen ("mapping
#     process and mapped file (non-platform) have different Team
#     IDs"). The three entitlements below are the documented
#     PyInstaller-on-macOS exception set:
#       - disable-library-validation: skip the Team ID check on
#         dlopen so the extracted Python.framework loads
#       - allow-unsigned-executable-memory: Python eval / ctypes
#         pathways need executable memory regions
#       - allow-dyld-environment-variables: PyInstaller's bootloader
#         sets DYLD_LIBRARY_PATH so the extracted dylibs are found
#     This re-sign is what finally makes the daemon boot on Apple
#     Silicon under macOS Sonoma+.
say "Applying PyInstaller-friendly entitlements to the sidecar..."
ENT_PLIST="$TMPDIR_INST/delfi-entitlements.plist"
cat > "$ENT_PLIST" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key>
    <true/>
</dict>
</plist>
PLIST
codesign --force --options runtime \
         --entitlements "$ENT_PLIST" \
         --sign - \
         "$APP_PATH/Contents/MacOS/delfi-sidecar"

# 6. Create the UI-less sidecar sub-bundle. Without this, the
#    launchd-managed daemon runs from /Applications/Delfi.app's main
#    Info.plist and paints a duplicate Dock tile next to the GUI.
#    The wrapper carries its own LSUIElement=true Info.plist; the
#    binary inside is a hard link to the real sidecar so we don't
#    double 120MB of disk.
say "Creating the headless sidecar wrapper..."
mkdir -p "$SIDECAR_WRAPPER/Contents/MacOS"
cat > "$SIDECAR_WRAPPER/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.delfi.sidecar</string>
    <key>CFBundleName</key>
    <string>Delfi Sidecar</string>
    <key>CFBundleDisplayName</key>
    <string>Delfi Sidecar</string>
    <key>CFBundleExecutable</key>
    <string>delfi-sidecar</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
PLIST
rm -f "$SIDECAR_WRAPPER/Contents/MacOS/delfi-sidecar"
# Hard link the (already re-signed) sidecar binary. Since the link
# shares the inode, the wrapper inherits the entitlements applied
# at step 5b.
ln "$SIDECAR_REAL" "$SIDECAR_WRAPPER/Contents/MacOS/delfi-sidecar"
chmod 0755 "$SIDECAR_WRAPPER/Contents/MacOS/delfi-sidecar"

# 7. Install the LaunchAgent. This is the step the DMG drag-install
#    flow has been missing. Without it, the sidecar never runs on
#    macOS release builds (the GUI delegates lifecycle to launchd).
say "Installing the LaunchAgent so the sidecar runs 24/7..."
mkdir -p "$LAUNCHAGENT_DIR" "$LOG_DIR"
cat > "$LAUNCHAGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.delfi.bot</string>

    <key>ProgramArguments</key>
    <array>
        <string>$SIDECAR_WRAPPER/Contents/MacOS/delfi-sidecar</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>EnvironmentVariables</key>
    <dict>
        <key>DELFI_DB_PATH</key>
        <string>$DESKTOP_ID_DIR/delfi.db</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>DELFI_LIVE_KILLSWITCH_OFF</key>
        <string>1</string>
    </dict>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/sidecar.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/sidecar.err</string>

    <key>WorkingDirectory</key>
    <string>$HOME</string>
</dict>
</plist>
PLIST

# Idempotent (re-)bootstrap. bootout first in case a prior plist is
# still registered, then bootstrap the new one. kickstart -k to
# force-start now without waiting for ThrottleInterval.
launchctl bootout   "$USER_GUI" "$LAUNCHAGENT"          >/dev/null 2>&1 || true
launchctl bootstrap "$USER_GUI" "$LAUNCHAGENT"          >/dev/null 2>&1 || true
launchctl kickstart -k "$USER_GUI/com.delfi.bot"        >/dev/null 2>&1 || true

# 8. Clear stale runtime state from any earlier broken install. Keeps
#    license + DB intact; only resets the singleton lock and port file.
say "Resetting runtime state..."
rm -f "$DESKTOP_ID_DIR/sidecar.lock"
rm -f "$APPDATA_DIR/sidecar.port"
rm -f "$DESKTOP_ID_DIR/sidecar.port"

# 9. Wait briefly for the daemon to come up + write its port file,
#    then launch the GUI. The GUI is a viewer; it reads the port file
#    the daemon wrote and connects.
say "Waiting for the daemon to start (up to 30s)..."
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if [[ -s "$DESKTOP_ID_DIR/sidecar.port" ]] || [[ -s "$APPDATA_DIR/sidecar.port" ]]; then
    break
  fi
  sleep 2
done

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
