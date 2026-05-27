import { useEffect, useRef, useState } from "react";
import { save as saveDialog } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  api,
  AutostartStatus,
  Credentials,
  LaunchStats,
  LicenseStatus,
  LogTail,
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
          The starting cash Delfi treats as 100% of capital. Stake size and
          circuit breakers are computed against this number.
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
          Clears all simulation positions and resets the synthetic capital
          to your starting cash. Live trading is untouched.
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
    </>
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
      <LogsPanel />
      <LaunchStatsPanel />
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
        If something looks broken and Restart Delfi below didn't fix it,
        email us and we'll respond.
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
        <span className="panel-meta">
          Currently: {tz || `system (${resolvedTz()})`}
        </span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Every date and time across the dashboard renders in this
        timezone. Default is your operating system clock; pick a
        specific zone if you want the dashboard to follow a different
        clock than the OS (e.g. trading hours in another region).
      </p>
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
      <p className="page-sub" style={{ marginBottom: 16 }}>
        When on, Delfi launches automatically every time you sign in to
        your computer. Trading resumes without you having to open the app
        manually. Turning it off means Delfi only runs when you open it.
      </p>
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
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Adds Delfi to your macOS Login Items so the dashboard window
        opens automatically when you sign in. Independent of
        auto-start: Delfi can run headlessly without the window
        opening.
      </p>
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
            ? "The bot keeps trading. We're just bouncing the daemon and reconnecting."
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
        <span className="panel-meta">macOS</span>
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

// ── Logs viewer ─────────────────────────────────────────────────────────

/** Read-only tail of the daemon's stdout / stderr log files (the
 *  StandardOutPath / StandardErrorPath wired in the LaunchAgent
 *  plist). Lets the user diagnose issues without opening Terminal. */
