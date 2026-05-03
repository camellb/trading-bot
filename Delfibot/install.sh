#!/usr/bin/env bash
#
# Delfi local install: replace /Applications/Delfi.app with a fresh
# build and prevent the macOS Dock from accumulating ghost "recent app"
# entries for every rebuild.
#
# Usage:
#   cd Delfibot/bot && npm run tauri build -- --bundles app
#   bash ../install.sh
#
# What this does, in order:
#   1. Ask Delfi to quit gracefully (AppleEvent), then SIGKILL backstop.
#   2. rsync the freshly built bundle contents into /Applications/Delfi.app
#      with --delete. Note the trailing slashes: this preserves the outer
#      directory inode of /Applications/Delfi.app, which is what stops
#      macOS from registering each rebuild as "a different app" and
#      piling up Dock ghosts.
#   3. lsregister -f to push the new code into LaunchServices.
#   4. Strip every recent-apps entry whose bundle-identifier is
#      com.delfi.desktop from ~/Library/Preferences/com.apple.dock.plist.
#      Even with a stable inode the Dock fingerprints recent-apps by
#      file-mod-date, which advances per build. Stripping forces a clean
#      slate every install.
#   5. killall Dock to apply the changes (auto-respawns).
#   6. Launch the new bundle.
#
# Idempotent. If no rebuild happened, this still works (rsync is a no-op
# on identical content; the rest is purely a Dock cleanup pass).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_BUNDLE="$REPO_ROOT/Delfibot/bot/src-tauri/target/release/bundle/macos/Delfi.app"
INSTALLED="/Applications/Delfi.app"

if [[ ! -d "$BUILD_BUNDLE" ]]; then
  echo "error: built bundle not found at $BUILD_BUNDLE" >&2
  echo "       run: cd Delfibot/bot && npm run tauri build -- --bundles app" >&2
  exit 1
fi

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"

echo "[install] quitting any running Delfi..."
osascript -e 'quit app "Delfi"' 2>/dev/null || true
sleep 2

# CRITICAL: bootout the LaunchAgent FIRST, before pkill + rsync.
#
# Without this, KeepAlive=true makes launchd respawn the daemon
# every ~ThrottleInterval=10s. While we're mid-rsync (replacing
# /Applications/Delfi.app/Contents/MacOS/delfi-sidecar and the
# wrapped link inside DelfiSidecar.app), the just-respawned
# daemon's PyInstaller bootloader still has the OLD archive open
# - rsync swaps the inode under it, the bootloader's deferred
# module reads hit "PYZ magic pattern mismatch", the daemon
# crashes, launchd respawns it, rsync hits another in-flight
# bootloader, repeat.
#
# Symptom from a 2026-05-03 outage: 30+ "[delfi] starting..."
# lines in the log within minutes, plus stderr lines like
# "delfi-sidecar appears to have been moved or deleted since
# this application was launched. Continuation from this state
# is impossible. Exiting now."
#
# bootout removes the agent registration so launchd stops
# respawning. The pkill loop below then reaps any in-flight
# daemon and stops; rsync runs safely; the LaunchAgent install
# block at the bottom of this script bootstraps a fresh
# registration that runs the new binary cleanly.
USER_GUI="gui/$(id -u)"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/com.delfi.bot.plist"
if [[ -f "$LAUNCH_AGENT_PLIST" ]]; then
  echo "[install] booting out LaunchAgent so launchd stops respawning during rsync..."
  launchctl bootout "$USER_GUI" "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
  # Settle: launchctl bootout signals SIGTERM async; the daemon
  # may take a couple of seconds to actually exit, especially
  # mid-keychain syscall. Without this, pkill below races the
  # daemon's normal shutdown path and we still hit the
  # bootloader-replaced-mid-run scenario above.
  sleep 2
fi

# Hard kill loop. A single `pkill -KILL` doesn't always reap the
# sidecar when it's blocked inside SecItemCopyMatching waiting for a
# macOS keychain prompt - SIGKILL is enqueued but the process stays
# alive holding the keychain mutex until the syscall returns. If a
# zombie sidecar sticks around the next install's sidecar gets blocked
# behind the same keychain mutex and every API endpoint times out
# (the user has hit this with /api/archetypes timing out).
# Loop SIGKILL + pgrep until nothing matches, then a final settle.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  pkill -KILL -f "Delfi.app/Contents/MacOS" 2>/dev/null || true
  pkill -KILL -x "delfi-sidecar" 2>/dev/null || true
  pkill -KILL -x "delfi" 2>/dev/null || true
  sleep 0.5
  pgrep -f "Delfi.app/Contents/MacOS" >/dev/null 2>&1 || break
