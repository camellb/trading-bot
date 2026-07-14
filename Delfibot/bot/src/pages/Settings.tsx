import { useEffect, useRef, useState } from "react";
import { save as saveDialog } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import { getVersion } from "@tauri-apps/api/app";
import { check } from "@tauri-apps/plugin-updater";
import { UPDATE_CHECK_EVENT } from "../components/UpdatePrompt";
import {
  api,
  AutostartStatus,
  Credentials,
  LicenseStatus,
  LLMConnection,
  LLMProvider,
  LLMRole,
  LLMRoles,
  LoginItemStatus,
  NotificationsConfig,
  TelegramConfig,
  tauriRestartSidecar,
  waitForSidecar,
} from "../api";
import {
  COMMON_TIMEZONES,
  formatDateTime,
  getDisplayTz,
  resolvedTz,
  setDisplayTz,
} from "../lib/format";
import type { Page, SettingsTab } from "../App";
import { HELP_ANCHORS } from "./Help";

// Routing helper passed from App. `goto("help", undefined, anchor)`
// switches to the Help page and auto-opens the matching guide.
type Goto = (p: Page, tab?: SettingsTab, helpAnchor?: string) => void;

/** Compact "?" affordance rendered inline with a credential label.
 *  Clicking it routes to Help and opens the matching guide. */
function HelpHint({ anchor, goto }: { anchor: string; goto: Goto }) {
  return (
    <button
      type="button"
      className="help-hint"
      onClick={() => goto("help", undefined, anchor)}
      aria-label="Open setup guide"
      title="Need help? Open the setup guide."
    >
      <span aria-hidden="true">?</span>
      <span className="help-hint-label">Need help?</span>
    </button>
  );
}

/**
 * Settings - SaaS-parity layout, with desktop additions:
 *   - Simulation reset (desktop-only)
 *   - Auto-start at login toggle (macOS LaunchAgent supervision)
 *
 * Risk and archetype controls used to live here as a sub-tab. They were
 * promoted to a top-level page on 2026-05-02 (see pages/Risk.tsx).
 *
 * The active tab is owned by App and surfaced via the sidebar sub-nav.
 * This page renders one panel at a time. Each form submits independently
 * and refreshes the parent on success.
 */

const BOUNDS = {
  starting_cash: [10, 100_000] as const,
};

type ConfigShape = {
  starting_cash?: number | null;
  [k: string]: unknown;
};

interface Props {
  tab: SettingsTab;
  setTab: (t: SettingsTab) => void;
  creds: Credentials | null;
  config: ConfigShape | null;
  onSaved: () => void;
  goto: Goto;
}

const TITLES: Record<SettingsTab, { h1: string; sub: string }> = {
  account:       { h1: "Account",         sub: "" },
  app:           { h1: "App",             sub: "" },
  diagnostics:   { h1: "Diagnostics",     sub: "" },
  connections:   { h1: "Connections",     sub: "" },
  notifications: { h1: "Notifications",   sub: "" },
};

export default function Settings({ tab, creds, config, onSaved, goto }: Props) {
  // setTab is in Props for future use (eg deep-linking) but the sidebar owns
  // tab switching today; ignore it here without triggering noUnusedLocals.
  const t = TITLES[tab];
  return (
    <div className="page-wrap narrow">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">{t.h1}</h1>
            {t.sub && <p className="page-sub">{t.sub}</p>}
          </div>
        </div>
      </div>

      {tab === "account"       && <AccountPanel       config={config} onSaved={onSaved} />}
      {tab === "app"           && <AppPanel />}
      {tab === "diagnostics"   && <DiagnosticsPanel />}
      {tab === "connections"   && <ConnectionsPanel   creds={creds}   onSaved={onSaved} goto={goto} />}
      {tab === "notifications" && <NotificationsPanel goto={goto} />}
    </div>
  );
}

// ── Account ──────────────────────────────────────────────────────────────

function AccountPanel({
  config,
  onSaved,
}: {
  config: ConfigShape | null;
  onSaved: () => void;
}) {
  const [startingCash, setStartingCash] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);

  useEffect(() => {
    if (config?.starting_cash != null) setStartingCash(String(config.starting_cash));
  }, [config?.starting_cash]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const n = Number(startingCash);
      if (!Number.isFinite(n)) throw new Error("Starting cash must be a number.");
      const [lo, hi] = BOUNDS.starting_cash;
      if (n < lo || n > hi) throw new Error(`Starting cash must be between ${lo} and ${hi}.`);
      await api.updateConfig({ starting_cash: n });
      setMsg({ kind: "ok", text: `Capital set to $${n.toFixed(2)}.` });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setResetBusy(true);
    setMsg(null);
    try {
      const r = await api.resetSimulation();
      setMsg({ kind: "ok", text: r.detail || "Simulation reset." });
      setConfirm(false);
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setResetBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Capital</h2>
        </div>
        <p className="page-sub" style={{ marginBottom: 16 }}>
          Starting capital in Simulation mode.
        </p>
        <form className="form-row" onSubmit={save}>
          <div className="form-field">
            <label>Starting cash (USD)</label>
            <input
              type="number"
              min={BOUNDS.starting_cash[0]}
              max={BOUNDS.starting_cash[1]}
              step="1"
              value={startingCash}
              onChange={(e) => setStartingCash(e.target.value)}
            />
          </div>
          <div className="form-actions">
            <button type="submit" className="btn small" disabled={busy}>
              {busy ? "Saving..." : "Save capital"}
            </button>
            {msg && (
              <span className={msg.kind === "ok" ? "form-success" : "form-error"}>
                {msg.text}
              </span>
            )}
          </div>
        </form>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Simulation reset</h2>
        </div>
        <p className="page-sub" style={{ marginBottom: 16 }}>
          Restart simulation data.
        </p>
        {!confirm ? (
          <div className="form-actions">
            <button
              type="button"
              className="btn ghost small"
              onClick={() => setConfirm(true)}
            >
              Reset simulation
            </button>
          </div>
        ) : (
          <div className="form-actions">
            <button
              type="button"
              className="btn danger small"
              onClick={reset}
              disabled={resetBusy}
            >
              {resetBusy ? "Resetting..." : "Yes, reset"}
            </button>
            <button
              type="button"
              className="btn ghost small"
              onClick={() => setConfirm(false)}
              disabled={resetBusy}
            >
              Cancel
            </button>
          </div>
        )}
      </div>

      <LicensePanel />
    </>
  );
}

// ── App tab: how the app behaves on this machine ────────────────────────

function AppPanel() {
  return (
    <>
      <TimezonePanel />
      <LoginItemPanel />
      <AutostartPanel />
      <UpdateCheckPanel />
    </>
  );
}

// ── App tab: manual update check ────────────────────────────────────────

