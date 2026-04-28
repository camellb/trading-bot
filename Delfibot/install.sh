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
pkill -KILL -f "Delfi.app/Contents/MacOS" 2>/dev/null || true
sleep 1

echo "[install] syncing bundle into $INSTALLED (preserving directory inode)..."
mkdir -p "$INSTALLED"
rsync -a --delete "$BUILD_BUNDLE/" "$INSTALLED/"
touch "$INSTALLED"

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
  # Strip every com.delfi.desktop entry, then collapse any remaining
  # duplicate URLs (Dock occasionally inserts the same .app twice, even
  # when LaunchServices only has one registration). Idempotent — safe
  # to run before AND after launch.
  python3 - <<'PY'
import plistlib, subprocess
xml = subprocess.check_output(["defaults", "export", "com.apple.dock", "-"])
data = plistlib.loads(xml)
removed_delfi = 0
deduped = 0
for key in ("recent-apps", "persistent-apps"):
    arr = data.get(key)
    if not isinstance(arr, list):
        continue
    seen_urls = set()
    out = []
    for entry in arr:
        td = entry.get("tile-data", {}) or {}
        bid = td.get("bundle-identifier")
        url = (td.get("file-data", {}) or {}).get("_CFURLString") or ""
        if bid == "com.delfi.desktop":
            removed_delfi += 1
            continue
        # Dedupe by (bundle-id, url) so any other app duplicates also
        # collapse — generic robustness, not Delfi-specific.
        dedup_key = (bid, url)
        if dedup_key in seen_urls:
            deduped += 1
            continue
        seen_urls.add(dedup_key)
        out.append(entry)
    data[key] = out
print(f"[install]   delfi-entries-stripped={removed_delfi} url-duplicates-collapsed={deduped}")
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

echo "[install] launching Delfi..."
open -a "$INSTALLED"

# Second pass: when `open -a` fires, the Dock notices the launch and
# auto-adds the app to recent-apps. On some macOS versions it inserts
# TWO entries for the same URL. Wait for the launch to settle, then
# run the dedupe again so any duplicate the Dock just added gets
# collapsed.
echo "[install] waiting for app to settle..."
sleep 6
echo "[install] post-launch dedupe pass..."
_dedupe_dock_recents
pkill -KILL Dock 2>/dev/null || true

echo "[install] done."