done
if pgrep -f "Delfi.app/Contents/MacOS" >/dev/null 2>&1; then
  echo "[install] WARNING: a Delfi process is still alive after 5s of kills." >&2
  echo "[install] If this rebuild misbehaves, run 'pkill -9 -f delfi' manually." >&2
fi
sleep 1

echo "[install] syncing bundle into $INSTALLED (preserving directory inode)..."
mkdir -p "$INSTALLED"
rsync -a --delete "$BUILD_BUNDLE/" "$INSTALLED/"
touch "$INSTALLED"

# Wrap the sidecar in a UI-element-only sub-bundle.
#
# Without this, both /Applications/Delfi.app/Contents/MacOS/delfi
# (the Tauri shell, foreground GUI) and
# /Applications/Delfi.app/Contents/MacOS/delfi-sidecar (the launchd
# daemon) launch from the SAME .app bundle and SHARE
# /Applications/Delfi.app/Contents/Info.plist (which has no
# LSUIElement). LaunchServices registers BOTH as type="Foreground"
# under bundle-id com.delfi.desktop, and the Dock paints a tile per
# foreground process - so the user sees two Delfi tiles.
#
# The fix: give the sidecar its own .app wrapper INSIDE Delfi.app
# at Contents/Library/Daemon/DelfiSidecar.app, with an Info.plist
# that sets LSUIElement=true and a distinct CFBundleIdentifier
# (com.delfi.sidecar). When launchd execs the binary at the wrapped
# path, macOS walks up from the binary, finds DelfiSidecar.app
# first, and applies its UI-element Info.plist - so the daemon
# registers as a background helper and gets NO Dock tile. The
# binary itself is a hard link to the Tauri externalBin location,
# so we don't duplicate 160MB.
SIDECAR_WRAPPER="$INSTALLED/Contents/Library/Daemon/DelfiSidecar.app"
SIDECAR_REAL="$INSTALLED/Contents/MacOS/delfi-sidecar"
echo "[install] creating UI-less sidecar wrapper at $SIDECAR_WRAPPER..."
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
    <!-- LSUIElement=true: this is a background helper, not a
         foreground application. macOS will not paint a Dock tile,
         not show it in Cmd-Tab, and not register it for the App
         Switcher. -->
    <key>LSUIElement</key>
    <true/>
    <!-- LSBackgroundOnly is the stronger version of LSUIElement;
         we use the milder LSUIElement so the process can still
         interact with windowing if needed in future. -->
</dict>
</plist>
PLIST
# Hard link the real binary so the wrapper sees the same file
# without doubling disk usage. Re-link from scratch each install
# so a stale link from a previous build can't survive.
rm -f "$SIDECAR_WRAPPER/Contents/MacOS/delfi-sidecar"
ln "$SIDECAR_REAL" "$SIDECAR_WRAPPER/Contents/MacOS/delfi-sidecar"
chmod 0755 "$SIDECAR_WRAPPER/Contents/MacOS/delfi-sidecar"

# LaunchServices keeps a registration per *path*, not per bundle-id. Past
# `cargo tauri build` runs (without --bundles app) generate a DMG that
# auto-mounts under /Volumes/dmg.<rand>/, register Delfi.app from inside
# the DMG, and never unregister when the volume detaches. The build
# output dir under target/release/... also gets registered separately
# from /Applications. The result: 4 LS entries pointing at 4 paths,
# 4 Dock icons. We unregister every known stale path, then re-register
# /Applications/Delfi.app as the single canonical entry.
echo "[install] cleaning stale LaunchServices registrations..."
for stale in \
  "$BUILD_BUNDLE" \
  /Volumes/dmg.*/Delfi.app; do
  [[ -e "$stale" ]] && "$LSREGISTER" -u "$stale" >/dev/null 2>&1 || true
done
# Unregister + re-register /Applications/Delfi.app so any duplicate
# pointing at the same path gets collapsed to a single entry.
"$LSREGISTER" -u "$INSTALLED" >/dev/null 2>&1 || true
"$LSREGISTER" -f "$INSTALLED" >/dev/null 2>&1 || true

echo "[install] killing Dock + cfprefsd before plist edit..."
# Kill Dock with SIGKILL (not SIGTERM), otherwise Dock writes its
# in-memory state on shutdown and overwrites our plist edit - which
# brings back the duplicate Delfi entries we just removed. cfprefsd
# also gets bounced so subsequent `defaults` reads don't replay the
# pre-edit cached state. Both auto-respawn via launchd.
pkill -KILL Dock 2>/dev/null || true
pkill cfprefsd 2>/dev/null || true
sleep 2

