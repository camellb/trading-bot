#!/usr/bin/env bash
#
# Dedupe Delfi entries in the macOS Dock.
#
# Why does this exist?
#   macOS's Dock occasionally inserts a second entry pointing at the
#   exact same /Applications/Delfi.app URL during long-running
#   sessions, even when LaunchServices has only one registration of
#   the bundle. install.sh has a dedupe pass baked in but is heavy
#   (it also rebuilds + reinstalls). This script does just the Dock
#   cleanup.
#
# Usage:
#   bash Delfibot/dock-clean.sh
#
# What it does:
#   1. Strips every entry whose bundle-identifier is
#      com.delfi.desktop from both recent-apps and persistent-apps.
#   2. Dedupes the remaining entries by (bundle-id, URL) so any
#      same-URL duplicates from other apps also collapse.
#   3. SIGKILLs the Dock so it re-reads the patched plist.
#      (auto-respawns via launchd within a second.)
#
# Idempotent. Safe to run with Delfi running (it'll reappear in
# recent-apps automatically once Dock respawns) or not (it stays
# gone until you launch Delfi again).

set -euo pipefail

python3 - <<'PY'
import plistlib
import subprocess

xml = subprocess.check_output(["defaults", "export", "com.apple.dock", "-"])
data = plistlib.loads(xml)

removed_delfi = 0
deduped = 0
for key in ("recent-apps", "persistent-apps"):
    arr = data.get(key)
    if not isinstance(arr, list):
        continue
    seen = set()
    out = []
    for entry in arr:
        td = entry.get("tile-data", {}) or {}
        bid = td.get("bundle-identifier")
        url = (td.get("file-data", {}) or {}).get("_CFURLString") or ""
        if bid == "com.delfi.desktop":
            removed_delfi += 1
            continue
        k = (bid, url)
        if k in seen:
            deduped += 1
            continue
        seen.add(k)
        out.append(entry)
    data[key] = out

print(f"[dock-clean] delfi-entries-stripped={removed_delfi} "
      f"url-duplicates-collapsed={deduped}")

with open("/tmp/_delfi_dock_clean.plist", "wb") as f:
    plistlib.dump(data, f)
subprocess.check_call(
    ["defaults", "import", "com.apple.dock", "/tmp/_delfi_dock_clean.plist"]
)
PY

pkill -KILL Dock 2>/dev/null || true
sleep 2
echo "[dock-clean] done. If Delfi is running it'll re-appear in recent-apps shortly."
