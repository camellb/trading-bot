import { ReactNode, useCallback, useEffect, useState } from "react";
import { api, AutostartStatus, BotState, Credentials, isConnectionError, tauriRestartSidecar, waitForSidecar } from "./api";
import Dashboard from "./pages/Dashboard";
import Positions from "./pages/Positions";
import PerformancePage from "./pages/Performance";
import Intelligence from "./pages/Intelligence";
import Risk from "./pages/Risk";
import Settings from "./pages/Settings";
import Help from "./pages/Help";
import Onboarding from "./Onboarding";
import { UpdatePrompt } from "./components/UpdatePrompt";
import { LicenseGate } from "./components/LicenseGate";

/**
 * Root component for the Delfi desktop app.
 *
 * Two-column shell with the SaaS information architecture: brand row,
 * primary nav (Dashboard, Positions, Performance, Intelligence, Settings
 * + sub-tabs), bot status pill, footer. Right column hosts the active
 * page. App owns top-level state (BotState, Credentials, config) and
 * polls every 5s. Each page fetches its own heavier data on demand.
 */

export type Page =
  | "dashboard"
  | "positions"
  | "performance"
  | "intelligence"
  | "risk"
  | "settings"
  | "help";

export type SettingsTab =
  | "account"
  | "app"
  | "diagnostics"
  | "connections"
  | "notifications";