_dedupe_dock_recents() {
  # Keep at most ONE com.delfi.desktop entry per Dock list, drop the
  # rest. Also collapse any non-Delfi (bundle-id, url) duplicates as
  # generic robustness. Idempotent - safe to run pre- AND post-launch.
  #
  # The previous version stripped every com.delfi.desktop entry and
  # relied on the launch to re-add a single one. That raced: when the
  # Dock notices a launch it sometimes inserts TWO tiles in quick
  # succession (especially across rapid install cycles or when the
  # launchd-managed daemon and the user-clicked GUI both register as
  # launches). "Keep at most one" is race-proof: even if the Dock has
  # already inserted a duplicate by the time we run, we collapse to
  # one without depending on a perfectly-timed re-launch.
  python3 - <<'PY'
import plistlib, subprocess
xml = subprocess.check_output(["defaults", "export", "com.apple.dock", "-"])
data = plistlib.loads(xml)
delfi_dropped = 0
deduped = 0
for key in ("recent-apps", "persistent-apps"):
    arr = data.get(key)
    if not isinstance(arr, list):
        continue
    seen = set()
    delfi_kept = False
    out = []
    for entry in arr:
        td = entry.get("tile-data", {}) or {}
        bid = td.get("bundle-identifier")
        url = (td.get("file-data", {}) or {}).get("_CFURLString") or ""
        if bid == "com.delfi.desktop":
            if delfi_kept:
                delfi_dropped += 1
                continue
            delfi_kept = True
            seen.add((bid, url))
            out.append(entry)
            continue
        dedup_key = (bid, url)
        if dedup_key in seen:
            deduped += 1
            continue
        seen.add(dedup_key)
        out.append(entry)
    data[key] = out
print(f"[install]   delfi-duplicates-dropped={delfi_dropped} other-url-duplicates={deduped}")
with open("/tmp/dock_patched.plist", "wb") as f:
    plistlib.dump(data, f)
subprocess.check_call(["defaults", "import", "com.apple.dock", "/tmp/dock_patched.plist"])
PY
}

echo "[install] cleaning Dock plist..."
_dedupe_dock_recents

echo "[install] forcing Dock to re-read the cleaned plist..."
# Second SIGKILL so the auto-respawned Dock from before re-reads the
# freshly-written plist instead of using its still-cached in-memory
# state.
pkill -KILL Dock 2>/dev/null || true
sleep 2

# ── LaunchAgent: keep the sidecar running 24/7 ─────────────────────────
# The sidecar is the bot. It needs to survive Tauri closing, survive
# crashes (auto-restart), and start at user login. launchd handles
# all three when we install a LaunchAgent. The Tauri GUI becomes a
# viewer that connects to the running daemon via the port file.
echo "[install] installing LaunchAgent (24/7 daemon)..."
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/com.delfi.bot.plist"
PLIST_TEMPLATE="$REPO_ROOT/Delfibot/bot/src-tauri/com.delfi.bot.plist.template"
LOG_DIR="$HOME/Library/Logs/Delfi"
mkdir -p "$LAUNCH_AGENT_DIR" "$LOG_DIR"

if [[ -f "$PLIST_TEMPLATE" ]]; then
  # Substitute __HOME__ with the user's actual home dir.
  sed "s|__HOME__|${HOME}|g" "$PLIST_TEMPLATE" > "$LAUNCH_AGENT_PLIST"

  USER_GUI="gui/$(id -u)"
  # Idempotent (re-)bootstrap. `bootout` first to clear any prior
  # registration with the OLD plist contents, then bootstrap the new
  # one. Both calls swallow non-zero exits so the script keeps going
  # whether the LaunchAgent was already loaded or never loaded.
  launchctl bootout  "$USER_GUI" "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
  launchctl bootstrap "$USER_GUI" "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
  # Force-start now (RunAtLoad already implies this; kickstart -k is
  # belt-and-suspenders for the case where launchd had it cached as
  # not-running).
  launchctl kickstart -k "$USER_GUI/com.delfi.bot" 2>/dev/null || true
  echo "[install]   LaunchAgent loaded -> sidecar auto-restarts on crash + starts at login"
else
  echo "[install]   warning: LaunchAgent template not found at $PLIST_TEMPLATE" >&2
  echo "[install]   skipping LaunchAgent install; the bot will only run while the GUI is open" >&2
fi

echo "[install] launching Delfi GUI..."
open -a "$INSTALLED"

# Two more dedupe passes after the GUI launch. The Dock can insert
# a second tile up to 8-10 seconds after the launch event, especially
# when both the launchd daemon and the user-clicked GUI are observed
# as separate launches. One pass at +6s and another at +12s catches
# the late-arriving duplicate without us having to guess the timing.
echo "[install] waiting for app to settle..."
sleep 6
echo "[install] post-launch dedupe pass 1..."
_dedupe_dock_recents
pkill -KILL Dock 2>/dev/null || true
sleep 6
echo "[install] post-launch dedupe pass 2..."
_dedupe_dock_recents
pkill -KILL Dock 2>/dev/null || true

echo "[install] done."
