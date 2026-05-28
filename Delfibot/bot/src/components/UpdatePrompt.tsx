import { useCallback, useEffect, useState } from "react";
import { check, type Update } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import { formatDate } from "../lib/format";

/**
 * Auto-update prompt.
 *
 * Asks the Tauri updater plugin whether a newer version is published
 * in `latest.json` (configured in `tauri.conf.json`). If so, surfaces
 * a non-modal banner above the app shell offering "Update now" / "Later".
 *
 * The check runs:
 *   1. Once on mount (initial launch).
 *   2. On a 30-minute interval (catches releases shipped after launch
 *      without forcing the user to restart the GUI).
 *   3. When the OS window regains focus (Cmd-Tab back to Delfi).
 *   4. On demand when the Settings page dispatches
 *      `delfi:check-for-updates` (the manual "Check for updates"
 *      button).
 *
 * "Later" stores the dismissed version. The banner stays hidden for
 * that exact version but re-appears when a NEWER one arrives.
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

// 30 minutes between background re-checks. Long enough that the
// GitHub API budget is comfortable even with hundreds of installs;
// short enough that a user who leaves the GUI open all day still
// sees a fresh release within the same workday.
const UPDATE_CHECK_INTERVAL_MS = 30 * 60 * 1000;

/** Custom event the Settings page (or anything else) can fire to
 *  force the prompt to re-poll latest.json. Kept loose-typed (no
 *  CustomEvent payload) because the only signal is "go look again". */
export const UPDATE_CHECK_EVENT = "delfi:check-for-updates";

export function UpdatePrompt() {
  const [update, setUpdate] = useState<Update | null>(null);
  const [phase, setPhase] = useState<
    "idle" | "downloading" | "installing" | "relaunching" | "error"
  >("idle");
  const [progress, setProgress] = useState<{ done: number; total?: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Track WHICH version the user dismissed. The banner stays hidden
  // for that exact version but re-appears when latest.json advances
  // to a newer one. Once you dismiss v1.5.13 you don't see it again
  // until v1.5.14 exists.
  const [dismissedVersion, setDismissedVersion] = useState<string | null>(null);

  // Single source of truth for the check. Wrapped in useCallback so
  // the various useEffect hooks (initial / interval / focus / event)
  // can share one reference without re-firing on every render.
  const runCheck = useCallback(async () => {
    try {
      const u = await check();
      if (u) setUpdate(u);
    } catch {
      // Silent. Most common cause is no release tagged yet
      // (404 fetching latest.json) - not worth surfacing.
    }
  }, []);

  // 1. Initial check on mount.
  useEffect(() => {
    runCheck();
  }, [runCheck]);

  // 2. Periodic background re-check (30 minutes). Catches releases
  //    that ship while the GUI is open.
  useEffect(() => {
    const id = setInterval(runCheck, UPDATE_CHECK_INTERVAL_MS);
    return () => clearInterval(id);
  }, [runCheck]);

  // 3. Re-check when the window regains focus. If the user Cmd-Tabs
  //    away for a few minutes and comes back, they get a fresh
  //    answer without waiting for the 30-min interval.
  useEffect(() => {
    const onFocus = () => { void runCheck(); };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [runCheck]);

  // 4. Manual re-check trigger from the Settings page.
  useEffect(() => {
    const onTrigger = () => { void runCheck(); };
    window.addEventListener(UPDATE_CHECK_EVENT, onTrigger);
    return () => window.removeEventListener(UPDATE_CHECK_EVENT, onTrigger);
  }, [runCheck]);

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
                if (update?.version) setDismissedVersion(update.version);
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

  if (!update || update.version === dismissedVersion) return null;

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
            onClick={() => setDismissedVersion(update.version)}
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
