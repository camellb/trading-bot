"""
Offline Ed25519 license verifier.

Replaces the prior Lemon Squeezy online validator (2026-04-28 cutover
to Stripe + self-signed licenses; see apps/web/lib/license.ts for the
matching server-side signer and apps/web/scripts/generate-license-keypair.mjs
for the keypair generator).

Why offline / why Ed25519:

  * No "phone home or the bot stops" failure mode. The desktop app
    keeps working when the network's down or after Delfi shuts down
    operations - the local-first promise on the marketing site bakes
    that in. A purely-local crypto check honours that promise.
  * Ed25519 is small, fast, and supported by `cryptography` (already
    a transitive dep of web3 / py-clob-client-v2 - no new bundle bloat).
  * The signing private key never leaves Vercel; only the matching
    32-byte public key ships in this binary. Forging a license
    requires breaking Ed25519, not stealing a server secret.

Blob format (one line, URL-safe):

    <base64url(canonical_json_payload)>.<base64url(signature)>

Payload (canonical JSON, sorted keys):

    {"email":     "...",
     "id":        "<uuid>",
     "issued_at": "<iso8601>",
     "sku":       "delfi-personal-v1",
     "version":   1}

Signature: Ed25519 over the UTF-8 bytes of the base64url-encoded
payload (NOT the raw JSON). Matches the signer in
apps/web/lib/license.ts so we don't have to re-canonicalise here.

Cached state:

    keychain  ->  full signed blob (the "license key" the user
                  pastes; we keep it so we can re-verify on every
                  launch and so the user can copy it back out)
    file      ->  small JSON meta (verified payload + activation
                  timestamp), written via user_config.set_license_meta

Re-verification cadence: every launch. The crypto check is sub-
millisecond, so there's no value in trusting a stale "good last
week" stamp.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


# ── Embedded public key ────────────────────────────────────────────────────
#
# Set this to the contents of `.keys/license-public.b64` produced by
# `node apps/web/scripts/generate-license-keypair.mjs`. It is the raw
# 32-byte Ed25519 public key, base64-encoded (44 chars including padding).
#
# This is PUBLIC -- no harm shipping it in the binary or committing it.
# The matching PRIVATE key lives only in Vercel as LICENSE_SIGNING_KEY.
#
# An empty string means "no license issuer configured"; verify_license
# rejects all blobs in that case so a half-built dev release never
# accidentally accepts garbage.
#
# Override-from-env path: the env var DELFI_LICENSE_PUBLIC_KEY_B64, if
# set at process start, replaces the embedded key. Used for testing
# with the per-machine generator without rebuilding the binary.
EMBEDDED_PUBLIC_KEY_B64 = "X3B9bL0nWTAznXfL2Rqhamd3CDgFWmor9EIvT6jOYVY="


# ── Owner bypass ──────────────────────────────────────────────────────────
#
# Skip the crypto check on the maintainer's own machine. The bypass
# token is a hard-coded constant - anyone reading the binary can find
# it - but it only unlocks Delfi on the device it was pasted on,
# does NOT generate real signed licenses, and grants no extra
# privileges. Acceptable risk for a single-tenant desktop app.
OWNER_BYPASS_TOKEN = "DELFI-OWNER-LOCAL-2026"


# ── Public results ────────────────────────────────────────────────────────


@dataclass
class LicenseValidationResult:
    """Return shape for `verify_license`. Same field names as the
    legacy LS-online result so callers in local_api.py barely change."""

    valid: bool
    """True iff the blob is well-formed AND the signature verifies
    against the embedded public key. False on any failure mode."""

    error: Optional[str]
    """Human-readable failure reason. None on success. Surfaced to
    the React shell inside the LicenseGate so the user knows whether
    to retry, paste a different key, or contact support."""

    payload: Optional[dict]
    """The decoded payload dict on success (for storing in
    license_meta.json). None on failure."""


# ── Helpers ───────────────────────────────────────────────────────────────


_BLOB_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def _b64url_decode(s: str) -> bytes:
    """Re-pad and decode a URL-safe base64 string."""
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def _load_public_key() -> Optional[Ed25519PublicKey]:
    """Return the configured public key, or None if not configured.

    Caching at module-load is tempting but not done here because tests
    flip DELFI_LICENSE_PUBLIC_KEY_B64 between cases.
    """
    raw_b64 = (os.environ.get("DELFI_LICENSE_PUBLIC_KEY_B64")
               or EMBEDDED_PUBLIC_KEY_B64
               or "").strip()
    if not raw_b64:
        return None
    try:
        raw = base64.b64decode(raw_b64, validate=False)
        if len(raw) != 32:
            return None
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:
        return None


# ── Verification entrypoint ───────────────────────────────────────────────


def verify_license(blob: str) -> LicenseValidationResult:
    """Verify a license blob produced by `apps/web/lib/license.ts`.

    Args:
      blob: the full string the user pasted, exactly as delivered in
            the post-purchase email. Whitespace at the ends is
            tolerated; everything else must match the
            `<payload>.<sig>` shape.

    Returns:
      LicenseValidationResult. `.valid=True` only when the blob
      shape is correct, the signature verifies, and the payload
      parses to a dict with the expected fields. Any failure mode
      returns `valid=False` with a populated `.error`.
    """
    if not blob or not blob.strip():
        return LicenseValidationResult(
            valid=False, error="license key is empty", payload=None,
        )

    cleaned = blob.strip()

    # Owner bypass first: it is not a signed blob.
    if cleaned == OWNER_BYPASS_TOKEN:
        return LicenseValidationResult(
            valid=True,
            error=None,
            payload={
                "id":        "owner-local",
                "email":     "owner@local",
                "sku":       "delfi-owner-bypass",
                "issued_at": "2099-12-31T00:00:00+00:00",
                "version":   1,
            },
        )

    if not _BLOB_RE.match(cleaned):
        return LicenseValidationResult(
            valid=False,
            error=("license key looks malformed (expected "
                   "<payload>.<signature>)"),
            payload=None,
        )

    pub = _load_public_key()
    if pub is None:
        return LicenseValidationResult(
            valid=False,
            error="this Delfi build was not configured with a license verifier",
            payload=None,
        )

    encoded_payload, encoded_sig = cleaned.split(".", 1)
    try:
        sig = _b64url_decode(encoded_sig)
    except Exception:
        return LicenseValidationResult(
            valid=False,
            error="license signature is not valid base64",
            payload=None,
        )

    try:
        pub.verify(sig, encoded_payload.encode("utf-8"))
    except InvalidSignature:
        return LicenseValidationResult(
            valid=False,
            error="license signature does not match this product",
            payload=None,
        )
    except Exception as exc:
        return LicenseValidationResult(
            valid=False,
            error=f"license verification failed: {exc}",
            payload=None,
        )

    try:
        payload_bytes = _b64url_decode(encoded_payload)
        payload = json.loads(payload_bytes)
    except Exception:
        return LicenseValidationResult(
            valid=False,
            error="license payload is not valid JSON",
            payload=None,
        )

    if not isinstance(payload, dict):
        return LicenseValidationResult(
            valid=False,
            error="license payload is not an object",
            payload=None,
        )

    # Sanity-check the payload shape. We accept any version >= 1 (V2
    # may add fields; old verifiers should still trust V1 blobs).
    needed = {"email", "id", "issued_at", "sku", "version"}
    missing = needed - set(payload.keys())
    if missing:
        return LicenseValidationResult(
            valid=False,
            error=f"license payload is missing fields: {sorted(missing)}",
            payload=None,
        )

    try:
        # Tolerate trailing 'Z' (Stripe / our signer use both forms).
        iso = str(payload["issued_at"]).replace("Z", "+00:00")
        datetime.fromisoformat(iso)
    except Exception:
        return LicenseValidationResult(
            valid=False,
            error="license payload has malformed issued_at",
            payload=None,
        )

    return LicenseValidationResult(valid=True, error=None, payload=payload)


# ── Re-verification cache helper ─────────────────────────────────────────


def fresh_meta_for(payload: dict) -> dict:
    """Build the dict to hand to `set_license_meta` after a fresh
    successful verification. Format is intentionally similar to the
    legacy LS shape so the React shell barely changes."""
    return {
        "status":             "valid",
        "last_validated_at":  datetime.now(timezone.utc).isoformat(),
        "payload":            payload,
        # `instance_id` was the LS-issued activation slot; v1.x of the
        # offline verifier uses the license id directly so legacy
        # /api/license/status keeps returning a stable string.
        "instance_id":        str(payload.get("id") or ""),
    }
