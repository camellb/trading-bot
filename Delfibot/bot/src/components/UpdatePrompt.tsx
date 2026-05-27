import { useEffect, useState } from "react";
import { check, type Update } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import { formatDate } from "../lib/format";

/**
 * Auto-update prompt.
 *
 * On mount, asks the Tauri updater plugin whether a newer version
 * is published in `latest.json` (configured in `tauri.conf.json`).
 * If so, surfaces a non-modal banner above the app shell offering
 * "Update now" / "Later".
 *
 * Clicking Update Now:
 *   1. Hides the banner and takes over the entire viewport with a
 *      full-screen "Updating Delfi" splash modelled on the boot
 *      screen (same logo + wordmark + indeterminate progress bar).
 *   2. Downloads the platform-specific bundle from GitHub Releases,
 *      verifies the Ed25519 signature against the pubkey embedded
 *      in tauri.conf.json, and installs in place.
 *   3. Calls `process.relaunch()` so the new binary boots without
 *      asking the user to manually quit + reopen.
 *
 * Failure during download/install shows a recovery card with the
 * error message and a "Try again" button; user is never stranded
 * with nothing to click. Failure during the initial `check()` is
 * silent (most common cause is "no release tagged yet" / 404 on
 * latest.json, which we shouldn't show as an error to a fresh
 * user).
 */
export function UpdatePrompt() {
  const [update, setUpdate] = useState<Update | null>(null);
  const [phase, setPhase] = useState<
    "idle" | "downloading" | "installing" | "relaunching" | "error"
  >("idle");
  const [progress, setProgress] = useState<{ done: number; total?: number } | null>(null);
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

  const onUpdate = async () => {
    if (!update) return;
    setError(null);
    setProgress(null);
    setPhase("downloading");
    try {
      await update.downloadAndInstall((event) => {
        switch (event.event) {
          case "Started":
            setPhase("downloading");
            setProgress({
              done: 0,
              total: event.data?.contentLength ?? undefined,
            });
            break;
          case "Progress":
            setProgress((prev) => ({
              done: (prev?.done ?? 0) + (event.data?.chunkLength ?? 0),
              total: prev?.total,
            }));
            break;
          case "Finished":
            setPhase("installing");
            break;
        }
      });
      setPhase("relaunching");
      // Hand the new binary the steering wheel. The Tauri shell
      // process exits and the new one boots from the freshly-
      // installed bundle. The user sees the boot splash for a few
      // seconds while the daemon comes back up (the macOS self-
      // bootstrap in main.rs registers the LaunchAgent on first
      // run of the new bundle), then the dashboard reappears.
      await relaunch();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("error");
    }
  };

  const retry = () => {
    setError(null);
    setPhase("idle");
  };

  // While an update is in flight take over the entire viewport
  // with a boot-style splash. The dashboard underneath would
  // otherwise keep updating live and look broken when the daemon
  // is mid-restart.
  if (phase !== "idle") {
    const title =
      phase === "downloading"
        ? `Updating Delfi to v${update?.version ?? ""}`
        : phase === "installing"
        ? "Installing update"
        : phase === "relaunching"
        ? "Restarting Delfi"
        : "Update failed";

    const downloadedMb = (progress?.done ?? 0) / 1024 / 1024;
    const totalMb = progress?.total ? progress.total / 1024 / 1024 : null;
    const detail =
      phase === "downloading" && totalMb
        ? `${Math.round((progress!.done / progress!.total!) * 100)}%  ` +
          `(${downloadedMb.toFixed(1)} / ${totalMb.toFixed(1)} MB)`
        : phase === "downloading"
        ? downloadedMb < 1
          ? "Starting download..."
          : `${downloadedMb.toFixed(1)} MB downloaded`
        : phase === "installing"
        ? "Replacing the installed bundle. Don't quit Delfi."
        : phase === "relaunching"
        ? "Delfi will restart in a moment with the new version."
        : (error ?? "Something went wrong.");

    return (
      <div className="boot update-busy" role="status" aria-live="polite">
        <img src="/brand/mark.svg" alt="" className="boot-mark" />
        <h1>DELFI</h1>
        <p className="boot-status">{title}</p>
        <p className="boot-detail">{detail}</p>
        {phase === "error" ? (
          <div className="boot-actions">
            <button type="button" className="btn small" onClick={retry}>
              Try again
            </button>
            <button
              type="button"
              className="btn ghost small"
              onClick={() => {
                setDismissed(true);
                setPhase("idle");
              }}
            >
              Continue without updating
            </button>
          </div>
        ) : (
          <div className="boot-progress" aria-hidden="true" />
        )}
      </div>
    );
  }

  if (!update || dismissed) return null;

  return (
    <div className="update-banner" role="status" aria-live="polite">
      <div className="update-banner-inner">
        <div className="update-banner-text">
          <span className="update-banner-title">New version available</span>
          <span className="update-banner-version">
            v{update.version}
            {update.date ? ` (${formatDate(update.date)})` : ""}
          </span>
        </div>
        <div className="update-banner-buttons">
          <button
            type="button"
            className="update-banner-btn ghost"
            onClick={() => setDismissed(true)}
          >
            Later
          </button>
          <button
            type="button"
            className="update-banner-btn primary"
            onClick={onUpdate}
          >
            Update to v{update.version}
          </button>
        </div>
      </div>
    </div>
  );
}
