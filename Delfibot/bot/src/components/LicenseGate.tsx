import { useEffect, useState } from "react";
import { api, LicenseStatus } from "../api";

/**
 * LicenseGate.
 *
 * Wraps the entire app shell. While the cached license is not
 * `valid`, renders a blocking input screen ("Paste your license
 * key"). Once activation succeeds (sidecar confirmed against LS
 * /v1/licenses/validate), unmounts itself and renders the children
 * — the normal app — instead.
 *
 * Polling: one GET /api/license/status on mount + one re-check on
 * the `delfi:license-changed` custom event (dispatched after a
 * successful activate). No periodic polling; the sidecar holds the
 * cached state and the UI trusts it until the user re-deactivates.
 *
 * If the sidecar isn't reachable yet (e.g. boot screen still
 * showing), the gate stays in its loading state. This matches the
 * existing BootScreen flow — the LicenseGate only renders inside
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
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    refresh();
    const onChange = () => refresh();
    window.addEventListener("delfi:license-changed", onChange);
    return () => window.removeEventListener("delfi:license-changed", onChange);
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
  const reason =
    status === "error"
      ? errorMsg ?? "could not reach the local engine"
      : status.reason ?? "license is not active";
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

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (!key.trim()) {
      setSubmitError("Paste your license key.");
      return;
    }
    setBusy(true);
    setSubmitError(null);
    try {
      await api.activateLicense(key.trim());
      onActivated();
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="license-gate">
      <div className="license-gate-card">
        <img src="/brand/mark.svg" alt="" className="license-gate-mark" />
        <h1 className="license-gate-title">DELFI</h1>
        <p className="license-gate-eyebrow">
          {hasKey ? "Re-activate your license" : "Activate to continue"}
        </p>
        <p className="license-gate-reason">{reason}</p>
        <form className="license-gate-form" onSubmit={submit}>
          <label htmlFor="lk" className="license-gate-label">
            License key
          </label>
          <input
            id="lk"
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="XXXX-YYYY-ZZZZ-WWWW"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            className="license-gate-input"
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
          Your license key was emailed to you after purchase. Lost it?
          Email <a href="mailto:info@delfibot.com">info@delfibot.com</a>.
        </p>
      </div>
    </div>
  );
}