/** Shows the current running version and a button to manually poll
 *  GitHub Releases for a newer one. The same poll also runs
 *  automatically on mount, on a 30-min interval, and on window focus
 *  (see `src/components/UpdatePrompt.tsx`); this button exists for
 *  the impatient case (user knows a release shipped, doesn't want
 *  to wait for the next tick) and for the diagnostic case (user
 *  wants to confirm the updater can reach the manifest at all).
 *
 *  We call `check()` locally for the result-text feedback below the
 *  button, AND dispatch `UPDATE_CHECK_EVENT` so the global
 *  UpdatePrompt re-runs its own check and surfaces the banner if a
 *  newer version exists. Two calls in flight at once is harmless;
 *  Tauri's updater plugin is idempotent.
 */
function UpdateCheckPanel() {
  const [appVersion, setAppVersion] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "info" | "err"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    getVersion()
      .then((v) => alive && setAppVersion(v))
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  const onCheck = async () => {
    if (busy) return;
    setBusy(true);
    setMsg({ kind: "info", text: "Checking for updates..." });
    // Fire the global re-check too so the banner appears above the
    // app shell if a newer version is found. The banner is the
    // place the user actually clicks "Update now" - this button
    // just kicks the check.
    try {
      window.dispatchEvent(new Event(UPDATE_CHECK_EVENT));
    } catch {
      // No-op: dispatchEvent shouldn't throw, but we don't want a
      // dispatch failure to mask the real check result below.
    }
    try {
      const u = await check();
      if (u) {
        setMsg({
          kind: "ok",
          text: `Update available: v${u.version}. See the banner at the top of the window.`,
        });
      } else {
        setMsg({
          kind: "ok",
          text: appVersion
            ? `You're on the latest version (v${appVersion}).`
            : "You're on the latest version.",
        });
      }
    } catch (err) {
      setMsg({
        kind: "err",
        text: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Updates</h2>
      </div>
      <div className="notif-row">
        <div>
          <div className="notif-name">
            Current version: {appVersion ? `v${appVersion}` : "Loading..."}
          </div>
          <div className="notif-desc">
            {msg ? msg.text : "Click to check for a newer version."}
          </div>
        </div>
        <button
          type="button"
          className="btn small"
          disabled={busy}
          onClick={onCheck}
        >
          {busy ? "Checking..." : "Check for updates"}
        </button>
      </div>
    </div>
  );
}

// ── Diagnostics tab: ops + debugging ────────────────────────────────────

function DiagnosticsPanel() {
  return (
    <>
      <HelpPanel />
      <RestartPanel />
      <SettingsExportPanel />
      <DbBackupPanel />
    </>
  );
}

// ── Help / Contact support ─────────────────────────────────────────────

const SUPPORT_EMAIL = "info@delfibot.com";

/** Opens the user's mail client with a pre-filled support address.
 *  Surfaced at the top of Diagnostics so a user hitting an unrecoverable
 *  state (wedge that survived the watchdog, license dispute, billing
 *  question) can reach a human in one click. */
function HelpPanel() {
  const subject = "Delfi support";
  const href = `mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`;
  const open = () => { void openUrl(href); };
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Need help</h2>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        If something doesn't work properly and restarting Delfi doesn't fix it, email us.
      </p>
      <div className="form-actions">
        <button type="button" className="btn small" onClick={open}>
          Email {SUPPORT_EMAIL}
        </button>
      </div>
    </div>
  );
}

// ── Display timezone ─────────────────────────────────────────────────────

/**
 * Lets the user pick which timezone every date in the app is rendered
 * in. Defaults to "system" - whatever Intl.DateTimeFormat resolves to
 * via the OS clock. Setting persists in localStorage and applies to
 * all formatted dates on the next render.
 */
function TimezonePanel() {
  const [tz, setTz] = useState<string>(getDisplayTz() ?? "");
  const [saved, setSaved] = useState<{ kind: "ok"; text: string } | null>(null);
  const sample = "2026-05-03T15:30:00+00:00";

  const apply = (next: string) => {
    setTz(next);
    setDisplayTz(next || null);
    setSaved({
      kind: "ok",
      text: next
        ? `Saved. Dates now render in ${next}.`
        : `Saved. Dates now follow the system clock (${resolvedTz()}).`,
    });
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Display timezone</h2>
      </div>
      <div className="form-row">
        <div className="form-field">
          <label>Timezone</label>
          <select
            value={tz}
            onChange={(e) => apply(e.target.value)}
          >
            <option value="">System default ({resolvedTz()})</option>
            {COMMON_TIMEZONES.map((z) => (
              <option key={z.value} value={z.value}>
                {z.label} - {z.value}
              </option>
            ))}
          </select>
          <span className="form-hint">
            Sample: {formatDateTime(sample)}
          </span>
        </div>
      </div>
      {saved && (
        <p className="form-success" style={{ marginTop: 12 }}>
          {saved.text}
        </p>
      )}
    </div>
  );
}

// ── Auto-start at login ──────────────────────────────────────────────────

/**
 * Auto-start panel inside Account.
 *
 * Toggles the macOS LaunchAgent at ~/Library/LaunchAgents/
 * com.delfi.bot.plist. ON means the daemon launches at every user
 * login and auto-restarts on crash (RunAtLoad=true + KeepAlive=true).
 * OFF means the daemon doesn't start at login; toggling OFF also
 * stops the currently-running daemon (launchctl bootout signals
 * SIGTERM).
 *
 * Currently macOS-only. On other platforms the panel renders a
 * disabled state with a "macOS-only" hint.
 */
function AutostartPanel() {
  const [status, setStatus] = useState<AutostartStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.autostart()
      .then((s) => alive && setStatus(s))
      .catch(() => alive && setStatus({
        supported: false,
        enabled:   false,
        reason:    "Could not read auto-start status.",
      }));
    return () => { alive = false; };
  }, []);

  const toggle = async () => {
    if (!status?.supported || busy) return;
    const next = !status.enabled;
    const previous = status;
    // Optimistic flip: the launchctl call takes 1-2s and the user
    // wants to see the switch move immediately.
    setStatus({ ...status, enabled: next });
    setBusy(true);
    setMsg(null);
    try {
      const updated = await api.setAutostart(next);
      setStatus(updated);
      setMsg({
        kind: "ok",
        text: next
          ? "Auto-start enabled. Delfi will launch at every login."
          : "Auto-start disabled. Delfi stopped and won't start at login.",
      });
    } catch (err) {
      setStatus(previous);
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Auto-start at login</h2>
      </div>
      <div className="notif-row">
        <div>
          <div className="notif-name">
            Run Delfi automatically at login
          </div>
          <div className="notif-desc">
            {status === null
              ? "Loading..."
              : status.supported === false
                ? (status.reason ?? "Not available on this platform.")
                : status.enabled
                  ? "Currently enabled. Delfi is running in the background."
                  : (status.reason ?? "Currently disabled. Delfi is not running.")}
          </div>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            checked={!!status?.enabled}
            disabled={!status?.supported || busy}
            onChange={toggle}
          />
          <span className="toggle-slider" />
        </label>
      </div>
      {msg && (
        <p className={msg.kind === "ok" ? "form-success" : "form-error"}
           style={{ marginTop: 12 }}>
          {msg.text}
        </p>
      )}
    </div>
  );
}

// ── Login item: open the GUI window at login ─────────────────────────────

/** Toggle that adds Delfi.app to the user's macOS Login Items so the
 *  GUI window pops up at login. Independent of the autostart-the-
 *  daemon toggle above: that one runs the bot headlessly, this one
 *  controls whether you also see the dashboard window automatically. */
function LoginItemPanel() {
  const [status, setStatus] = useState<LoginItemStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.loginItem()
      .then((s) => alive && setStatus(s))
      .catch(() => alive && setStatus({
        supported: false,
        enabled:   false,
        reason:    "Could not read login item status.",
      }));
    return () => { alive = false; };
  }, []);

  const toggle = async () => {
    if (!status?.supported || busy) return;
    const next = !status.enabled;
    const previous = status;
    setStatus({ ...status, enabled: next });
    setBusy(true);
    setMsg(null);
    try {
      const updated = await api.setLoginItem(next);
      setStatus(updated);
      setMsg({
        kind: "ok",
        text: next
          ? "Delfi window will open at login."
          : "Delfi window will not open automatically at login.",
      });
    } catch (err) {
      setStatus(previous);
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  // Hide on platforms that don't support this distinct concept.
  // macOS separates "launchd autostart" (headless daemon) from
  // "Login Items" (open the window). Windows has only one concept,
  // already covered by AutostartPanel via HKCU\Run.
  if (status && status.supported === false) {
    return null;
  }
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Open Delfi window at login</h2>
      </div>
      <div className="notif-row">
        <div>
          <div className="notif-name">Open Delfi window at login</div>
          <div className="notif-desc">
            {status === null
              ? "Loading..."
              : status.supported === false
                ? (status.reason ?? "Not available on this platform.")
                : status.enabled
                  ? "Enabled."
                  : "Disabled. Open Delfi manually from Applications."}
          </div>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            checked={!!status?.enabled}
            disabled={!status?.supported || busy}
            onChange={toggle}
          />
          <span className="toggle-slider" />
        </label>
      </div>
      {msg && (
        <p className={msg.kind === "ok" ? "form-success" : "form-error"}
           style={{ marginTop: 12 }}>
          {msg.text}
        </p>
      )}
    </div>
  );
}

