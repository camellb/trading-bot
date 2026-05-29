#!/usr/bin/env bash
# Build the Delfi Python sidecar with PyInstaller and stage it where
# Tauri expects to find it.
#
# Tauri's externalBin entry in tauri.conf.json points at
# binaries/delfi-sidecar. At bundle time Tauri appends the host's
# Rust target triple (e.g. -aarch64-apple-darwin) to that base name
# and looks for that exact file. Anything else is a "resource path
# does not exist" build error.
#
# Run this from Delfibot/bot/:
#
#     ./scripts/build_sidecar.sh
#
# After it succeeds, cargo check (and cargo tauri build) will see
# the bundled binary.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${BOT_DIR}/../.." && pwd)"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

if ! "${PYTHON}" -m PyInstaller --version >/dev/null 2>&1; then
  echo "[build_sidecar] PyInstaller not installed in ${PYTHON}." >&2
  echo "[build_sidecar] Install with: ${PYTHON} -m pip install pyinstaller" >&2
  exit 1
fi

# ─── Regression-test gates ────────────────────────────────────────────
# Each script asserts a previously-fixed bug stays fixed. Failing the
# gate fails the build by design, so a regression never ships. Add a
# new gate per fixed bug whose recurrence would erode user trust.
# Cheap to run (source-text checks, no DB / network).
echo "[build_sidecar] running regression-test gates..."
"${PYTHON}" "${BOT_DIR}/tools/test_pending_payout_guards.py" || {
  echo "[build_sidecar] BUILD FAILED: pending_payout guard regression." >&2
  echo "[build_sidecar] See Delfibot/bot/tools/test_pending_payout_guards.py" >&2
  echo "[build_sidecar] and Obsidian/Delfi/50_Feedback/log_every_major_bug.md" >&2
  exit 1
}

# rustc may not be on the non-interactive PATH even though it is
# installed (rustup puts it in ~/.cargo/bin and only updates .zshrc /
# .bash_profile, which non-login shells do not source).
if command -v rustc >/dev/null 2>&1; then
  RUSTC="rustc"
elif [[ -x "${HOME}/.cargo/bin/rustc" ]]; then
  RUSTC="${HOME}/.cargo/bin/rustc"
  export PATH="${HOME}/.cargo/bin:${PATH}"
else
  echo "[build_sidecar] rustc not on PATH and not at ~/.cargo/bin/rustc." >&2
  echo "[build_sidecar] Install Rust toolchain first: https://rustup.rs" >&2
  exit 1
fi
TARGET_TRIPLE="$("${RUSTC}" -vV | sed -n 's/host: //p')"
if [[ -z "${TARGET_TRIPLE}" ]]; then
  echo "[build_sidecar] could not parse host triple from rustc -vV" >&2
  exit 1
fi

OUT_DIR="${BOT_DIR}/src-tauri/binaries"
OUT_NAME="delfi-sidecar-${TARGET_TRIPLE}"
OUT_PATH="${OUT_DIR}/${OUT_NAME}"

echo "[build_sidecar] python:     ${PYTHON}"
echo "[build_sidecar] bot dir:    ${BOT_DIR}"
echo "[build_sidecar] target:     ${TARGET_TRIPLE}"
echo "[build_sidecar] output:     ${OUT_PATH}"

cd "${BOT_DIR}"

# Clean previous artefacts so a stale binary cannot survive a failed
# rebuild and ship anyway.
#
# CAREFUL: we used to `rm -rf dist/` here, but that's also where
# Vite writes the React frontend (see tauri.conf.json
# frontendDist: "../dist"). Wiping it caused the Tauri bundle to
# ship an empty frontend — the GUI loaded but every UI change
# since the last Vite build was silently missing. PyInstaller now
# writes to `.pyinstaller-dist/`; Vite's `dist/` is left alone.
rm -rf "${BOT_DIR}/build" "${BOT_DIR}/.pyinstaller-dist"

"${PYTHON}" -m PyInstaller \
  scripts/delfi_sidecar.spec \
  --noconfirm \
  --distpath "${BOT_DIR}/.pyinstaller-dist" \
  --workpath "${BOT_DIR}/build"

SRC="${BOT_DIR}/.pyinstaller-dist/delfi-sidecar"
if [[ ! -f "${SRC}" ]]; then
  echo "[build_sidecar] expected ${SRC} after pyinstaller run, not found" >&2
  ls -la "${BOT_DIR}/dist" >&2 || true
  exit 1