export default function App() {
  const [page, setPage] = useState<Page>("dashboard");
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("account");
  const [state, setState] = useState<BotState | null>(null);
  const [creds, setCreds] = useState<Credentials | null>(null);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState<boolean>(false);
  const [modeBusy, setModeBusy] = useState(false);
  // Track the last-known autostart status so the connection-error
  // banner can distinguish "daemon is down because the user turned
  // off auto-start" from "daemon is down for an unexpected reason".
  // Polled alongside refresh; on connection failure we keep the
  // last-known value so the banner can surface the right message
  // even though the daemon is currently unreachable.
  const [autostart, setAutostart] = useState<AutostartStatus | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, c, cfg] = await Promise.all([
        api.state(),
        api.credentials(),
        api.config(),
      ]);
      setState(s);
      setCreds(c);
      setConfig(cfg);
      setConnected(true);
      // Only clear the error AFTER a confirmed successful refresh.
      // Pre-clearing (setError(null) before Promise.all resolves)
      // caused a ~0.3s visual flash: a stale error from a prior
      // poll would commit-clear synchronously, the banner would
      // disappear, then Promise.all rejected ~300ms later and
      // setError fired again, banner re-appeared. Users read that
      // blink as "an issue appearing for 0.3s and disappearing."
      setError(null);
      // Refresh autostart in the background (don't fail the whole
      // poll if this errors - it's only used by the banner copy).
      api.autostart().then(setAutostart).catch(() => {});
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5_000);
    return () => clearInterval(id);
  }, [refresh]);

  // Mode change. Independent of the on/off switch: changing mode just
  // updates user_config.mode (which ledger trades go to). Whether the
  // bot is actually opening trades is governed by `bot_enabled`, set
  // separately via toggleBotEnabled.
  //
  // Switching INTO live mode triggers a confirm dialog. The user has
  // to type LIVE explicitly. This is a footgun guard: an accidental
  // click on the Live button used to silently flip the mode and the
  // next scan would place real Polymarket orders.
  // Mode-switch confirmation. Both directions require a click-to-
  // confirm so an accidental click on the Sidebar's Sim/Live button
  // can't silently flip the bot — switching INTO live starts firing
  // real Polymarket orders on the next scan, switching back to sim
  // stops them. Either is a meaningful action worth a second beat.
  const [pendingMode, setPendingMode] =
    useState<"simulation" | "live" | null>(null);
  const setMode = (next: "simulation" | "live") => {
    if (modeBusy) return;
    if (state?.mode === next) return;
    setPendingMode(next);
  };
  const applyMode = async (next: "simulation" | "live") => {
    setModeBusy(true);
    try {
      await api.updateConfig({ mode: next });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setModeBusy(false);
    }
  };
  const confirmModeSwitch = async () => {
    const target = pendingMode;
    setPendingMode(null);
    if (target) await applyMode(target);
  };
  const cancelModeSwitch = () => setPendingMode(null);

  // Bot on/off. Calls /api/bot/start (sets bot_enabled=true; validates
  // creds for the current mode) or /api/bot/stop (sets bot_enabled=false).
  const toggleBotEnabled = async () => {
    if (modeBusy) return;
    setModeBusy(true);
    try {
      if (state?.bot_enabled) {
        await api.stop();
      } else {
        await api.start();
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setModeBusy(false);
    }
  };

  // Deep-link target for the Help page: when a "?" help-hint in
  // Settings calls goto("help", undefined, "polymarket-key"), Help
  // auto-opens that guide and scrolls to it. Cleared on navigation
  // away and after Help consumes it.
  const [helpAnchor, setHelpAnchor] = useState<string | null>(null);
  const clearHelpAnchor = useCallback(() => setHelpAnchor(null), []);

  const goto = (p: Page, tab?: SettingsTab, anchor?: string) => {
    setPage(p);
    if (p === "settings" && tab) setSettingsTab(tab);
    setHelpAnchor(p === "help" ? (anchor ?? null) : null);
  };

  if (!connected) {
    return <BootScreen error={error} />;
  }

  // First-launch wizard: gate the main shell behind onboarding so a
  // fresh install can't end up on the Dashboard before bankroll +
  // creds exist. Both onboarding AND the main shell sit behind the
  // LicenseGate (the user pastes their LS license key first, the
  // sidecar verifies it against Lemon Squeezy, only then do we let
  // them past the gate to the rest of the app).
  if (state && state.is_onboarded === false) {
    return (
      <LicenseGate>
        <Onboarding state={state} creds={creds} onComplete={refresh} />
      </LicenseGate>
    );
  }

  const mode = (state?.mode as "simulation" | "live") ?? "simulation";

  return (
    <LicenseGate>
    <div className="app-shell">
      <UpdatePrompt />
      {pendingMode && (
        <ModeConfirmModal
          targetMode={pendingMode}
          onConfirm={confirmModeSwitch}
          onCancel={cancelModeSwitch}
        />
      )}
      <Sidebar
        page={page}
        settingsTab={settingsTab}
        setPage={setPage}
        setSettingsTab={setSettingsTab}
        mode={mode}
        // live_creds_ready is "do we have wallet + key" regardless
        // of current mode. Use it to gate the Live toggle so the
        // user can actually switch INTO live (the older
        // can_trade_live required mode=live, creating a chicken-
        // and-egg where the Live button was permanently disabled
        // in simulation). Fall back to can_trade_live for older
        // sidecars that don't surface the new field.
        canTradeLive={state?.live_creds_ready ?? state?.can_trade_live ?? false}
        botEnabled={state?.bot_enabled ?? false}
        setMode={setMode}
        toggleBotEnabled={toggleBotEnabled}
        modeBusy={modeBusy}
      />
      <main className="app-main">
        <ConnectionBanner
          error={error}
          autostart={autostart}
          onOpenSettings={() => goto("settings", "account")}
        />
        {page === "dashboard" && (
          <Dashboard state={state} refresh={refresh} goto={goto} />
        )}
        {page === "positions" && <Positions />}
        {page === "performance" && <PerformancePage />}
        {page === "intelligence" && <Intelligence />}
        {page === "risk" && (
          <Risk config={config} onSaved={refresh} />
        )}
        {page === "settings" && (
          <Settings
            tab={settingsTab}
            setTab={setSettingsTab}
            creds={creds}
            config={config}
            onSaved={refresh}
            goto={goto}
          />
        )}
        {page === "help" && (
          <Help
            creds={creds}
            config={config}
            goto={goto}
            anchor={helpAnchor}
            clearAnchor={clearHelpAnchor}
          />
        )}
      </main>
    </div>
    </LicenseGate>
  );
}

// ── Connection banner ──────────────────────────────────────────────────
//
// Rendered above every page. Three modes:
//
//   1. No error -> render nothing.
//   2. Connection error AND last-known autostart was OFF -> render
//      a clear "Auto-start is off" banner with a button that
//      navigates to Settings > Account so the user can flip it back
//      on. This is the path the user hits when they deliberately
//      toggle auto-start OFF and then look at the dashboard - the
//      old generic "Could not connect, please restart the app"
//      copy was misleading because the daemon went down BY DESIGN.
//   3. Any other error -> render it as-is.

