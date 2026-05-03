import { ReactNode, useCallback, useEffect, useState } from "react";
import { api, AutostartStatus, BotState, Credentials, isConnectionError } from "./api";
import Dashboard from "./pages/Dashboard";
import Positions from "./pages/Positions";
import PerformancePage from "./pages/Performance";
import Intelligence from "./pages/Intelligence";
import Risk from "./pages/Risk";
import Settings from "./pages/Settings";
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
  | "settings";

export type SettingsTab = "account" | "connections" | "notifications";

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
  const [pendingLiveSwitch, setPendingLiveSwitch] = useState(false);
  const setMode = async (next: "simulation" | "live") => {
    if (modeBusy) return;
    if (state?.mode === next) return;
    if (next === "live") {
      setPendingLiveSwitch(true);
      return;
    }
    await applyMode(next);
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
  const confirmLiveSwitch = async () => {
    setPendingLiveSwitch(false);
    await applyMode("live");
  };
  const cancelLiveSwitch = () => setPendingLiveSwitch(false);

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

  const goto = (p: Page, tab?: SettingsTab) => {
    setPage(p);
    if (p === "settings" && tab) setSettingsTab(tab);
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
      {pendingLiveSwitch && (
        <LiveConfirmModal
          onConfirm={confirmLiveSwitch}
          onCancel={cancelLiveSwitch}
        />
      )}
      <Sidebar
        page={page}
        settingsTab={settingsTab}
        setPage={setPage}
        setSettingsTab={setSettingsTab}
        mode={mode}
        canTradeLive={state?.can_trade_live ?? false}
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
  return <div className="error">{error}</div>;
}

// ── Boot screen ────────────────────────────────────────────────────────

function BootScreen({ error }: { error: string | null }) {
  return (
    <div className="boot">
      <img src="/brand/mark.svg" alt="" className="boot-mark" />
      <h1>DELFI</h1>
      <p className="boot-status">
        {error ? "Delfi could not start" : "Launching..."}
      </p>
      {error ? (
        <p className="boot-detail">{error}</p>
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
  {
    id: "settings",
    label: "Settings",
    icon: IconGear(),
    sub: [
      { id: "account",       label: "Account" },
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

function LiveConfirmModal({
  onConfirm, onCancel,
}: {
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [typed, setTyped] = useState("");
  const enabled = typed === "LIVE";

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

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
        <h2 style={{ margin: "0 0 12px", fontSize: 20 }}>
          Switch to live trading?
        </h2>
        <p style={{ margin: "0 0 16px", color: "var(--vellum-70, #b8b6b1)" }}>
          Delfi will place real Polymarket orders on the next scan
          using the wallet you connected. Make sure you have funded
          the wallet and reviewed your risk settings.
        </p>
        <p style={{
          margin: "0 0 8px",
          color: "var(--vellum-60, #888)",
          fontSize: 13,
        }}>
          Type <strong>LIVE</strong> to confirm.
        </p>
        <input
          type="text"
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && enabled) onConfirm();
          }}
          style={{
            width: "100%",
            padding: "8px 12px",
            background: "var(--obsidian-100, #050505)",
            color: "inherit",
            border: "1px solid var(--obsidian-70, #2a2a2c)",
            borderRadius: 4,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            letterSpacing: "0.1em",
            marginBottom: 16,
          }}
          placeholder="LIVE"
        />
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button
            type="button"
            className="btn ghost small"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn small"
            onClick={onConfirm}
            disabled={!enabled}
            style={enabled ? {} : { opacity: 0.5, cursor: "not-allowed" }}
          >
            Switch to live
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