fi

mkdir -p "${OUT_DIR}"
cp "${SRC}" "${OUT_PATH}"
chmod 0755 "${OUT_PATH}"

# Ad-hoc codesign WITH ENTITLEMENTS so macOS doesn't SIGKILL the binary
# when we copy it directly into /Applications without going through a
# full `npm run tauri build`. Without this, macOS 13+ kills the binary
# immediately (EXC_BAD_ACCESS / Code Signature Invalid) because the
# hash no longer matches what was recorded at last-install time.
#
# Entitlements (entitlements.plist) are MANDATORY here, not optional:
# PyInstaller bundles Python by extracting libpython3.12.dylib to
# /tmp/_MEIxxx at runtime and dlopen()ing it. That extracted dylib was
# signed by python.org with a different Team ID than our ad-hoc parent
# binary. With hardened runtime ON (which Tauri's release build sets)
# and no `disable-library-validation` entitlement, macOS rejects the
# dlopen with "code signature... not valid for use in process: mapping
# process and mapped file (non-platform) have different Team IDs" and
# the daemon enters a launchd respawn loop that never establishes a
# port file. Confirmed 2026-05-29 on the v1.5.26 build: every spawn
# attempt died at PYI-NNNN: Failed to load Python shared library.
ENTITLEMENTS="${BOT_DIR}/src-tauri/entitlements.plist"
if command -v codesign >/dev/null 2>&1; then
  if [[ -f "${ENTITLEMENTS}" ]]; then
    codesign --sign - --force --options runtime --entitlements "${ENTITLEMENTS}" "${OUT_PATH}" 2>&1 && \
      echo "[build_sidecar] ad-hoc signed (with entitlements) ${OUT_PATH}" || \
      echo "[build_sidecar] codesign failed (non-fatal; full tauri build will sign)" >&2
  else
    codesign --sign - --force "${OUT_PATH}" 2>&1 && \
      echo "[build_sidecar] ad-hoc signed (no entitlements) ${OUT_PATH}" || \
      echo "[build_sidecar] codesign failed (non-fatal; full tauri build will sign)" >&2
  fi
fi

echo "[build_sidecar] wrote ${OUT_PATH}"
echo "[build_sidecar] $(file "${OUT_PATH}" 2>/dev/null || true)"
echo "[build_sidecar] size: $(du -h "${OUT_PATH}" | cut -f1)"

# Sidecar-only hot-install (skips full Tauri build)
# When /Applications/Delfi.app exists and only the Python code changed,
# you can push the new sidecar without rebuilding the Tauri shell.
# Usage:  DELFI_HOTINSTALL=1 ./scripts/build_sidecar.sh
#
# The script copies the sidecar into both locations the installed app
# uses, ad-hoc re-signs both copies (required by macOS codesigning),
# then SIGKILL's the running daemon so launchd respawns with the new binary.
INSTALLED_APP="/Applications/Delfi.app"
if [[ "${DELFI_HOTINSTALL:-0}" == "1" && -d "${INSTALLED_APP}" ]]; then
  MAIN_BIN="${INSTALLED_APP}/Contents/MacOS/delfi-sidecar"
  DAEMON_BIN="${INSTALLED_APP}/Contents/Library/Daemon/DelfiSidecar.app/Contents/MacOS/delfi-sidecar"
  echo "[build_sidecar] hot-installing into ${INSTALLED_APP}..."
  cp "${SRC}" "${MAIN_BIN}"   && chmod 0755 "${MAIN_BIN}"
  cp "${SRC}" "${DAEMON_BIN}" && chmod 0755 "${DAEMON_BIN}"
  codesign --sign - --force "${MAIN_BIN}"   2>&1
  codesign --sign - --force "${DAEMON_BIN}" 2>&1
  echo "[build_sidecar] re-signed installed binaries"
  OLD_PID=$(pgrep -f "DelfiSidecar.app" 2>/dev/null | head -1)
  if [[ -n "${OLD_PID}" ]]; then
    kill -9 "${OLD_PID}" 2>/dev/null && echo "[build_sidecar] killed old daemon (launchd will respawn)"
  fi
  echo "[build_sidecar] hot-install done"
fi