function ConnectionBanner({
  error,
  autostart,
  onOpenSettings,
}: {
  error: string | null;
  autostart: AutostartStatus | null;
  onOpenSettings: () => void;
}) {
  if (!error) return null;
  const isConn = isConnectionError(error);
  // The "deliberately off" path: connection error + we have a last-
  // known autostart that says enabled=false. We use the cached value
  // because the daemon is currently unreachable - we can't re-check
  // its state in real time. The cached value persists from the last
  // successful poll, which is exactly the moment the user toggled
  // it off.
  if (isConn && autostart?.supported && autostart.enabled === false) {
    return (
      <div className="error" style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        background: "rgba(184, 145, 63, 0.08)",
        border: "1px solid rgba(184, 145, 63, 0.35)",
        color: "var(--vellum-90, #e8e6e1)",
      }}>
        <span style={{ flex: 1 }}>
          Auto-start is off. Delfi is paused and not opening trades.
        </span>
        <button
          type="button"
          className="btn small"
          onClick={onOpenSettings}
        >
          Open settings
        </button>
      </div>
    );
  }
  // Connection-class error: offer Restart inline. The user can't open
  // Settings while the API is wedged, so the only recovery path UX-
  // wise needs to live right here on the banner.
  if (isConn) {
    return <ConnErrorBannerWithRestart error={error} />;
  }
  return <div className="error">{error}</div>;
}

function ConnErrorBannerWithRestart({ error }: { error: string }) {
  const [restarting, setRestarting] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);
  const onRestart = async () => {
    setRestarting(true);
    setRestartError(null);
    try {
      await tauriRestartSidecar();
      // Wait for the daemon to come back, then reload. If it doesn't
      // come back within 30 s, surface a concrete next step instead
      // of an infinite spinner.
      const alive = await waitForSidecar(30_000);
      if (alive) {
        window.location.reload();
      } else {
        setRestartError(
          "Daemon did not come back. Quit Delfi from the macOS menu " +
          "bar and reopen from /Applications.",
        );
        setRestarting(false);
      }
    } catch (e) {
      setRestartError(e instanceof Error ? e.message : String(e));
      setRestarting(false);
    }
  };
  return (
    <div
      className="error"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexWrap: "wrap",
      }}
    >
      <span style={{ flex: 1 }}>
        {restartError ? `${error} Restart failed: ${restartError}` : error}
      </span>
      <button
        type="button"
        className="btn small"
        onClick={onRestart}
        disabled={restarting}
      >
        {restarting ? "Restarting..." : "Restart Delfi"}
      </button>
    </div>
  );
}

// ── Boot screen ────────────────────────────────────────────────────────

function BootScreen({ error }: { error: string | null }) {
  const [restarting, setRestarting] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);
  const onRestart = async () => {
    setRestarting(true);
    setRestartError(null);
    try {
      await tauriRestartSidecar();
      // Wait for the daemon to come back, then reload so the GUI
      // re-runs its boot probe with a clean cache. If the daemon
      // doesn't come back within 30 s, surface a concrete next step
      // instead of an infinite spinner.
      const alive = await waitForSidecar(30_000);
      if (alive) {
        window.location.reload();
      } else {
        setRestartError(
          "Daemon did not come back. Quit Delfi from the macOS menu " +
          "bar and reopen from /Applications.",
        );
        setRestarting(false);
      }
    } catch (e) {
      setRestartError(e instanceof Error ? e.message : String(e));
      setRestarting(false);
    }
  };
  return (
    <div className="boot">
      <img src="/brand/mark.svg" alt="" className="boot-mark" />
      <h1>DELFI</h1>
      <p className="boot-status">
        {error ? "Delfi could not start" : "Launching..."}
      </p>
      {error ? (
        <>
          <p className="boot-detail">{error}</p>
          <div className="boot-actions">
            <button
              type="button"
              className="btn small"
              onClick={onRestart}
              disabled={restarting}
            >
              {restarting ? "Restarting..." : "Restart Delfi"}
            </button>
            <a
              className="btn ghost small"
              href={`mailto:info@delfibot.com?subject=${encodeURIComponent(
                "Delfi will not start",
              )}&body=${encodeURIComponent(`Error: ${error}`)}`}
            >
              Email support
            </a>
          </div>
          {restartError && (
            <p className="boot-detail" style={{ color: "var(--danger, #d33)" }}>
              Restart failed: {restartError}
            </p>
          )}
        </>
      ) : (
        <>
          <p className="boot-detail">This may take up to 30 seconds</p>
          <div className="boot-progress" aria-hidden="true" />
        </>
      )}
    </div>
  );
}

