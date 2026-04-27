import { ReactNode, useCallback, useEffect, useState } from "react";
import { api, BotState, Credentials } from "./api";
import Dashboard from "./pages/Dashboard";
import Positions from "./pages/Positions";
import PerformancePage from "./pages/Performance";
import Intelligence from "./pages/Intelligence";
import Settings from "./pages/Settings";

/**
 * Root component for the Delfi desktop app.
 *
 * Layout
 * ======
 * Two-column shell. Left sidebar carries the brand, primary nav, mode
 * toggle, and a status pill. Right column hosts the current page. The
 * sidebar mirrors the SaaS dashboard's information architecture so the
 * experience is identical regardless of where Delfi runs.
 *
 * State ownership
 * ===============
 * App owns the lightweight top-level state every page wants to see at a
 * glance: BotState (mode, can_trade_live, uptime, error count) and
 * Credentials (presence flags + wallet address). Both are refreshed
 * every 5 seconds and on demand via `refresh()`.
 *
 * Heavier per-page data (positions, evaluations, performance summary,
 * suggestions, learning reports) is fetched inside each page so we
 * don't waste cycles polling for things the user can't see. Each page
 * gets `mode` and a `refresh()` callback; that's enough to keep their
 * panels coherent with the global state.
 *
 * Boot screen
 * ===========
 * The bundled Python sidecar takes up to ~30 seconds to decompress on a
 * first launch (PyInstaller unpacks the interpreter to a tempdir). The
 * Rust shell waits up to 120s for the ready handshake. Showing a clean
 * boot screen instead of an empty shell with placeholders avoids the
 * "is it broken?" moment.
 */

export type Page =
  | "dashboard"
  | "positions"
  | "performance"
  | "intelligence"
  | "settings";

export default function App() {
  const [page, setPage] = useState<Page>("dashboard");
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

  if (!connected) {
    return <BootScreen error={error} />;
  }

  return (
    <div className="shell">
      <Sidebar
        page={page}
        setPage={setPage}
        mode={(state?.mode as "simulation" | "live") ?? "simulation"}
        canTradeLive={state?.can_trade_live ?? false}
        setMode={setMode}
        modeBusy={modeBusy}
      />
      <main className="main">
        {error && <div className="error">{error}</div>}
        {page === "dashboard" && (
          <Dashboard state={state} refresh={refresh} />
        )}
        {page === "positions" && <Positions />}
        {page === "performance" && <PerformancePage />}
        {page === "intelligence" && <Intelligence />}
        {page === "settings" && (
          <Settings creds={creds} config={config} onSaved={refresh} />
        )}
      </main>
    </div>
  );
}

// ── Boot screen ────────────────────────────────────────────────────────

function BootScreen({ error }: { error: string | null }) {
  return (
    <div className="boot">
      <img src="/src/assets/brand/mark.svg" alt="" className="mark" />
      <h1>DELFI</h1>
      <p className="boot-status">
        {error
          ? "Could not reach the local engine"
          : "Starting the local engine..."}
      </p>
      {error ? (
        <p className="boot-detail">{error}</p>
      ) : (
        <p className="boot-detail">
          First launch can take up to 30 seconds while the sidecar unpacks.
        </p>
      )}
    </div>
  );
}

// ── Sidebar ───────────────────────────────────────────────────────────

const NAV: Array<{ id: Page; label: string; icon: ReactNode }> = [
  { id: "dashboard",    label: "Dashboard",    icon: IconGrid() },
  { id: "positions",    label: "Positions",    icon: IconLayers() },
  { id: "performance",  label: "Performance",  icon: IconTrend() },
  { id: "intelligence", label: "Intelligence", icon: IconBolt() },
  { id: "settings",     label: "Settings",     icon: IconGear() },
];

function Sidebar({
  page,
  setPage,
  mode,
  canTradeLive,
  setMode,
  modeBusy,
}: {
  page: Page;
  setPage: (p: Page) => void;
  mode: "simulation" | "live";
  canTradeLive: boolean;
  setMode: (m: "simulation" | "live") => void;
  modeBusy: boolean;
}) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <img src="/src/assets/brand/mark.svg" alt="" width={28} height={28} />
        <span className="sidebar-brand-text">DELFI</span>
      </div>

      <nav className="sidebar-nav">
        {NAV.map((item) => (
          <button
            key={item.id}
            className={`nav-item ${page === item.id ? "active" : ""}`}
            onClick={() => setPage(item.id)}
            type="button"
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        <div className="status-pill" data-mode={mode}>
          <span className={`live-dot ${mode === "live" ? "profit" : ""}`} />
          {mode}
        </div>
        <div className="mode-toggle">
          <button
            type="button"
            className={mode === "simulation" ? "on" : ""}
            onClick={() => setMode("simulation")}
            disabled={modeBusy}
          >
            Sim
          </button>
          <button
            type="button"
            className={mode === "live" ? "on" : ""}
            onClick={() => setMode("live")}
            disabled={modeBusy || !canTradeLive}
            title={
              canTradeLive ? "" : "Add Polymarket key + wallet to enable live"
            }
          >
            Live
          </button>
        </div>
      </div>
    </aside>
  );
}

// ── Inline sidebar icons (pixel-matched to SaaS shell) ─────────────────

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
