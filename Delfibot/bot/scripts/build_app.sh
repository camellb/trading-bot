#!/usr/bin/env bash
#
# One-shot local build for the Delfi desktop app.
#
# Wraps the two build steps the install path needs:
#   1. ./scripts/build_sidecar.sh - PyInstaller bundles the Python
#      daemon into src-tauri/binaries/delfi-sidecar-<triple>.
#   2. npm run tauri build -- --bundles app - Tauri compiles the Rust
#      shell, bundles the freshly built sidecar inside Delfi.app, and
#      signs the updater artifacts.
#
# Why this script exists: tauri.conf.json has
# `createUpdaterArtifacts: true`, which makes EVERY build demand the
# Tauri Ed25519 private key. CI gets it from the
# TAURI_SIGNING_PRIVATE_KEY GitHub secret; locally we have to source
# it from ~/.tauri-keys/delfi.key. Forgetting to set the env var
# turned every local rebuild into a confusing "no private key" error.
# This wrapper makes the local flow Just Work.
#
# Usage:
#   bash Delfibot/bot/scripts/build_app.sh
#   # then: bash Delfibot/install.sh
#
# Skip the sidecar rebuild when you only changed frontend code:
#   bash Delfibot/bot/scripts/build_app.sh --no-sidecar

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SKIP_SIDECAR=0
for arg in "$@"; do
  case "$arg" in
    --no-sidecar) SKIP_SIDECAR=1 ;;
    *) echo "[build_app] unknown arg: $arg" >&2; exit 1 ;;
  esac
done

# Auto-source the Tauri signing key. tauri.conf.json's
# createUpdaterArtifacts demands a private key on every build. If
# the caller already set TAURI_SIGNING_PRIVATE_KEY in env, respect
# it. Otherwise look at the canonical local location.
if [[ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]]; then
  KEY_FILE="${HOME}/.tauri-keys/delfi.key"
  if [[ -f "${KEY_FILE}" ]]; then
    export TAURI_SIGNING_PRIVATE_KEY="$(cat "${KEY_FILE}")"
    # Empty password matches both the local key + the GH secret. If
    # you regenerated the keypair WITH a real password, also set
    # TAURI_SIGNING_PRIVATE_KEY_PASSWORD before invoking this script.
    : "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:=}"
    export TAURI_SIGNING_PRIVATE_KEY_PASSWORD
    echo "[build_app] signing key sourced from ${KEY_FILE}"
  else
    cat >&2 <<EOF
[build_app] WARNING: no signing key found.
  TAURI_SIGNING_PRIVATE_KEY is unset and ${KEY_FILE} is missing.

  The Tauri build will fail with "no private key" while
  createUpdaterArtifacts is true (Delfibot/bot/src-tauri/tauri.conf.json).

  Either:
    - Restore the key file at ~/.tauri-keys/delfi.key, or
    - Export TAURI_SIGNING_PRIVATE_KEY in your shell before re-running.

  Aborting before the build fails noisily.
EOF
    exit 1
  fi
fi

# Sidecar (PyInstaller).
if [[ "${SKIP_SIDECAR}" -eq 0 ]]; then
  echo "[build_app] building sidecar..."
  bash "${SCRIPT_DIR}/build_sidecar.sh"
else
  echo "[build_app] --no-sidecar: skipping PyInstaller step"
fi

# Tauri (Rust shell + frontend + bundle + sign).
echo "[build_app] building Tauri bundle..."
(
  cd "${BOT_DIR}"
  npm run tauri build -- --bundles app
)

cat <<EOF

[build_app] DONE.
  Built bundle: ${BOT_DIR}/src-tauri/target/release/bundle/macos/Delfi.app

  Install it:
    bash $(cd "${BOT_DIR}/.." && pwd)/install.sh
EOF
