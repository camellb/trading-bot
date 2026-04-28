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

echo "[install] re-registering bundle with LaunchServices..."
"$LSREGISTER" -f "$INSTALLED" >/dev/null 2>&1 || true

echo "[install] stripping com.delfi.desktop from Dock recent-apps..."
python3 - <<'PY'
import plistlib, subprocess
xml = subprocess.check_output(["defaults", "export", "com.apple.dock", "-"])
data = plistlib.loads(xml)
removed = 0
for key in ("recent-apps", "persistent-apps"):
    arr = data.get(key)
    if not isinstance(arr, list):
        continue
    out = []
    for entry in arr:
        bid = None
        try:
            bid = entry.get("tile-data", {}).get("bundle-identifier")
        except Exception:
            pass
        if bid == "com.delfi.desktop":
            removed += 1
            continue
        out.append(entry)
    data[key] = out
print(f"[install]   removed {removed} stale Delfi entries from Dock plist")
with open("/tmp/dock_patched.plist", "wb") as f:
    plistlib.dump(data, f)
subprocess.check_call(["defaults", "import", "com.apple.dock", "/tmp/dock_patched.plist"])
PY

echo "[install] restarting Dock..."
killall Dock 2>/dev/null || true
sleep 2

echo "[install] launching Delfi..."
open -a "$INSTALLED"

echo "[install] done."
