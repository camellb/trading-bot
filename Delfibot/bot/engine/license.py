"""
Lemon Squeezy license verification client.

Hard-gate model: the desktop app refuses to boot past the
`<LicenseGate>` React shell until a license key has been validated
against Lemon Squeezy's `/v1/licenses/validate` endpoint at least
once. The validated state is cached in the OS keychain so the user
doesn't have to re-paste on every launch.

We do NOT permanently re-validate against the LS API on every
launch:
  - LS would see a request volume spike on every "good morning"
    boot worldwide.
  - A user's home internet flapping should not lock them out of
    a paid product.

Instead, after a successful validation, we record (in
user_config / keychain):
  - LICENSE_KEY              the actual key (so we can re-validate)
  - LICENSE_LAST_VALIDATED   ISO-8601 UTC timestamp of last success
  - LICENSE_STATUS           "valid" | "invalid" | "revoked"

The caller policy in `local_api.py`:
  - On activation (first ever paste) -> live LS call, no fallback
  - On subsequent boots -> trust the cached "valid" stamp if it's
    less than LICENSE_REVALIDATE_DAYS old; otherwise re-call LS in
    the background with LICENSE_OFFLINE_GRACE_DAYS of slack against
    network failures.

If LS responds `valid: false` (key expired/revoked/refunded) we
flip the cached status to "invalid" immediately — the bot is
disabled at next /api/bot/start call and the LicenseGate re-shows.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

LS_VALIDATE_URL = "https://api.lemonsqueezy.com/v1/licenses/validate"
LICENSE_REVALIDATE_DAYS = 7
LICENSE_OFFLINE_GRACE_DAYS = 30


@dataclass
class LicenseValidationResult:
    """What the caller in local_api.py needs to know after a call."""

    valid: bool
    """True only if LS confirmed the key + activation. False on
    revoked / expired / unknown / network-error / malformed."""

    error: Optional[str]
    """Human-readable failure reason. None on success. Surfaced to
    the user inside the LicenseGate so they know whether to retry,
    contact support, or paste a different key."""

    instance_id: Optional[str]
    """LS-issued instance identifier returned on successful
    validation. We don't currently use it for anything — instances
    are LS's mechanism for binding a license to a specific machine
    activation, useful later if we want per-machine seat limits.
    Stored alongside the key for reference."""


def validate_license(
    key: str,
    instance_name: str,
    *,
    timeout: float = 8.0,
) -> LicenseValidationResult:
    """Call Lemon Squeezy's `/v1/licenses/validate`.

    Args:
      key:           the license key the user pasted (LS format
                     `XXXX-YYYY-ZZZZ-WWWW`).
      instance_name: a human-friendly tag for which machine this is.
                     We use os.uname().nodename in user_config. LS
                     records it with the validation event so the
                     dashboard shows where the license is being used.
      timeout:       seconds before the HTTPS call gives up. Short
                     because the LicenseGate UI is blocking on this.

    Returns:
      LicenseValidationResult. `.valid` is True only if LS said yes.
      Any network failure / non-2xx / malformed body / `valid: false`
      from LS produces `valid=False` with a populated `.error`.
    """
    if not key or not key.strip():
        return LicenseValidationResult(
            valid=False, error="license key is empty", instance_id=None
        )

    body = json.dumps(
        {"license_key": key.strip(), "instance_name": instance_name or "delfi"}
    ).encode("utf-8")

    req = urllib.request.Request(
        LS_VALIDATE_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # 4xx / 5xx with a JSON body. LS uses 400 for "invalid key"
        # which we surface as "invalid" rather than "network error".
        try:
            data = json.loads(exc.read())
        except Exception:
            return LicenseValidationResult(
                valid=False,
                error=f"license server returned {exc.code}",
                instance_id=None,
            )
        return LicenseValidationResult(
            valid=False,
            error=str(data.get("error") or data.get("message") or f"HTTP {exc.code}"),
            instance_id=None,
        )
    except urllib.error.URLError as exc:
        # Network down, DNS failure, TLS error. Caller falls back to
        # the offline-grace cache if the user previously validated.
        return LicenseValidationResult(
            valid=False,
            error=f"could not reach license server: {exc.reason}",
            instance_id=None,
        )
    except Exception as exc:
        return LicenseValidationResult(
            valid=False, error=f"validation failed: {exc}", instance_id=None
        )

    try:
        data = json.loads(raw)
    except Exception:
        return LicenseValidationResult(
            valid=False,
            error="malformed response from license server",
            instance_id=None,
        )

    if not data.get("valid"):
        # LS reports the reason in either `error` (4xx-like
        # rejections returned with a 200) or in
        # `license_key.status` (e.g. "expired", "revoked").
        reason = (
            data.get("error")
            or (data.get("license_key") or {}).get("status")
            or "license is not valid"
        )
        return LicenseValidationResult(
            valid=False, error=str(reason), instance_id=None
        )

    instance = data.get("instance") or {}
    return LicenseValidationResult(
        valid=True, error=None, instance_id=instance.get("id")
    )