function LogsPanel() {
  const [tail, setTail] = useState<LogTail | null>(null);
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (s: "stdout" | "stderr") => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.logs(s, 200);
      setTail(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Activity log</h2>
        <span className="panel-meta">last 200 lines</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Recent activity from Delfi. The error log captures crashes
        and warnings; the activity log shows routine operations.
      </p>
      <div className="form-actions" style={{ marginBottom: 12 }}>
        <button
          type="button"
          className={`btn small ${stream === "stdout" ? "" : "ghost"}`}
          onClick={() => { setStream("stdout"); load("stdout"); }}
          disabled={busy}
        >
          Activity
        </button>
        <button
          type="button"
          className={`btn small ${stream === "stderr" ? "" : "ghost"}`}
          onClick={() => { setStream("stderr"); load("stderr"); }}
          disabled={busy}
        >
          Errors
        </button>
        <button
          type="button"
          className="btn ghost small"
          onClick={() => load(stream)}
          disabled={busy}
        >
          {busy ? "Loading..." : "Refresh"}
        </button>
      </div>
      {error && <div className="error">{error}</div>}
      {tail && (
        <>
          {tail.note && (
            <p className="page-sub" style={{ marginBottom: 8 }}>{tail.note}</p>
          )}
          <pre style={{
            background: "var(--obsidian-90, #0a0a0a)",
            color: "var(--vellum-90, #e8e6e1)",
            border: "1px solid var(--obsidian-80, #1a1a1a)",
            borderRadius: 4,
            padding: "12px 16px",
            margin: 0,
            maxHeight: 360,
            overflow: "auto",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 12,
            lineHeight: 1.55,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}>
            {tail.lines.length === 0 ? "(empty)" : tail.lines.join("\n")}
          </pre>
        </>
      )}
      {!tail && !busy && !error && (
        <p className="page-sub">Click Activity or Errors to load.</p>
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
 *   1. Bootstrap a new machine in one click — export on old, import
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
        Save your risk limits, archetype multipliers, skip list, and
        notification prefs to a portable JSON file. Use it to bootstrap
        another machine, share a strategy with another Delfi user, or
        roll back after experimenting. API keys, wallet keys, and
        trade history are never included.
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
        Export a consistent snapshot of every position, evaluation,
        and config row to a SQLite file you choose. Uses VACUUM INTO
        so the backup is safe to take while Delfi is trading. Save
        these somewhere outside the app (cloud sync folder, external
        drive) so you can recover after a disk failure.
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

// ── Launch stats ────────────────────────────────────────────────────────

/** Read-only diagnostic panel surfacing what launchd thinks is
 *  going on with the daemon: current PID, total respawn count,
 *  last exit code, state. High respawn counts or a nonzero last
 *  exit code mean something's misbehaving. */
function LaunchStatsPanel() {
  const [stats, setStats] = useState<LaunchStats | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.launchStats();
      setStats(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { load(); }, []);

  // Hide on platforms without launchd stats. macOS's launchctl `print`
  // is the only thing surfacing these counters today; on Windows the
  // respawn loop lives inside the Tauri shell and doesn't track them
  // yet. Showing an empty "Stats are macOS-only" tile is worse than
  // not showing the panel at all.
  if (stats && stats.supported === false) {
    return null;
  }
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Reliability</h2>
        <button
          type="button"
          className="btn ghost small"
          onClick={load}
          disabled={busy}
        >
          {busy ? "..." : "Refresh"}
        </button>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        How Delfi has been behaving since it was installed. The run
        count goes up every time Delfi starts (login, recovery from
        a crash, or a manual restart). A non-zero exit code means
        the last run ended unexpectedly.
      </p>
      {error && <div className="error">{error}</div>}
      {stats && stats.supported === false && (
        <p className="page-sub">Reliability stats are macOS-only.</p>
      )}
      {stats && stats.supported && (
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: 24,
          maxWidth: 720,
        }}>
          <Stat label="Status" value={stats.state ?? "-"} />
          <Stat label="Total runs" value={stats.runs != null ? String(stats.runs) : "-"} />
          <Stat
            label="Last exit code"
            value={stats.last_exit_code != null ? String(stats.last_exit_code) : "-"}
          />
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{
        fontSize: 11,
        textTransform: "uppercase",
        letterSpacing: "0.1em",
        color: "var(--vellum-60, #888)",
      }}>{label}</div>
      <div style={{
        fontSize: 22,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        marginTop: 4,
      }}>{value}</div>
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

  const lastValidated = status?.last_validated_at
    ? formatDateTime(status.last_validated_at)
    : null;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">License</h2>
      </div>
      {status?.has_key ? (
        <p className="page-sub" style={{ marginBottom: 16 }}>
          This machine is activated. Last validated {lastValidated || "never"}.
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

function ConnectionsPanel({
  creds,
  onSaved,
  goto,
}: {
  creds: Credentials | null;
  onSaved: () => void;
  goto: Goto;
}) {
  const [pmKey, setPmKey] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [llmBackup, setLlmBackup] = useState("");
  const [newsapi, setNewsapi] = useState("");
  const [cryptopanic, setCryptopanic] = useState("");
  const [gemini, setGemini] = useState("");
  const [pmApiKey, setPmApiKey] = useState("");
  const [pmApiSecret, setPmApiSecret] = useState("");
  const [pmApiPass, setPmApiPass] = useState("");
  const [pmRelayerKey, setPmRelayerKey] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // Older sidecars don't return `has_llm_key`; fall back to the legacy
  // `has_anthropic_key` so the "(stored)" placeholder is correct on
  // either version.
  const hasLlm = creds?.has_llm_key ?? creds?.has_anthropic_key ?? false;
  const hasLlmBackup = creds?.has_llm_backup_key ?? false;
  const hasNewsapi = creds?.has_newsapi_key ?? false;
  const hasCryptopanic = creds?.has_cryptopanic_key ?? false;
  const hasGemini = (creds as Record<string, unknown> | null | undefined)?.has_gemini_key === true;
  const hasPmApiKey  = (creds as Record<string, unknown> | null | undefined)?.has_polymarket_api_key === true;
  const hasPmApiSec  = (creds as Record<string, unknown> | null | undefined)?.has_polymarket_api_secret === true;
  const hasPmApiPass = (creds as Record<string, unknown> | null | undefined)?.has_polymarket_api_passphrase === true;
  const hasPmRelayerKey = (creds as Record<string, unknown> | null | undefined)?.has_polymarket_relayer_api_key === true;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const payload: Record<string, string> = {};
      if (pmKey.trim())       payload.polymarket_private_key = pmKey.trim();
      if (llmKey.trim())      payload.llm_api_key = llmKey.trim();
      if (llmBackup.trim())   payload.llm_backup_key = llmBackup.trim();
      if (newsapi.trim())     payload.newsapi_key = newsapi.trim();
      if (cryptopanic.trim()) payload.cryptopanic_key = cryptopanic.trim();
      if (gemini.trim())      payload.gemini_key = gemini.trim();
      if (pmApiKey.trim())    payload.polymarket_api_key = pmApiKey.trim();
      if (pmApiSecret.trim()) payload.polymarket_api_secret = pmApiSecret.trim();
      if (pmApiPass.trim())   payload.polymarket_api_passphrase = pmApiPass.trim();
      if (pmRelayerKey.trim()) payload.polymarket_relayer_api_key = pmRelayerKey.trim();
      if (Object.keys(payload).length === 0) {
        setMsg({ kind: "err", text: "Nothing to save." });
        return;
      }
      const res = await api.saveCredentials(payload as Parameters<typeof api.saveCredentials>[0]);
      setPmKey("");
      setLlmKey("");
      setLlmBackup("");
      setNewsapi("");
      setCryptopanic("");
      setGemini("");
      setPmApiKey("");
      setPmApiSecret("");
      setPmApiPass("");
      setPmRelayerKey("");
      setMsg({ kind: "ok", text: `Saved: ${res.wrote.join(", ") || "nothing"}.` });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Credentials</h2>
        <span className="panel-meta">Stored in OS keychain</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        All keys live in your operating system keychain. Never on disk and
        never sent to Delfi servers. Leaving a field blank keeps the existing
        value.
      </p>
      <form className="form-row" onSubmit={save}>
        <div className="form-field">
          <div className="form-label-row">
            <label>Polymarket private key</label>
            <HelpHint anchor={HELP_ANCHORS.polymarketKey} goto={goto} />
          </div>
          <input
            type="password"
            autoComplete="off"
            placeholder={creds?.has_polymarket_key ? "(stored)" : "0x..."}
            value={pmKey}
            onChange={(e) => setPmKey(e.target.value)}
          />
          <p className="form-hint">
            Signs Polymarket orders in live mode. The wallet address auto-derives from this key.
          </p>
        </div>

        <div className="form-field">
          <div className="form-label-row">
            <label>LLM API key</label>
            <HelpHint anchor={HELP_ANCHORS.llm} goto={goto} />
          </div>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasLlm ? "(stored)" : "Paste your LLM API key"}
            value={llmKey}
            onChange={(e) => setLlmKey(e.target.value)}
          />
          <p className="form-hint">
            The forecaster that reads each market.
          </p>
        </div>

        <div className="form-field">
          <div className="form-label-row">
            <label>Backup LLM API key</label>
            <HelpHint anchor={HELP_ANCHORS.llmBackup} goto={goto} />
          </div>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasLlmBackup ? "(stored)" : "Paste a second LLM API key"}
            value={llmBackup}
            onChange={(e) => setLlmBackup(e.target.value)}
          />
          <p className="form-hint">
            Used when the primary LLM errors or rate-limits.
          </p>
        </div>

        <div className="form-field">
          <div className="form-label-row">
            <label>Search LLM API key</label>
            <HelpHint anchor={HELP_ANCHORS.searchLlm} goto={goto} />
          </div>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasGemini ? "(stored)" : "Paste a Search LLM API key"}
            value={gemini}
            onChange={(e) => setGemini(e.target.value)}
          />
          <p className="form-hint">
            Used for keyword extraction and headline filtering. Cheap models recommended.
          </p>
        </div>

        <div className="form-field">
          <div className="form-label-row">
            <label>NewsAPI key</label>
            <HelpHint anchor={HELP_ANCHORS.newsapi} goto={goto} />
          </div>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasNewsapi ? "(stored)" : "..."}
            value={newsapi}
            onChange={(e) => setNewsapi(e.target.value)}
          />
          <p className="form-hint">
            Headlines for geopolitical, economic, and current-event markets.
          </p>
        </div>

        <div className="form-field">
          <div className="form-label-row">
            <label>CryptoPanic key</label>
            <HelpHint anchor={HELP_ANCHORS.cryptopanic} goto={goto} />
          </div>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasCryptopanic ? "(stored)" : "..."}
            value={cryptopanic}
            onChange={(e) => setCryptopanic(e.target.value)}
          />
          <p className="form-hint">
            Crypto-specific news for Polymarket crypto markets.
          </p>
        </div>

        <div className="form-field">
          <div className="form-label-row">
            <label>Polymarket Relayer API key</label>
            <HelpHint anchor={HELP_ANCHORS.polymarketRelayer} goto={goto} />
          </div>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasPmRelayerKey ? "(stored)" : "019d9954-..."}
            value={pmRelayerKey}
            onChange={(e) => setPmRelayerKey(e.target.value)}
          />
          <p className="form-hint">
            Enables auto-redeem of winning positions.
          </p>
        </div>

        <div className="form-field">
          <label>Gemini API key (optional)</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasGemini ? "(stored)" : "AIzaSy..."}
            value={gemini}
            onChange={(e) => setGemini(e.target.value)}
          />
          <p className="form-hint">
            Used for fast keyword extraction and headline pre-filtering.
            Without it, Delfi falls back to raw RSS titles (still works,
            but research is noisier). Free at aistudio.google.com.
          </p>
        </div>

        <div className="form-field">
          <label>Polymarket API key (optional)</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasPmApiKey ? "(stored)" : "leave blank to auto-derive"}
            value={pmApiKey}
            onChange={(e) => setPmApiKey(e.target.value)}
          />
          <p className="form-hint">
            Normally Delfi derives this automatically from your private
            key. If you&apos;ve gone through Polymarket&apos;s V2
            account upgrade and orders keep being rejected, generate
            a fresh key at polymarket.com &rarr; Settings &rarr;
            Relayer API keys and paste all three fields (key + secret
            + passphrase) here.
          </p>
        </div>

        <div className="form-field">
          <label>Polymarket API secret (optional)</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasPmApiSec ? "(stored)" : ""}
            value={pmApiSecret}
            onChange={(e) => setPmApiSecret(e.target.value)}
          />
        </div>

        <div className="form-field">
          <label>Polymarket API passphrase (optional)</label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasPmApiPass ? "(stored)" : ""}
            value={pmApiPass}
            onChange={(e) => setPmApiPass(e.target.value)}
          />
        </div>

        <div className="form-actions">
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save credentials"}
          </button>
          {msg && (
            <span className={msg.kind === "ok" ? "form-success" : "form-error"}>
              {msg.text}
            </span>
          )}
        </div>
      </form>
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
    description: "Circuit breaker trips: daily loss cap, drawdown halt, or streak cooldown.",
  },
  bot_status: {
    title: "Bot status changes",
    description: "When Delfi pauses or resumes trading, with the reason.",
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

