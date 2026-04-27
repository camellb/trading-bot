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
rm -rf "${BOT_DIR}/build" "${BOT_DIR}/dist"

"${PYTHON}" -m PyInstaller \
  scripts/delfi_sidecar.spec \
  --noconfirm \
  --distpath "${BOT_DIR}/dist" \
  --workpath "${BOT_DIR}/build"

SRC="${BOT_DIR}/dist/delfi-sidecar"
if [[ ! -f "${SRC}" ]]; then
  echo "[build_sidecar] expected ${SRC} after pyinstaller run, not found" >&2
  ls -la "${BOT_DIR}/dist" >&2 || true
  exit 1
fi

mkdir -p "${OUT_DIR}"
cp "${SRC}" "${OUT_PATH}"
chmod 0755 "${OUT_PATH}"

echo "[build_sidecar] wrote ${OUT_PATH}"
echo "[build_sidecar] $(file "${OUT_PATH}" 2>/dev/null || true)"
echo "[build_sidecar] size: $(du -h "${OUT_PATH}" | cut -f1)"
