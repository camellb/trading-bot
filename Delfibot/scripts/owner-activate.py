#!/usr/bin/env python3
"""
Owner license bypass.

Writes a synthetic, indefinitely-cached license entry to the macOS
keychain so the LicenseGate passes without an LS round-trip. Use this
on the maintainer's own machine when:

  - Lemon Squeezy is not yet configured.
  - The maintainer needs to launch the app without spending an LS
    activation slot.
  - LS is offline and the cached state has expired past the 30-day
    grace.

This bypass works because `local_api._license_status_payload` reads
the cached `last_validated_at` from the keychain and accepts anything
within `LICENSE_OFFLINE_GRACE_DAYS`. Setting the timestamp far in the
future makes the gate permanently happy without ever talking to LS.

Run from the trading-bot venv (it has `keyring` installed):

    /Users/macmini/Desktop/trading-bot/.venv/bin/python \\
        Delfibot/scripts/owner-activate.py

After running, restart the Delfi app. The LicenseGate will read the
keychain on mount and let you through.

To revert, run with `clear` or use the in-app deactivate flow.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import keyring


SERVICE = "delfi"
KEY_LICENSE = "license_key"
KEY_META = "license_meta"

OWNER_KEY = "DELFI-OWNER-LOCAL-2026"
OWNER_META = {
    "status": "valid",
    # Year 2099 means the 30-day offline grace check always passes.
    "last_validated_at": datetime(2099, 12, 31, tzinfo=timezone.utc).isoformat(),
    "instance_id": "owner-local",
}


def activate() -> None:
    keyring.set_password(SERVICE, KEY_LICENSE, OWNER_KEY)
    keyring.set_password(SERVICE, KEY_META, json.dumps(OWNER_META))
    print(f"wrote owner license: {OWNER_KEY}")
    print("meta:", json.dumps(OWNER_META, indent=2))
    print()
    print("Restart Delfi.app to pick up the new keychain state.")


def clear() -> None:
    for k in (KEY_LICENSE, KEY_META):
        try:
            keyring.delete_password(SERVICE, k)
            print(f"deleted: {k}")
        except keyring.errors.PasswordDeleteError:
            print(f"already gone: {k}")


def status() -> None:
    key = keyring.get_password(SERVICE, KEY_LICENSE)
    meta = keyring.get_password(SERVICE, KEY_META)
    print("license_key :", key or "<unset>")
    print("license_meta:", meta or "<unset>")


def main() -> int:
    p = argparse.ArgumentParser(description="Owner license bypass for Delfi.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("activate", help="Write owner license entries (default).")
    sub.add_parser("clear",    help="Wipe license entries from keychain.")
    sub.add_parser("status",   help="Print current keychain state.")
    args = p.parse_args()

    cmd = args.cmd or "activate"
    if cmd == "activate":
        activate()
    elif cmd == "clear":
        clear()
    elif cmd == "status":
        status()
    else:
        p.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