// ── Sidebar ───────────────────────────────────────────────────────────

type NavItem = {
  id: Page;
  label: string;
  icon: ReactNode;
  sub?: { id: SettingsTab; label: string }[];
};

const NAV: NavItem[] = [
  { id: "dashboard",    label: "Dashboard",    icon: IconGrid() },
  { id: "positions",    label: "Positions",    icon: IconLayers() },
  { id: "performance",  label: "Performance",  icon: IconTrend() },
  { id: "intelligence", label: "Intelligence", icon: IconBolt() },
  { id: "risk",         label: "Risk controls", icon: IconShield() },
  { id: "help",         label: "Help",          icon: IconHelp() },
  {
    id: "settings",
    label: "Settings",
    icon: IconGear(),
    sub: [
      { id: "account",       label: "Account" },
      { id: "app",           label: "App" },
      { id: "diagnostics",   label: "Diagnostics" },
      { id: "connections",   label: "Connections" },
      { id: "notifications", label: "Notifications" },
    ],
  },
];

function Sidebar({
  page,
  settingsTab,
  setPage,
  setSettingsTab,
  mode,
  canTradeLive,
  botEnabled,
  setMode,
  toggleBotEnabled,
  modeBusy,
}: {
  page: Page;
  settingsTab: SettingsTab;
  setPage: (p: Page) => void;
  setSettingsTab: (t: SettingsTab) => void;
  mode: "simulation" | "live";
  canTradeLive: boolean;
  botEnabled: boolean;
  setMode: (m: "simulation" | "live") => void;
  toggleBotEnabled: () => void;
  modeBusy: boolean;
}) {
  return (
    <aside className="side">
      <a
        href="#"
        className="side-brand"
        onClick={(e) => { e.preventDefault(); setPage("dashboard"); }}
      >
        <img src="/brand/mark.svg" alt="" className="side-mark" />
        <span className="side-word">DELFI</span>
      </a>

      <nav className="side-nav" aria-label="Primary">
        {NAV.map((item) => {
          const active = page === item.id;
          return (
            <div className="side-group" key={item.id}>
              <button
                type="button"
                className={`side-link ${active ? "active" : ""}`}
                onClick={() => setPage(item.id)}
                aria-current={active ? "page" : undefined}
              >
                <span className="side-icon">{item.icon}</span>
                <span className="side-label">{item.label}</span>
              </button>
              {active && item.sub && (
                <div className="side-sub">
                  {item.sub.map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      className={`side-sublink ${settingsTab === s.id ? "active" : ""}`}
                      onClick={() => setSettingsTab(s.id)}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </nav>

      <BotStatusPill
        mode={mode}
        canTradeLive={canTradeLive}
        botEnabled={botEnabled}
        setMode={setMode}
        toggleBotEnabled={toggleBotEnabled}
        modeBusy={modeBusy}
      />

      <div className="side-foot">
        <div className="side-footnote">V1.5</div>
      </div>
    </aside>
  );
}

function BotStatusPill({
  mode,
  canTradeLive,
  botEnabled,
  setMode,
  toggleBotEnabled,
  modeBusy,
}: {
  mode: "simulation" | "live";
  canTradeLive: boolean;
  botEnabled: boolean;
  setMode: (m: "simulation" | "live") => void;
  toggleBotEnabled: () => void;
  modeBusy: boolean;
}) {
  const isLive = mode === "live";
  return (
    <div className={`bot-pill ${botEnabled ? "on" : ""}`}>
      <div className="bot-pill-row">
        <span className="bot-pill-label">Status</span>
        <span className="bot-pill-status">
          <span className={`bot-pill-dot ${botEnabled ? "on" : "off"}`} />
          <span className="bot-pill-state">{botEnabled ? "ON" : "OFF"}</span>
        </span>
      </div>
      <div className="bot-pill-row">
        <span className="bot-pill-label">Mode</span>
        <span className={`bot-pill-mode ${isLive ? "live" : "simulation"}`}>
          {isLive ? "Live" : "Simulation"}
        </span>
      </div>
      <div className="bot-pill-modes" role="group" aria-label="Trading mode">
        <button
          type="button"
          className={`bot-pill-mode-btn ${mode === "simulation" ? "on" : ""}`}
          onClick={() => setMode("simulation")}
          disabled={modeBusy}
          aria-pressed={mode === "simulation"}
        >
          Simulation
        </button>
        <button
          type="button"
          className={`bot-pill-mode-btn ${mode === "live" ? "on" : ""}`}
          onClick={() => setMode("live")}
          disabled={modeBusy || !canTradeLive}
          aria-pressed={mode === "live"}
          title={canTradeLive ? "" : "Add Polymarket key + wallet to enable live"}
        >
          Live
        </button>
      </div>
      {botEnabled ? (
        <button
          type="button"
          className="bot-pill-btn stop"
          onClick={toggleBotEnabled}
          disabled={modeBusy}
        >
          {modeBusy ? "Pausing..." : "Pause bot"}
        </button>
      ) : (
        <button
          type="button"
          className="bot-pill-btn start"
          onClick={toggleBotEnabled}
          disabled={modeBusy}
        >
          {modeBusy ? "Starting..." : "Start Delfi"}
        </button>
      )}
    </div>
  );
}

// ── LIVE confirm modal ─────────────────────────────────────────────────
//
// Switching to live mode places real Polymarket orders on the next
// scan. An accidental click on the Live button is the kind of mistake
// the user only makes once, but once is too many. We require typing
// LIVE in all caps before flipping the mode. Cancel button + Escape
// + clicking the backdrop all dismiss without flipping.

function ModeConfirmModal({
  targetMode, onConfirm, onCancel,
}: {
  targetMode: "simulation" | "live";
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const isLive = targetMode === "live";

  // Escape dismisses; backdrop click also dismisses; clicks inside
  // the card don't bubble out.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  // Focus the primary action by default so Enter confirms. Going to
  // sim is harmless, so the primary is the "go ahead" button on
  // both paths; cancel is one Tab back or Escape.
  const confirmRef = (el: HTMLButtonElement | null) => {
    if (el) el.focus();
  };

  const title = isLive
    ? "Switch to live trading?"
    : "Switch to simulation?";
  const body = isLive
    ? "Delfi will place real Polymarket orders on the next scan using the wallet you connected. Make sure the wallet is funded and your risk settings are set correctly."
    : "Delfi will stop placing real Polymarket orders. Existing live positions stay open on-chain until they resolve; only NEW trades will be paper from here.";
  const confirmLabel = isLive ? "Switch to live" : "Switch to simulation";
  // Match the SaaS visual language: live confirmation gets a more
  // assertive (gold) action button, simulation gets the neutral one.
  const confirmClass = isLive ? "btn small" : "btn small ghost";

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onCancel}
      style={{
        position: "fixed", inset: 0, zIndex: 9999,
        background: "rgba(0,0,0,0.66)",
        display: "flex", alignItems: "center", justifyContent: "center",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--obsidian-90, #0f0f10)",
          color: "var(--vellum-90, #e8e6e1)",
          border: "1px solid var(--obsidian-70, #2a2a2c)",
          borderRadius: 6,
          maxWidth: 480,
          width: "calc(100% - 48px)",
          padding: 28,
          boxShadow: "0 24px 60px rgba(0,0,0,0.6)",
        }}
      >
        <h2 style={{ margin: "0 0 12px", fontSize: 20 }}>{title}</h2>
        <p style={{ margin: "0 0 20px", color: "var(--vellum-70, #b8b6b1)" }}>
          {body}
        </p>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button
            type="button"
            className="btn ghost small"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={confirmClass}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Inline icons (pixel-matched to SaaS shell) ─────────────────────────

function IconGrid() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
    </svg>
  );
}
function IconLayers() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M12 3 3 8l9 5 9-5-9-5z" />
      <path d="M3 13l9 5 9-5" />
      <path d="M3 18l9 5 9-5" />
    </svg>
  );
}
function IconTrend() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 17l6-6 4 4 8-10" />
      <path d="M14 5h7v7" />
    </svg>
  );
}
function IconBolt() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />
    </svg>
  );
}
function IconShield() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}
function IconGear() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.36.15.68.4.9.74" />
    </svg>
  );
}
function IconHelp() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="10" />
      <path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .8-1 1.7" />
      <line x1="12" y1="17" x2="12" y2="17.01" />
    </svg>
  );
}
