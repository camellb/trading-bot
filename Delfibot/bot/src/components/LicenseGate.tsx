import { useEffect, useState } from "react";
import { api, LicenseConflictError, LicenseStatus } from "../api";

/**
 * LicenseGate.
 *
 * Wraps the entire app shell. While the cached license is not
 * `valid`, renders a blocking input screen ("Paste your license
 * key"). Once activation succeeds (sidecar confirmed against LS
 * /v1/licenses/validate), unmounts itself and renders the children
 * - the normal app - instead.
 *
 * Polling: one GET /api/license/status on mount + one re-check on
 * the `delfi:license-changed` custom event (dispatched after a
 * successful activate). No periodic polling; the sidecar holds the
 * cached state and the UI trusts it until the user re-deactivates.
 *
 * If the sidecar isn't reachable yet (e.g. boot screen still
 * showing), the gate stays in its loading state. This matches the
 * existing BootScreen flow - the LicenseGate only renders inside
 * the connected app shell, not before sidecar ready.
 */

interface Props {
  children: React.ReactNode;
}

export function LicenseGate({ children }: Props) {
  const [status, setStatus] = useState<LicenseStatus | "loading" | "error">(
    "loading",
  );
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const s = await api.license();
      setStatus(s);
    } catch (err) {
      // A network/timeout error here means the daemon is slow or
      // momentarily unreachable - it does NOT mean the user's
      // license is invalid. Flipping to the gate on every transient
      // hiccup is what the user (2026-05-24) called "it fucking
      // logged me out again." The license key never expired; the
      // daemon's executor pool just clogged on a CLOB api-key
      // retry storm and could not service /api/license/status
      // within the GUI's timeout.
      //
      // Policy: on a connection-class error, KEEP the last-known
      // status. If we were valid, we stay valid; the next 60s
      // poll will refresh once the daemon recovers. Only flip to
      // "error" / the gate if we never had a valid status to fall
      // back on (first-launch case where status is still
      // "loading").
      const raw = (err instanceof Error ? err.message : String(err)).toLowerCase();
      const isConnError =
        raw.includes("timed out")
        || raw.includes("could not connect")
        || raw.includes("connection refused")
        || raw.includes("load failed")
        || raw.includes("network");
      if (isConnError) {
        // First-launch only: surface "error" so the gate shows the
        // booting message. Otherwise hold the previous status -
        // most importantly, hold `valid: true` so the user stays
        // in the app while the daemon recovers.
        setStatus((prev) => prev === "loading" ? "error" : prev);
        if (status === "loading") {
          setErrorMsg(err instanceof Error ? err.message : String(err));
        }
        return;
      }
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    refresh();
    // Periodic re-poll. The sidecar runs a daily revocation check
    // against the licensing server; when it clears a revoked
    // license, the UI needs to flip to the gate without requiring
    // the user to relaunch. 60s is short enough to catch revocation
    // quickly, long enough to stay quiet for happy-path users.
    const id = setInterval(refresh, 60_000);
    const onChange = () => refresh();
    window.addEventListener("delfi:license-changed", onChange);
    return () => {
      clearInterval(id);
      window.removeEventListener("delfi:license-changed", onChange);
    };
  }, []);

  if (status === "loading") {
    // Blank during the first fetch so we don't flash the gate at
    // returning users who already have a valid cached license.
    return null;
  }

  if (status !== "error" && status.valid) {
    return <>{children}</>;
  }

  // Either the status fetch errored, or the sidecar reports
  // !valid. In both cases we show the gate; the difference is the
  // copy.
  //
  // Connection-class errors are silenced: while the daemon is
  // booting on first launch there's no useful information to
  // surface, and a raw "/api/license/status: timed out" reads as a
  // developer error. The activate-status page silently retries via
  // setInterval; once the daemon responds, this component flips
  // out of the gate entirely.
  const isConnError = (() => {
    if (status !== "error") return false;
    const raw = (errorMsg ?? "").toLowerCase();
    return (
      raw.includes("timed out")
      || raw.includes("could not connect")
      || raw.includes("connection refused")
      || raw.includes("load failed")
    );
  })();
  const reason = isConnError
    ? ""
    : status === "error"
      ? (errorMsg ?? "Could not reach Delfi. It may still be starting up - try again in a moment.")
      : (status.reason ?? "Your license is not active yet.");
  const hasKey = status !== "error" && status.has_key;

  return (
    <LicenseGateScreen
      reason={reason}
      hasKey={hasKey}
      onActivated={async () => {
        await refresh();
        window.dispatchEvent(new CustomEvent("delfi:license-changed"));
      }}
    />
  );
}

