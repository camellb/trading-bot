"""
Stable per-machine fingerprint for the one-device-per-license lock.

The desktop side of the license-binding system. The server side is
`apps/web/app/api/license/{claim-device,release-device,check}` and the
table `license_activations`. See
`Obsidian/Delfi/50_Feedback/license_one_device_at_a_time.md` for the
doctrine.

What we produce:

  * `get_device_id()` -> 32-char hex string (SHA-256 hash of the platform
    machine identifier, truncated). Opaque on the server. Stable across
    reboots, app reinstalls, and Delfi version upgrades on the same
    hardware. Changes when the user gets a new physical machine, which
    is the correct behavior.
  * `get_device_label()` -> short human-readable string (max 80 chars)
    like "MacBook Pro" or "DESKTOP-A1B2C3" so the conflict-prompt UI
    can show the user something they recognise without exposing the
    hash.

Privacy: the RAW machine identifier never leaves the machine. The
hash is one-way; the server only sees the digest. Hostname /
ComputerName *does* leave the machine, but only to populate the
conflict prompt the user themselves triggered.

Failure modes: every helper has a safe fallback. If platform-specific
lookups fail we generate a best-effort identifier from
`platform.node() + platform.machine()` so a corrupted system file
doesn't lock the user out of activating. The fallback is still stable
across boots on the same hardware.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from functools import lru_cache
from typing import Optional


# Truncation length for the server-visible device_id. 32 hex chars is
# 128 bits of the SHA-256 output - more than enough to avoid practical
# collisions across the addressable customer base while keeping the
# wire form short.
_DEVICE_ID_LEN = 32

# Cap the human-readable label on the desktop side too so we never
# send a giant hostname to the server. Server also enforces 80 char
# truncation defensively.
_DEVICE_LABEL_MAX = 80


def _run(cmd: list[str], timeout: float = 2.0) -> Optional[str]:
    """Run a process, return stdout stripped, None on any failure."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            return None
        out = (proc.stdout or "").strip()
        return out or None
    except Exception:
        return None


def _platform_uuid_macos() -> Optional[str]:
    """ioreg -d2 -c IOPlatformExpertDevice | parse IOPlatformUUID.

    The IOPlatformUUID is the macOS hardware UUID. Stable across OS
    reinstalls, changes only on logic-board swap.
    """
    out = _run(["/usr/sbin/ioreg", "-d2", "-c", "IOPlatformExpertDevice"])
    if not out:
        return None
    # The line looks like:
    #   "IOPlatformUUID" = "B1F3E2A8-1234-5678-9ABC-DEF012345678"
    for line in out.splitlines():
        line = line.strip()
        if "IOPlatformUUID" in line:
            # Pull the quoted value at the end.
            parts = line.split('"')
            if len(parts) >= 4:
                return parts[-2] or None
    return None


def _platform_uuid_windows() -> Optional[str]:
    """Windows MachineGuid from HKLM\\SOFTWARE\\Microsoft\\Cryptography.

    Stable across reboots; survives Windows version upgrades. Reset on
    OS reinstall, which is the correct "different device" boundary.
    """
    out = _run([
        "reg", "query",
        r"HKLM\SOFTWARE\Microsoft\Cryptography",
        "/v", "MachineGuid",
    ])
    if not out:
        return None
    # Output format:
    #   HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Cryptography
    #       MachineGuid    REG_SZ    a1b2c3d4-...
    for line in out.splitlines():
        if "MachineGuid" in line and "REG_SZ" in line:
            parts = line.split()
            if parts:
                return parts[-1] or None
    return None


def _platform_uuid_linux() -> Optional[str]:
    """Read /etc/machine-id (or /var/lib/dbus/machine-id as a fallback).

    Stable across reboots, generated once at OS install time.
    """
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r") as fh:
                val = fh.read().strip()
            if val:
                return val
        except Exception:
            pass
    return None


def _raw_machine_identifier() -> str:
    """Return the most-stable machine identifier this OS exposes.

    Falls back to a hostname+arch composite if the platform-specific
    lookup fails so we always have SOMETHING to hash. The composite
    is stable across boots on the same hardware which is sufficient
    for the one-device-at-a-time lock; the trade-off is that two
    machines with identical hostnames + arch would collide, which is
    rare enough in practice to accept as a soft edge case.
    """
    plat = sys.platform
    val: Optional[str] = None
    if plat == "darwin":
        val = _platform_uuid_macos()
    elif plat.startswith("win"):
        val = _platform_uuid_windows()
    else:
        val = _platform_uuid_linux()
    if val:
        return val
    # Final fallback. Salt-prefixed to make it obvious in dumps that
    # this is the degraded path, not a real platform UUID.
    node = platform.node() or "unknown-host"
    mach = platform.machine() or "unknown-arch"
    return f"delfi-fallback:{node}|{mach}"


@lru_cache(maxsize=1)
def get_device_id() -> str:
    """SHA-256 of the raw machine identifier, truncated to 32 hex chars.

    Cached for the lifetime of the process - the underlying identifier
    cannot change without a process restart anyway (logic-board swap,
    OS reinstall).
    """
    raw = _raw_machine_identifier()
    # Domain-separation prefix so the same machine hashes to different
    # device_ids across unrelated products. Pure paranoia given the
    # values are only useful inside Delfi's licence table, but cheap.
    digest = hashlib.sha256(("delfi-device-v1|" + raw).encode("utf-8")).hexdigest()
    return digest[:_DEVICE_ID_LEN]


def _hostname_macos() -> Optional[str]:
    """`scutil --get ComputerName` returns the user-friendly Mac name."""
    out = _run(["/usr/sbin/scutil", "--get", "ComputerName"])
    return out or None


def _hostname_windows() -> Optional[str]:
    """`COMPUTERNAME` env var is the canonical Windows machine name."""
    val = os.environ.get("COMPUTERNAME")
    return val.strip() if val else None


@lru_cache(maxsize=1)
def get_device_label() -> str:
    """Short human-readable label for the conflict-prompt UI.

    Best-effort: macOS scutil ComputerName, Windows COMPUTERNAME env,
    `platform.node()` everywhere else. Truncated to keep the row size
    bounded. NEVER raises - if everything fails we return a generic
    "Unknown device" string so the prompt is still actionable.
    """
    plat = sys.platform
    label: Optional[str] = None
    if plat == "darwin":
        label = _hostname_macos()
    elif plat.startswith("win"):
        label = _hostname_windows()
    if not label:
        label = platform.node() or None
    if not label:
        label = "Unknown device"
    return label[:_DEVICE_LABEL_MAX]