// ── Restart Delfi ───────────────────────────────────────────────────────

/** One-click restart of the daemon.
 *
 *  Uses the Tauri-side `restart_sidecar` command, which runs in the
 *  shell process (not the daemon) and is therefore reachable even
 *  when the daemon's HTTP loop is wedged. The Rust command:
 *    1. reads the port file
 *    2. lsof's the actual pid listening on that port
 *    3. SIGKILLs that pid (so it can't survive in a hung state)
 *    4. runs `launchctl kickstart -k gui/<uid>/com.delfi.bot` as a
 *       belt-and-braces respawn trigger
 *  Step 3 is the bulletproof bit: it bypasses launchd's tracked-pid
 *  state entirely. KeepAlive=true on the LaunchAgent respawns the
 *  daemon regardless.
 *
 *  After triggering, we poll /api/state until the new daemon is
 *  reachable (or 30s elapses). On timeout we tell the user to quit
 *  + relaunch Delfi manually so they have a concrete next step. */
function RestartPanel() {
  const [phase, setPhase] = useState<"idle" | "restarting" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  const restart = async () => {
    setError(null);
    setPhase("restarting");
    setConfirm(false);
    try {
      await tauriRestartSidecar();

      // Poll Rust IPC directly for the new daemon's port. Rust
      // returns as soon as it's fired the kill + kickstart; the
      // daemon usually comes back in 5-10 s (launchd
      // ThrottleInterval=10 s + PyInstaller cold-start).
      const alive = await waitForSidecar(60_000);
      if (alive) {
        // Reload so the React tree reconnects to the freshly-booted
        // daemon on the new port. The full-screen overlay is gone
        // the instant the page reloads, so the user sees the boot
        // splash for a beat and then the dashboard.
        window.location.reload();
        return;
      }
      setError(
        "Delfi did not come back within 60 seconds. " +
        "Quit Delfi from the macOS menu bar and reopen " +
        "it from /Applications.",
      );
      setPhase("error");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("error");
    }
  };

  // While restarting, take over the entire viewport with a boot-
  // style splash. Same treatment as the auto-updater so the user
  // gets a single consistent "Delfi is doing something, sit tight"
  // experience instead of an inline button spinner over a stale
  // dashboard.
  if (phase !== "idle") {
    return (
      <div className="boot update-busy" role="status" aria-live="polite">
        <img src="/brand/mark.svg" alt="" className="boot-mark" />
        <h1>DELFI</h1>
        <p className="boot-status">
          {phase === "restarting" ? "Restarting Delfi" : "Restart failed"}
        </p>
        <p className="boot-detail">
          {phase === "restarting"
            ? "Don't worry, your positions are safe and sound."
            : (error ?? "Something went wrong.")}
        </p>
        {phase === "error" ? (
          <div className="boot-actions">
            <button
              type="button"
              className="btn small"
              onClick={() => {
                setError(null);
                setPhase("idle");
              }}
            >
              Back to settings
            </button>
          </div>
        ) : (
          <div className="boot-progress" aria-hidden="true" />
        )}
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Restart Delfi</h2>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Restarts Delfi. Open positions are preserved, any
        in-progress market scans are cancelled, and Delfi resumes
        within about 10 seconds. Use this when something looks
        stuck.
      </p>
      {!confirm ? (
        <div className="form-actions">
          <button
            type="button"
            className="btn ghost small"
            onClick={() => setConfirm(true)}
          >
            Restart Delfi
          </button>
        </div>
      ) : (
        <div className="form-actions">
          <button type="button" className="btn small" onClick={restart}>
            Yes, restart
          </button>
          <button
            type="button"
            className="btn ghost small"
            onClick={() => setConfirm(false)}
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

// ── DB backup ───────────────────────────────────────────────────────────

/** Export a consistent snapshot of the SQLite DB to a user-chosen
 *  path via SQLite VACUUM INTO. Refuses to overwrite the live DB. */
/**
 * Export / import the user's preferences as a portable JSON file.
 *
 * Use cases:
 *   1. Bootstrap a new machine in one click - export on old, import
 *      on new. Faster than re-clicking through every risk slider and
 *      archetype multiplier.
 *   2. Share a strategy. A user can export their tuned config and
 *      a friend can import the same multipliers + skip list + risk
 *      brakes verbatim.
 *
 * What does NOT travel in the file:
 *   - Polymarket private key, LLM API keys, license key, Telegram
 *     bot token. Custody stays per-machine.
 *   - Position history, learning state, calibration data. Too
 *     machine-specific; would lie about win rate on the new device.
 *   - wallet_address. Derived from the key on each machine.
 *
 * Backend strips these on export AND on import, so a hand-edited
 * file can't sneak credentials past the import.
 */
function SettingsExportPanel() {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // We deliberately don't use Tauri's fs plugin here. dbBackup
  // already takes that path because SQLite needs an absolute file
  // path to VACUUM INTO. For settings export the payload is small,
  // and a Blob + anchor.download click works inside Tauri's webview
  // without requiring an extra capability + plugin install. Same
  // story for import: a hidden <input type="file"> is enough.
  const exportSettings = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const settings = await api.exportSettings();
      const json = JSON.stringify(settings, null, 2);
      const today = new Date().toISOString().slice(0, 10);
      const blob = new Blob([json], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `delfi-settings-${today}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Revoke after a tick so the download has time to start. 1s
      // is plenty; the browser only needs the URL alive until it
      // commits the bytes to disk.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setMsg({ kind: "ok", text: `Saved delfi-settings-${today}.json to your Downloads folder.` });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const onImportFile = async (file: File) => {
    setBusy(true);
    setMsg(null);
    try {
      const raw = await file.text();
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(raw);
      } catch {
        throw new Error("File is not valid JSON.");
      }
      const r = await api.importSettings(parsed);
      setMsg({
        kind: "ok",
        text: (
          `Imported ${r.applied} setting${r.applied === 1 ? "" : "s"}. `
          + `Reload Delfi to see the new values applied.`
        ),
      });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const triggerImport = () => {
    setMsg(null);
    fileInputRef.current?.click();
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Settings backup</h2>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Save your risk settings to a portable JSON file. You can use
        it to share a strategy with another Delfi user, or roll back
        after experimenting.
      </p>
      <div className="form-actions" style={{ gap: 12 }}>
        <button
          type="button"
          className="btn small"
          onClick={exportSettings}
          disabled={busy}
        >
          Export settings...
        </button>
        <button
          type="button"
          className="btn small ghost"
          onClick={triggerImport}
          disabled={busy}
        >
          Import settings...
        </button>
        {/* Hidden file picker driven by the Import button. type=file
            with a JSON-only accept lets the OS native picker handle
            the path UX. We grab the selected File on change, hand it
            to onImportFile, then clear .value so picking the same
            file twice still triggers a change event. */}
        <input
          ref={fileInputRef}
          type="file"
          accept="application/json,.json"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) void onImportFile(f);
          }}
        />
      </div>
      {msg && (
        <p className={msg.kind === "ok" ? "form-success" : "form-error"}
           style={{ marginTop: 12 }}>
          {msg.text}
        </p>
      )}
    </div>
  );
}

function DbBackupPanel() {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const backup = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      const dest = await saveDialog({
        title: "Save Delfi backup",
        defaultPath: `delfi-backup-${ts}.db`,
        filters: [{ name: "SQLite DB", extensions: ["db"] }],
      });
      if (!dest) {
        setBusy(false);
        return;
      }
      const r = await api.dbBackup(dest);
      const mb = (r.size / (1024 * 1024)).toFixed(2);
      setMsg({
        kind: "ok",
        text: `Backup written to ${r.path} (${mb} MB)`,
      });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Database backup</h2>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Export a snapshot of every position, evaluation, and config row.
      </p>
      <div className="form-actions">
        <button
          type="button"
          className="btn small"
          onClick={backup}
          disabled={busy}
        >
          {busy ? "Backing up..." : "Export backup..."}
        </button>
      </div>
      {msg && (
        <p className={msg.kind === "ok" ? "form-success" : "form-error"}
           style={{ marginTop: 12 }}>
          {msg.text}
        </p>
      )}
    </div>
  );
}

// ── License ──────────────────────────────────────────────────────────────

/**
 * License panel inside Account.
 *
 * Lets the user see the license currently activated on this machine
 * and sign out of it. "Sign out" calls /api/license/deactivate which
 * (a) tells Lemon Squeezy to free the activation slot for this
 * instance, then (b) wipes the local keychain. After that the
 * LicenseGate re-mounts and the user can paste a different key.
 *
 * Used for: moving Delfi to a new computer, handing the machine to
 * someone else, recovering from a billing error after a refund.
 */
function LicensePanel() {
  const [status, setStatus] = useState<LicenseStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err" | "warn"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.license()
      .then((s) => alive && setStatus(s))
      .catch(() => alive && setStatus(null));
    return () => { alive = false; };
  }, []);

  const signOut = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const next = await api.deactivateLicense();
      setStatus(next);
      if (next.warning) {
        setMsg({ kind: "warn", text: next.warning });
      } else {
        setMsg({ kind: "ok", text: "Signed out. Restarting will show the license screen." });
      }
      // Re-mounts the LicenseGate the next time it polls.
      window.dispatchEvent(new CustomEvent("delfi:license-changed"));
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };


  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">License</h2>
      </div>
      {status?.has_key ? (
        <p className="page-sub" style={{ marginBottom: 16 }}>
          Activated and linked to {status.device_label || "this device"}.
        </p>
      ) : (
        <p className="page-sub" style={{ marginBottom: 16 }}>
          No license activated on this machine.
        </p>
      )}
      {status?.has_key && (
        !confirm ? (
          <div className="form-actions">
            <button
              type="button"
              className="btn ghost small"
              onClick={() => setConfirm(true)}
              disabled={busy}
            >
              Sign out from this device
            </button>
          </div>
        ) : (
          <div className="form-actions">
            <button
              type="button"
              className="btn danger small"
              onClick={signOut}
              disabled={busy}
            >
              {busy ? "Signing out..." : "Yes, sign out"}
            </button>
            <button
              type="button"
              className="btn ghost small"
              onClick={() => setConfirm(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        )
      )}
      {msg && (
        <p
          className={
            msg.kind === "ok"   ? "form-success" :
            msg.kind === "warn" ? "form-error"   :
            "form-error"
          }
          style={{ marginTop: 12 }}
        >
          {msg.text}
        </p>
      )}
    </div>
  );
}

// ── Connections ──────────────────────────────────────────────────────────

// Shared add/edit payload for an LLM connection. `api_key` is "" in edit
// mode when the user leaves it blank (the sidecar keeps the stored secret).
type ConnEntry = {
  provider: string;
  api_key: string;
  label?: string;
  model?: string;
  base_url?: string;
};

// The two use cases the bot routes through connections, each with a
// primary + backup slot. Forecasting = the per-market call; Research =
// keyword extraction + headline filtering (cheaper models are fine, and
// it falls back to the forecasting model when left unset).
const LLM_ROLE_ROWS: {
  title: string;
  hint: string;
  primary: LLMRole;
  backup: LLMRole;
}[] = [
  {
    title: "Forecasting",
    hint: "Reads every market and produces the forecast. Primary runs first; backup takes over on error or rate-limit.",
    primary: "forecaster_primary",
    backup: "forecaster_backup",
  },
  {
    title: "Research and news",
    hint: "Keyword extraction and headline filtering. Falls back to the forecasting model when left unset. Cheap models work well here.",
    primary: "search_primary",
    backup: "search_backup",
  },
];

// Short badge shown on a connection card for each role it currently holds.
const ROLE_BADGE_LABEL: Record<LLMRole, string> = {
  forecaster_primary: "Forecasting",
  forecaster_backup: "Forecasting backup",
  search_primary: "Research",
  search_backup: "Research backup",
};

function ConnectionsPanel({
  creds,
  onSaved,
  goto,
}: {
  creds: Credentials | null;
  onSaved: () => void;
  goto: Goto;
}) {
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [connections, setConnections] = useState<LLMConnection[]>([]);
  const [roles, setRoles] = useState<LLMRoles>({
    forecaster_primary: null,
    forecaster_backup: null,
    search_primary: null,
    search_backup: null,
  });
  const [llmLoading, setLlmLoading] = useState(true);
  const [llmMsg, setLlmMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [roleBusy, setRoleBusy] = useState(false);

  const reloadLlm = async () => {
    const data = await api.llmConnections();
    setConnections(data.connections);
    setRoles(data.roles);
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [prov, conns] = await Promise.all([
          api.llmProviders(),
          api.llmConnections(),
        ]);
        if (!alive) return;
        setProviders(prov.providers);
        setConnections(conns.connections);
        setRoles(conns.roles);
      } catch (err) {
        if (!alive) return;
        setLlmMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
      } finally {
        if (alive) setLlmLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const providerLabel = (key: string) =>
    providers.find((p) => p.key === key)?.label ?? key;

  const addConnection = async (entry: ConnEntry) => {
    setLlmMsg(null);
    try {
      await api.addLlmConnection(entry);
      await reloadLlm();
      setAdding(false);
      setLlmMsg({ kind: "ok", text: "Connection added." });
      onSaved();
    } catch (err) {
      setLlmMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
      throw err;
    }
  };

  const updateConnection = async (id: string, entry: ConnEntry) => {
    setLlmMsg(null);
    try {
      await api.updateLlmConnection(id, entry);
      await reloadLlm();
      setEditingId(null);
      setLlmMsg({ kind: "ok", text: "Connection updated." });
      onSaved();
    } catch (err) {
      setLlmMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
      throw err;
    }
  };

  const removeConnection = async (id: string) => {
    setBusyId(id);
    setLlmMsg(null);
    try {
      const res = await api.deleteLlmConnection(id);
      setConnections((cs) => cs.filter((c) => c.id !== id));
      setRoles(res.roles);
      setLlmMsg({ kind: "ok", text: "Connection removed." });
      onSaved();
    } catch (err) {
      setLlmMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusyId(null);
    }
  };

  const changeRole = async (role: LLMRole, connId: string | null) => {
    setRoleBusy(true);
    setLlmMsg(null);
    // set_llm_roles REPLACES the whole map on the sidecar, so always send
    // all four keys (the merged map), not just the one that changed.
    const next: LLMRoles = { ...roles, [role]: connId };
    try {
      const res = await api.setLlmRoles(next);
      setRoles(res.roles);
      onSaved();
    } catch (err) {
      setLlmMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setRoleBusy(false);
    }
  };

  const rolesForConn = (id: string): LLMRole[] =>
    (Object.keys(roles) as LLMRole[]).filter((r) => roles[r] === id);

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">LLM connections</h2>
          <HelpHint anchor={HELP_ANCHORS.llm} goto={goto} />
        </div>
        <p className="page-sub" style={{ marginBottom: 16 }}>
          Add a connection for any provider, then assign each one to a use
          case below. Keys are stored locally and never shared.
        </p>

        {llmLoading ? (
          <p className="form-hint">Loading...</p>
        ) : (
          <>
            <div className="conn-list">
              {connections.length === 0 && !adding && (
                <p className="form-hint">
                  No connections yet. Add one to start forecasting.
                </p>
              )}
              {connections.map((c) =>
                editingId === c.id ? (
                  <ConnectionEditor
                    key={c.id}
                    providers={providers}
                    existing={c}
                    onSubmit={(entry) => updateConnection(c.id, entry)}
                    onCancel={() => setEditingId(null)}
                  />
                ) : (
                  <div className="conn-card" key={c.id}>
                    <div className="conn-card-main">
                      <div className="conn-card-title">
                        {c.label || providerLabel(c.provider)}
                      </div>
                      <div className="conn-card-sub">
                        {providerLabel(c.provider)} &middot; {c.model || "default model"}
                        {!c.has_key && (
                          <span className="conn-card-warn"> &middot; no key</span>
                        )}
                      </div>
                      {rolesForConn(c.id).length > 0 && (
                        <div className="conn-card-roles">
                          {rolesForConn(c.id).map((r) => (
                            <span className="conn-role-badge" key={r}>
                              {ROLE_BADGE_LABEL[r]}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="conn-card-actions">
                      <button
                        type="button"
                        className="btn small ghost"
                        onClick={() => {
                          setEditingId(c.id);
                          setAdding(false);
                        }}
                      >
                        Edit
                      </button>
                      <ConfirmButton
                        label="Remove"
                        confirmLabel="Confirm remove"
                        busy={busyId === c.id}
                        onConfirm={() => removeConnection(c.id)}
                      />
                    </div>
                  </div>
                ),
              )}
            </div>

            {adding ? (
              <ConnectionEditor
                providers={providers}
                onSubmit={addConnection}
                onCancel={() => setAdding(false)}
              />
            ) : (
              <button
                type="button"
                className="btn small"
                style={{ marginTop: 12 }}
                onClick={() => {
                  setAdding(true);
                  setEditingId(null);
                }}
              >
                + Add connection
              </button>
            )}

            {connections.length > 0 && (
              <div className="conn-roles">
                <h3 className="form-section-title">Use cases</h3>
                <p className="form-hint" style={{ marginBottom: 12 }}>
                  Choose which connection serves each use case.
                </p>
                <div className="conn-role-selects">
                  {LLM_ROLE_ROWS.map((row) => (
                    <div className="conn-role-row" key={row.title}>
                      <div className="conn-role-row-head">
                        <span className="conn-role-row-title">{row.title}</span>
                        <span className="form-hint">{row.hint}</span>
                      </div>
                      <div className="conn-role-pair">
                        <RoleSelect
                          label="Primary"
                          value={roles[row.primary]}
                          connections={connections}
                          providerLabel={providerLabel}
                          disabled={roleBusy}
                          onChange={(v) => changeRole(row.primary, v)}
                        />
                        <RoleSelect
                          label="Backup"
                          value={roles[row.backup]}
                          connections={connections}
                          providerLabel={providerLabel}
                          disabled={roleBusy}
                          onChange={(v) => changeRole(row.backup, v)}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        {llmMsg && (
          <div className="form-actions" style={{ marginTop: 14 }}>
            <span className={llmMsg.kind === "ok" ? "form-success" : "form-error"}>
              {llmMsg.text}
            </span>
          </div>
        )}
      </div>

      <OtherCredentialsPanel creds={creds} onSaved={onSaved} goto={goto} />
    </>
  );
}

/** Shared add/edit form for one LLM connection. In edit mode the provider
 *  is locked and the API-key field may be left blank to keep the stored
 *  secret. Calls `onSubmit` and keeps itself open if the parent throws. */
function ConnectionEditor({
  providers,
  existing,
  onSubmit,
  onCancel,
}: {
  providers: LLMProvider[];
  existing?: LLMConnection;
  onSubmit: (entry: ConnEntry) => Promise<void>;
  onCancel: () => void;
}) {
  const isEdit = !!existing;
  const [provider, setProvider] = useState(existing?.provider ?? providers[0]?.key ?? "");
  const spec = providers.find((p) => p.key === provider);
  const [label, setLabel] = useState(existing?.label ?? "");
  const [model, setModel] = useState(existing?.model ?? spec?.default_model ?? "");
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? spec?.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Switching provider (add mode only) resets model + base_url to the new
  // provider's defaults so the editor never carries a stale model id.
  const onProviderChange = (key: string) => {
    setProvider(key);
    const next = providers.find((p) => p.key === key);
    setModel(next?.default_model ?? "");
    setBaseUrl(next?.base_url ?? "");
  };

  const submit = async () => {
    setErr(null);
    if (!isEdit && !apiKey.trim()) {
      setErr("API key is required.");
      return;
    }
    if (spec?.custom_base_url && !baseUrl.trim()) {
      setErr("Base URL is required for this provider.");
      return;
    }
    setBusy(true);
    try {
      await onSubmit({
        provider,
        api_key: apiKey.trim(),
        label: label.trim(),
        model: model.trim(),
        base_url: baseUrl.trim(),
      });
    } catch {
      // Parent surfaced the message; keep the editor open for a retry.
    } finally {
      setBusy(false);
    }
  };

  const datalistId = `models-${provider}`;

  return (
    <div className="conn-editor">
      <div className="conn-editor-grid">
        <div className="form-field">
          <label>Provider</label>
          <select
            value={provider}
            disabled={isEdit}
            onChange={(e) => onProviderChange(e.target.value)}
          >
            {providers.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </select>
        </div>

        <div className="form-field">
          <label>Label (optional)</label>
          <input
            type="text"
            autoComplete="off"
            placeholder={spec?.label ?? "My connection"}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
        </div>

        <div className="form-field">
          <label>Model</label>
          <input
            type="text"
            autoComplete="off"
            list={datalistId}
            placeholder={spec?.default_model ?? "model id"}
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
          <datalist id={datalistId}>
            {(spec?.models ?? []).map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </div>

        {spec?.custom_base_url && (
          <div className="form-field">
            <label>Base URL</label>
            <input
              type="text"
              autoComplete="off"
              placeholder="https://api.example.com/v1"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </div>
        )}
      </div>

      <div className="form-field conn-editor-key">
        <label>API key</label>
        <input
          type="password"
          autoComplete="off"
          placeholder={
            isEdit
              ? existing?.has_key
                ? "Leave blank to keep the stored key"
                : "Paste an API key"
              : spec?.key_hint || "Paste your API key"
          }
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
      </div>

      <div className="form-actions">
        <button type="button" className="btn small" disabled={busy} onClick={submit}>
          {busy ? "Saving..." : isEdit ? "Save changes" : "Add connection"}
        </button>
        <button type="button" className="btn small ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
        {err && <span className="form-error">{err}</span>}
      </div>
    </div>
  );
}

/** One labelled dropdown that assigns a connection (or "None") to a role. */
function RoleSelect({
  label,
  value,
  connections,
  providerLabel,
  disabled,
  onChange,
}: {
  label: string;
  value: string | null;
  connections: LLMConnection[];
  providerLabel: (key: string) => string;
  disabled: boolean;
  onChange: (connId: string | null) => void;
}) {
  return (
    <label className="conn-role-select">
      <span className="conn-role-select-label">{label}</span>
      <select
        value={value ?? ""}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">None</option>
        {connections.map((c) => (
          <option key={c.id} value={c.id}>
            {c.label || providerLabel(c.provider)}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Inline two-step confirm button. window.confirm is unreliable in the
 *  Tauri webview, so the first click arms and the second confirms. */
function ConfirmButton({
  label,
  confirmLabel,
  busy,
  onConfirm,
}: {
  label: string;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);

  // Auto-disarm so a stray armed button doesn't linger and get clicked
  // by accident much later.
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);

  if (busy) {
    return (
      <button type="button" className="btn small danger" disabled>
        Removing...
      </button>
    );
  }

  if (!armed) {
    return (
      <button type="button" className="btn small ghost" onClick={() => setArmed(true)}>
        {label}
      </button>
    );
  }

  return (
    <span className="confirm-pair">
      <button
        type="button"
        className="btn small danger"
        onClick={() => {
          setArmed(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </button>
      <button type="button" className="btn small ghost" onClick={() => setArmed(false)}>
        Cancel
      </button>
    </span>
  );
}

/** Non-LLM credentials: Polymarket signing key, relayer, and the optional
 *  research feeds. Each row saves and removes independently. */
function OtherCredentialsPanel({
  creds,
  onSaved,
  goto,
}: {
  creds: Credentials | null;
  onSaved: () => void;
  goto: Goto;
}) {
  const hasNewsapi = creds?.has_newsapi_key ?? false;
  const hasCryptopanic = creds?.has_cryptopanic_key ?? false;
  const hasPmRelayerKey =
    (creds as Record<string, unknown> | null | undefined)?.has_polymarket_relayer_api_key === true;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Other connections</h2>
        <span className="panel-meta">Stored locally</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Polymarket signing and optional research feeds. All keys stay on this
        device and are never shared.
      </p>

      <div className="cred-rows">
        <CredentialRow
          title="Polymarket private key"
          field="polymarket_private_key"
          deleteField="polymarket"
          stored={creds?.has_polymarket_key ?? false}
          storedPlaceholder="(stored)"
          emptyPlaceholder="0x..."
          hint="Signs Polymarket orders in live mode. The wallet address auto-derives from this key. Removing it also clears the derived wallet address."
          helpAnchor={HELP_ANCHORS.polymarketKey}
          goto={goto}
          onSaved={onSaved}
        />
        <CredentialRow
          title="Polymarket Relayer API key"
          field="polymarket_relayer_api_key"
          stored={hasPmRelayerKey}
          storedPlaceholder="(stored)"
          emptyPlaceholder="019d9954-..."
          hint="Enables auto-redeem of winning positions."
          helpAnchor={HELP_ANCHORS.polymarketRelayer}
          goto={goto}
          onSaved={onSaved}
        />
        <CredentialRow
          title="NewsAPI key"
          field="newsapi_key"
          stored={hasNewsapi}
          storedPlaceholder="(stored)"
          emptyPlaceholder="..."
          hint="Headlines for geopolitical, economic, and current-event markets."
          helpAnchor={HELP_ANCHORS.newsapi}
          goto={goto}
          onSaved={onSaved}
        />
        <CredentialRow
          title="CryptoPanic key"
          field="cryptopanic_key"
          stored={hasCryptopanic}
          storedPlaceholder="(stored)"
          emptyPlaceholder="..."
          hint="Crypto-specific news for Polymarket crypto markets."
          helpAnchor={HELP_ANCHORS.cryptopanic}
          goto={goto}
          onSaved={onSaved}
        />
      </div>
    </div>
  );
}

/** Generic single-credential row: password input + Save/Replace, plus a
 *  two-step Remove when a value is already stored. `deleteField` defaults
 *  to `field` (Polymarket overrides it to clear the derived wallet too). */
function CredentialRow({
  title,
  field,
  deleteField,
  stored,
  storedPlaceholder,
  emptyPlaceholder,
  hint,
  helpAnchor,
  goto,
  onSaved,
}: {
  title: string;
  field: string;
  deleteField?: string;
  stored: boolean;
  storedPlaceholder: string;
  emptyPlaceholder: string;
  hint: string;
  helpAnchor?: string;
  goto: Goto;
  onSaved: () => void;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const save = async () => {
    if (!value.trim()) {
      setMsg({ kind: "err", text: "Nothing to save." });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.saveCredentials({ [field]: value.trim() } as Parameters<typeof api.saveCredentials>[0]);
      setValue("");
      setMsg({ kind: "ok", text: "Saved." });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setRemoving(true);
    setMsg(null);
    try {
      await api.deleteCredential(deleteField ?? field);
      setMsg({ kind: "ok", text: "Removed." });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setRemoving(false);
    }
  };

  return (
    <div className="cred-row">
      <div className="form-label-row">
        <label>{title}</label>
        {helpAnchor && <HelpHint anchor={helpAnchor} goto={goto} />}
      </div>
      <div className="cred-row-input">
        <input
          type="password"
          autoComplete="off"
          placeholder={stored ? storedPlaceholder : emptyPlaceholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button type="button" className="btn small" disabled={busy} onClick={save}>
          {busy ? "Saving..." : stored ? "Replace" : "Save"}
        </button>
        {stored && (
          <ConfirmButton
            label="Remove"
            confirmLabel="Confirm remove"
            busy={removing}
            onConfirm={remove}
          />
        )}
      </div>
      <p className="form-hint">{hint}</p>
      {msg && (
        <span
          className={msg.kind === "ok" ? "form-success" : "form-error"}
          style={{ fontSize: 12 }}
        >
          {msg.text}
        </span>
      )}
    </div>
  );
}

// ── Notifications ────────────────────────────────────────────────────────

const CATEGORY_LABELS: Record<string, { title: string; description: string }> = {
  position_opened: {
    title: "New positions",
    description: "When Delfi opens a position. Shows market, side, stake, and forecast.",
  },
  position_settled: {
    title: "Position resolutions",
    description: "Wins and losses when a market resolves. Shows P&L and updated balance.",
  },
  position_closed_early: {
    title: "Early exits",
    description: "Positions closed before resolution by take-profit, stop-loss, or time-decay.",
  },
  position_invalid: {
    title: "Refunded positions",
    description: "Markets that resolve INVALID. The stake is refunded; no P&L.",
  },
  order_error: {
    title: "Order errors",
    description: "Orders rejected by Polymarket before they could fill.",
  },
  order_rejected: {
    title: "Unfilled orders",
    description: "Orders placed on Polymarket that didn't fill in time. No position opened.",
  },
  risk_event: {
    title: "Risk alerts",
    description: "Circuit breaker trips: daily loss limit, maximum drawdown, or consecutive loss cooldown.",
  },
  bot_status: {
    title: "Bot status changes",
    description: "When Delfi pauses or resumes trading, with the reason.",
  },
  bankroll_pause: {
    title: "Low-balance pause",
    description: "When available cash drops below the minimum needed to place a trade.",
  },
  mode_switch: {
    title: "Mode switches",
    description: "When the master Simulation/Live toggle changes.",
  },
  connectivity: {
    title: "Polymarket connection",
    description: "When Delfi loses or regains its connection to Polymarket (geo-block, network outage, VPN drop).",
  },
  trading_blocked: {
    title: "Trading blocked",
    description: "When a forecast provider or market scan failure prevents Delfi from opening new positions.",
  },
  learning_report_ready: {
    title: "Strategy proposals",
    description: "Every 50 settled trades, Delfi reviews performance and may propose a tuning change.",
  },
  daily_summary: {
    title: "Daily summary",
    description: "End-of-day recap of trades, P&L, and running record.",
  },
  weekly_summary: {
    title: "Weekly summary",
    description: "Weekly performance review with win rate and P&L.",
  },
  // Legacy alias kept for users whose stored prefs still reference
  // it. Hidden from the panel by NOTIFICATION_CATEGORIES_VISIBLE on
  // the server side; this entry exists only as a defensive label
  // in case it ever leaks through.
  calibration: {
    title: "Strategy proposals",
    description: "Every 50 settled trades, Delfi reviews performance and may propose a tuning change.",
  },
};

// Fallback for any new category the server adds before this map is
// updated: snake_case -> Title Case so the user never sees a raw
// key like "order_error" again.
function prettifyKey(key: string): string {
  return key
    .split("_")
    .map((w) => (w.length ? w[0].toUpperCase() + w.slice(1) : ""))
    .join(" ");
}

function NotificationsPanel({ goto }: { goto: Goto }) {
  const [notif, setNotif] = useState<NotificationsConfig | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [prefSavingKey, setPrefSavingKey] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const n = await api.notifications();
        if (!cancelled) setNotif(n);
      } catch (err) {
        if (!cancelled) {
          setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
        }
      }
    };
    load();
    return () => { cancelled = true; };
  }, []);

  const togglePref = async (key: string) => {
    if (!notif || prefSavingKey) return;
    const current = notif.notification_prefs[key];
    const next = current === false ? true : false;
    const previous = notif;
    const optimistic: NotificationsConfig = {
      ...notif,
      notification_prefs: { ...notif.notification_prefs, [key]: next },
    };
    setNotif(optimistic);
    setPrefSavingKey(key);
    try {
      const res = await api.saveNotifications(optimistic.notification_prefs);
      setNotif(res);
    } catch (err) {
      setNotif(previous);
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setPrefSavingKey(null);
    }
  };

  const isOn = (key: string): boolean => {
    if (!notif) return true;
    const v = notif.notification_prefs[key];
    return v === undefined ? true : v;
  };

  const categories = notif?.categories?.length
    ? notif.categories
    : Object.keys(CATEGORY_LABELS);

  return (
    <>
      <TelegramConnectorPanel goto={goto} />
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Notification types</h2>
          <span className="panel-meta">Changes apply immediately</span>
        </div>
        <div>
          {categories.map((key) => {
            const label = CATEGORY_LABELS[key] ?? {
              title: prettifyKey(key),
              description: "",
            };
            return (
              <div key={key} className="notif-row">
                <div>
                  <div className="notif-name">{label.title}</div>
                  {label.description && (
                    <div className="notif-desc">{label.description}</div>
                  )}
                </div>
                <label className="toggle-switch">
                  <input
                    type="checkbox"
                    checked={isOn(key)}
                    disabled={prefSavingKey === key}
                    onChange={() => togglePref(key)}
                  />
                  <span className="toggle-slider" />
                </label>
              </div>
            );
          })}
        </div>
        {msg && (
          <p className={msg.kind === "ok" ? "form-success" : "form-error"}
             style={{ marginTop: 12 }}>
            {msg.text}
          </p>
        )}
      </div>
    </>
  );
}

// ── Telegram connector ──────────────────────────────────────────────────

/**
 * Telegram connector card.
 *
 * Push-only outbound connection to a user-supplied Telegram bot.
 * Setup is BYO:
 *   1. User creates a bot via @BotFather on Telegram, gets a token.
 *   2. User starts a chat with their new bot and sends /start.
 *   3. User finds their numeric chat id (e.g. via @userinfobot, or
 *      by visiting `https://api.telegram.org/bot<TOKEN>/getUpdates`).
 *   4. User pastes both into this card and clicks "Test + save". The
 *      sidecar sends a probe message; on success it persists the
 *      pair (token to keychain, chat id to user_config). On failure
 *      nothing is persisted and the user sees Telegram's error.
 *
 * The token is treated as a secret: the GET endpoint returns only
 * `bot_token_configured: boolean`, never the token itself. Disconnect
 * wipes both.
 */
function TelegramConnectorPanel({ goto }: { goto: Goto }) {
  const [tg, setTg] = useState<TelegramConfig | null>(null);
  const [token, setToken] = useState("");
  const [chat, setChat] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api.telegram()
      .then((s) => {
        if (!alive) return;
        setTg(s);
        if (s.chat_id) setChat(s.chat_id);
      })
      .catch(() => alive && setTg(null));
    return () => { alive = false; };
  }, []);

  const isConnected = !!tg?.bot_token_configured && !!tg?.chat_id;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setMsg(null);
    // Save permits a partial update: empty token leaves the saved one
    // alone (the placeholder shows "saved"), empty chat id is the
    // same. Both empty + nothing already saved is a no-op error.
    if (!token.trim() && !chat.trim() && !tg?.bot_token_configured && !tg?.chat_id) {
      setMsg({ kind: "err", text: "Paste your bot token and chat id first." });
      return;
    }
    setBusy(true);
    try {
      const next = await api.saveTelegram(token.trim(), chat.trim());
      setTg(next);
      setToken("");
      setMsg({ kind: "ok", text: "Saved. Click Test to send a probe message." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    if (busy) return;
    setMsg(null);
    // Form values take priority; if the user is testing creds before
    // saving, those flow to the sidecar. If both are blank, the
    // sidecar falls back to whatever's saved.
    const formToken = token.trim();
    const formChat  = chat.trim();
    if (!formToken && !tg?.bot_token_configured) {
      setMsg({ kind: "err", text: "No bot token saved yet. Paste one and click Save first." });
      return;
    }
    if (!formChat && !tg?.chat_id) {
      setMsg({ kind: "err", text: "No chat id saved yet. Paste one and click Save first." });
      return;
    }
    setBusy(true);
    try {
      await api.testTelegram(formToken, formChat);
      setMsg({ kind: "ok", text: "Test sent. Check Telegram." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const next = await api.disconnectTelegram();
      setTg(next);
      setToken("");
      setChat("");
      setMsg({ kind: "ok", text: "Telegram disconnected." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
      setConfirmDisconnect(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Telegram</h2>
        <span className="panel-meta">
          {isConnected ? "Connected" : "Not connected"}
        </span>
      </div>
      <form className="form-row" onSubmit={save}>
        <div className="form-field">
          <div className="form-label-row">
            <label>Bot token</label>
            <HelpHint anchor={HELP_ANCHORS.telegram} goto={goto} />
          </div>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={
              tg?.bot_token_configured
                ? "•••••• (saved)"
                : "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
            }
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        </div>
        <div className="form-field">
          <label>Chat id</label>
          <input
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="123456789"
            value={chat}
            onChange={(e) => setChat(e.target.value)}
          />
        </div>
        <div className="form-actions">
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save"}
          </button>
          <button
            type="button"
            className="btn ghost small"
            onClick={test}
            disabled={busy}
          >
            {busy ? "..." : "Test"}
          </button>
          {isConnected && (
            !confirmDisconnect ? (
              <button
                type="button"
                className="btn ghost small"
                onClick={() => setConfirmDisconnect(true)}
                disabled={busy}
              >
                Disconnect
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className="btn danger small"
                  onClick={disconnect}
                  disabled={busy}
                >
                  Yes, disconnect
                </button>
                <button
                  type="button"
                  className="btn ghost small"
                  onClick={() => setConfirmDisconnect(false)}
                  disabled={busy}
                >
                  Cancel
                </button>
              </>
            )
          )}
        </div>
      </form>

      {msg && (
        <p className={msg.kind === "ok" ? "form-success" : "form-error"}
           style={{ marginTop: 12 }}>
          {msg.text}
        </p>
      )}
    </div>
  );
}
