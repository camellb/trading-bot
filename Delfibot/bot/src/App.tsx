import { ReactNode, useCallback, useEffect, useState } from "react";
import { api, BotState, Credentials } from "./api";
import Dashboard from "./pages/Dashboard";
import Positions from "./pages/Positions";
import PerformancePage from "./pages/Performance";
import Intelligence from "./pages/Intelligence";
import Settings from "./pages/Settings";
import Onboarding from "./Onboarding";

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
  | "settings";

export type SettingsTab = "account" | "connections" | "risk" | "notifications";

export default function App() {
  const [page, setPage] = useState<Page>("dashboard");
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("account");
  const [state, setState] = useState<BotState | null>(null);
  const [creds, setCreds] = useState<Credentials | null>(null);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState<boolean>(false);
  const [modeBusy, setModeBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [s, c, cfg] = await Promise.all([
        api.state(),
        api.credentials(),
        api.config(),
      ]);
      setState(s);
      setCreds(c);
      setConfig(cfg);
      setConnected(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5_000);
    return () => clearInterval(id);
  }, [refresh]);

  const setMode = async (next: "simulation" | "live") => {
    if (modeBusy) return;
    if (state?.mode === next) return;
    setModeBusy(true);
    try {
      if (next === "live") await api.start();
      else await api.stop();
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

  // First-launch wizard: gate the main shell behind onboarding so a fresh
  // install can't end up on the Dashboard before bankroll + creds exist.
  if (state && state.is_onboarded === false) {
    return <Onboarding state={state} creds={creds} onComplete={refresh} />;
  }

  const mode = (state?.mode as "simulation" | "live") ?? "simulation";

  return (
    <div className="app-shell">
      <Sidebar
        page={page}
        settingsTab={settingsTab}
        setPage={setPage}
        setSettingsTab={setSettingsTab}
        mode={mode}
        canTradeLive={state?.can_trade_live ?? false}
        setMode={setMode}
        modeBusy={modeBusy}
      />
      <main className="app-main">
        {error && <div className="error">{error}</div>}
        {page === "dashboard" && (
          <Dashboard state={state} refresh={refresh} goto={goto} />
        )}
        {page === "positions" && <Positions />}
        {page === "performance" && <PerformancePage />}
        {page === "intelligence" && <Intelligence />}
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
  );
}

// ── Boot screen ────────────────────────────────────────────────────────

function BootScreen({ error }: { error: string | null }) {
  return (
    <div className="boot">
      <img src="/brand/mark.svg" alt="" className="boot-mark" />
      <h1>DELFI</h1>
      <p className="boot-status">
        {error ? "Could not reach the local engine" : "Launching..."}
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
  {
    id: "settings",
    label: "Settings",
    icon: IconGear(),
    sub: [
      { id: "account",       label: "Account" },
      { id: "connections",   label: "Connections" },
      { id: "risk",          label: "Risk controls" },
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
  setMode,
  modeBusy,
}: {
  page: Page;
  settingsTab: SettingsTab;
  setPage: (p: Page) => void;
  setSettingsTab: (t: SettingsTab) => void;
  mode: "simulation" | "live";
  canTradeLive: boolean;
  setMode: (m: "simulation" | "live") => void;
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
        setMode={setMode}
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
  setMode,
  modeBusy,
}: {
  mode: "simulation" | "live";
  canTradeLive: boolean;
  setMode: (m: "simulation" | "live") => void;
  modeBusy: boolean;
}) {
  const isLive = mode === "live";
  return (
    <div className={`bot-pill ${isLive ? "on" : ""}`}>
      <div className="bot-pill-row">
        <span className="bot-pill-label">Status</span>
        <span className="bot-pill-status">
          <span className={`bot-pill-dot ${isLive ? "on" : "off"}`} />
          <span className="bot-pill-state">{isLive ? "LIVE" : "SIMULATION"}</span>
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
function IconGear() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.36.15.68.4.9.74" />
    </svg>
  );
}
