import { useEffect, useState } from "react";
import { check, type Update } from "@tauri-apps/plugin-updater";

/**
 * Auto-update prompt.
 *
 * On mount, asks the Tauri updater plugin whether a newer version
 * is published in `latest.json` (configured in `tauri.conf.json`).
 * If so, surfaces a non-modal banner above the app shell offering
 * "Update now" / "Later". Clicking Update downloads the
 * platform-specific bundle (signature-verified against the
 * embedded public key), installs it in place, and relaunches.
 *
 * Design choices:
 *   - One check on mount, no periodic polling. Quiet for long
 *     sessions; users who care can quit-and-relaunch any time.
 *   - `Later` dismisses for the current session only; the next
 *     launch re-prompts if the update is still pending.
 *   - Errors during the check (offline, GitHub 404 because no
 *     release tagged yet, manifest schema drift) are silently
 *     swallowed: a missing update is the default state and we don't
 *     want to scare new users with transient network blips.
 *   - Errors during download/install ARE surfaced inline.
 *
 * macOS unsigned-app caveat: until Apple notarization ships, the
 * in-place .app replacement on macOS will fail to relaunch
 * (Gatekeeper re-checks the fresh binary). On Windows the in-place
 * flow works without signing. The plugin returns a clear error in
 * either case; we relay it to the user.
 */
export function UpdatePrompt() {
  const [update, setUpdate] = useState<Update | null>(null);
  const [phase, setPhase] = useState<
    "idle" | "downloading" | "installing" | "done"
  >("idle");
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const u = await check();
        if (!cancelled && u) setUpdate(u);
      } catch {
        // Silent. Most common cause is no release tagged yet
        // (404 fetching latest.json) — not worth surfacing.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!update || dismissed) return null;

  const onUpdate = async () => {
    setError(null);
    setPhase("downloading");
    try {
      await update.downloadAndInstall((event) => {
        if (event.event === "Started") {
          setPhase("downloading");
        } else if (event.event === "Finished") {
          setPhase("installing");
        }
      });
      // Update is on disk; restart picks up the new binary. We
      // intentionally don't auto-relaunch — that would need the
      // tauri-plugin-process which adds yet another moving part,
      // and on macOS the auto-relaunch fights Gatekeeper anyway
      // until notarization ships. The user quits + reopens.
      setPhase("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("idle");
    }
  };

  const label =
    phase === "downloading"
      ? "Downloading..."
      : phase === "installing"
      ? "Installing..."
      : phase === "done"
      ? "Quit Delfi to finish"
      : `Update to v${update.version}`;

  return (
    <div className="update-banner" role="status" aria-live="polite">
      <div className="update-banner-inner">
        <div className="update-banner-text">
          <span className="update-banner-title">New version available</span>
          <span className="update-banner-version">
            v{update.version}
            {update.date
              ? ` (${new Date(update.date).toLocaleDateString()})`
              : ""}
          </span>
          {error && <span className="update-banner-error">{error}</span>}
        </div>
        <div className="update-banner-buttons">
          <button
            type="button"
            className="update-banner-btn ghost"
            onClick={() => setDismissed(true)}
            disabled={phase !== "idle"}
          >
            Later
          </button>
          <button
            type="button"
            className="update-banner-btn primary"
            onClick={onUpdate}
            disabled={phase !== "idle"}
          >
            {label}
          </button>
        </div>
      </div>
    </div>
  );
}