function LicenseGateScreen({
  reason,
  hasKey,
  onActivated,
}: {
  reason: string;
  hasKey: boolean;
  onActivated: () => void;
}) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  // When the server returns 409 "another_device_active" we capture
  // the conflict details and surface the "Sign out and use here?"
  // confirm panel. Clicking confirm re-runs activate with force=true
  // which overwrites the slot server-side; the old device locks
  // itself on its next periodic license-check poll (within 24h).
  const [conflict, setConflict] = useState<LicenseConflictError | null>(null);

  const runActivate = async (cleaned: string, force: boolean) => {
    setBusy(true);
    setSubmitError(null);
    try {
      await api.activateLicense(cleaned, force ? { force: true } : {});
      setConflict(null);
      onActivated();
    } catch (err) {
      if (err instanceof LicenseConflictError) {
        setConflict(err);
      } else {
        setSubmitError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    // Strip ALL whitespace, not just trim. Email clients sometimes
    // wrap the blob across multiple lines; the verifier's regex
    // rejects anything that isn't strictly `<base64url>.<base64url>`.
    const cleaned = key.replace(/\s+/g, "");
    if (!cleaned) {
      setSubmitError("Paste your license key.");
      return;
    }
    await runActivate(cleaned, false);
  };

  const confirmForceTakeover = async () => {
    if (busy) return;
    const cleaned = key.replace(/\s+/g, "");
    if (!cleaned) {
      setConflict(null);
      return;
    }
    await runActivate(cleaned, true);
  };

  const cancelConflict = () => {
    if (busy) return;
    setConflict(null);
  };

  // Conflict path: server says the licence is currently active on
  // another device. Show a focused confirm panel instead of the
  // paste form so the user can't paste a different key by mistake.
  if (conflict) {
    const where = conflict.currentDeviceLabel || "another device";
    return (
      <div className="license-gate">
        <div className="license-gate-card">
          <img src="/brand/mark.svg" alt="" className="license-gate-mark" />
          <h1 className="license-gate-title">DELFI</h1>
          <p className="license-gate-eyebrow">Licence already active</p>
          <p className="license-gate-reason">
            This licence is currently active on {where}. Each Delfi
            licence works on one device at a time. Sign out from
            there and activate here?
          </p>
          {submitError && (
            <p className="license-gate-error">{submitError}</p>
          )}
          <div
            style={{
              display: "flex",
              gap: 10,
              marginTop: 18,
              flexDirection: "column",
            }}
          >
            <button
              type="button"
              className="license-gate-submit"
              disabled={busy}
              onClick={confirmForceTakeover}
            >
              {busy ? "Activating..." : "Sign out there and use here"}
            </button>
            <button
              type="button"
              className="license-gate-submit"
              disabled={busy}
              onClick={cancelConflict}
              style={{ background: "transparent", border: "1px solid var(--vellum-30, #555)" }}
            >
              Cancel
            </button>
          </div>
          <p className="license-gate-help">
            The other device will sign itself out within a day, or
            immediately if it's online when you confirm.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="license-gate">
      <div className="license-gate-card">
        <img src="/brand/mark.svg" alt="" className="license-gate-mark" />
        <h1 className="license-gate-title">DELFI</h1>
        <p className="license-gate-eyebrow">
          {hasKey ? "Re-activate your license" : "Activate to continue"}
        </p>
        {reason ? <p className="license-gate-reason">{reason}</p> : null}
        <form className="license-gate-form" onSubmit={submit}>
          <label htmlFor="lk" className="license-gate-label">
            License key
          </label>
          {/* Real licenses are <base64url>.<base64url> blobs ~220
              chars long, which the email renders as a multi-line
              <pre>. A single-line <input> looked like it wanted a
              dashed short code and silently truncated/normalised
              line breaks on paste. A textarea handles the whole
              blob cleanly. */}
          <textarea
            id="lk"
            autoComplete="off"
            spellCheck={false}
            placeholder="Paste your license key"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            className="license-gate-input"
            rows={4}
            autoFocus
          />
          {submitError && (
            <p className="license-gate-error">{submitError}</p>
          )}
          <button
            type="submit"
            className="license-gate-submit"
            disabled={busy}
          >
            {busy ? "Validating..." : "Activate"}
          </button>
        </form>
        <p className="license-gate-help">
          Your license key was emailed to you after purchase.<br />
          Lost it? Email <a href="mailto:info@delfibot.com">info@delfibot.com</a>.
        </p>
      </div>
    </div>
  );
}
