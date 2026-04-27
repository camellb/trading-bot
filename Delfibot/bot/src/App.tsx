import { useCallback, useEffect, useState } from "react";
import { api, BotState, Credentials, PMPosition, EventLogRow } from "./api";
import Settings from "./Settings";

type Tab = "overview" | "settings";

/**
 * Root component. Two-tab shell: overview (status, positions, recent
 * events) and settings (credentials, bankroll, risk and sizing).
 *
 * State is owned at the root and pushed down. The 5-second refresh loop
 * keeps every panel synced with the sidecar; saves in Settings call
 * `refresh()` immediately so the UI reflects the new values without
 * waiting for the next tick.
 */
export default function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const [state, setState] = useState<BotState | null>(null);
  const [creds, setCreds] = useState<Credentials | null>(null);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [positions, setPositions] = useState<PMPosition[]>([]);
  const [events, setEvents] = useState<EventLogRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState<boolean>(false);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [s, c, cfg, p, e] = await Promise.all([
        api.state(),
        api.credentials(),
        api.config(),
        api.positions(50).then((r) => r.positions),
        api.events(50).then((r) => r.events),
      ]);
      setState(s);
      setCreds(c);
      setConfig(cfg);
      setPositions(p);
      setEvents(e);
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

  const onStart = async () => {
    try { await api.start(); refresh(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };
  const onStop = async () => {
    try { await api.stop(); refresh(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };
  const onScan = async () => {
    try { await api.scan(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  // First-launch boot screen. The bundled sidecar can take up to ~30s to
  // decompress its Python interpreter on cold launch, and the Rust shell
  // waits up to 120s for the ready handshake. Showing a clear startup
  // state (instead of an empty shell with "..." placeholders) avoids the
  // "is it broken?" moment.
  if (!connected) {
    return (
      <main className="app">
        <div className="boot">
          <h1>Delfi</h1>
          <p className="boot-status">
            {error ? "Could not reach the local engine" : "Starting the local engine..."}
          </p>
          {error ? (
            <p className="boot-detail">{error}</p>
          ) : (
            <p className="boot-detail">First launch can take up to 30 seconds while the sidecar unpacks.</p>
          )}
        </div>
      </main>
    );
  }

  return (
    <main className="app">
      <header className="app-header">
        <h1>Delfi</h1>
        <span className="mode-pill" data-mode={state?.mode ?? "unknown"}>
          {state?.mode ?? "..."}
        </span>
        <nav className="tabs">
          <button
            className={tab === "overview" ? "tab active" : "tab"}
            onClick={() => setTab("overview")}
          >
            Overview
          </button>
          <button
            className={tab === "settings" ? "tab active" : "tab"}
            onClick={() => setTab("settings")}
          >
            Settings
          </button>
        </nav>
      </header>

      {error && <div className="error">{error}</div>}

      {tab === "overview" && (
        <section className="grid">
          <div className="card">
            <h2>Status</h2>
            <dl>
              <dt>Mode</dt><dd>{state?.mode ?? "-"}</dd>
              <dt>Bankroll</dt><dd>${(state?.starting_cash ?? 0).toFixed(2)}</dd>
              <dt>Wallet</dt><dd className="mono">{state?.wallet_address ?? "(not set)"}</dd>
              <dt>Live ready</dt><dd>{state?.can_trade_live ? "yes" : "no"}</dd>
              <dt>Uptime</dt><dd>{Math.round((state?.uptime_s ?? 0))}s</dd>
              <dt>Errors</dt><dd>{state?.error_count ?? 0}</dd>
            </dl>
            <div className="actions">
              <button onClick={onStart} disabled={!state?.can_trade_live}>Go live</button>
              <button onClick={onStop}>Stop (sim)</button>
              <button onClick={onScan}>Scan now</button>
            </div>
          </div>

          <div className="card">
            <h2>Credentials</h2>
            <dl>
              <dt>Polymarket key</dt><dd>{creds?.has_polymarket_key ? "in keychain" : "missing"}</dd>
              <dt>Anthropic key</dt><dd>{creds?.has_anthropic_key ? "in keychain" : "missing"}</dd>
              <dt>Wallet</dt><dd className="mono">{creds?.wallet_address ?? "(not set)"}</dd>
            </dl>
            <p className="hint">
              Keys live in the OS keychain (Keychain on macOS, Credential Manager
              on Windows, Secret Service on Linux). They never touch the SQLite DB.
            </p>
          </div>

          <div className="card wide">
            <h2>Positions ({positions.length})</h2>
            {positions.length === 0 ? (
              <p className="empty">No positions yet.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Question</th><th>Side</th><th>Shares</th>
                    <th>Entry</th><th>Status</th><th>Mode</th><th>P&amp;L</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr key={p.id}>
                      <td title={p.question}>{p.question.slice(0, 60)}</td>
                      <td>{p.side}</td>
                      <td>{p.shares.toFixed(2)}</td>
                      <td>${p.entry_price.toFixed(3)}</td>
                      <td>{p.status}</td>
                      <td>{p.mode}</td>
                      <td>{p.realized_pnl_usd !== null ? `$${p.realized_pnl_usd.toFixed(2)}` : "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="card wide">
            <h2>Recent events</h2>
            {events.length === 0 ? (
              <p className="empty">No events yet.</p>
            ) : (
              <ul className="events">
                {events.slice(0, 15).map((e) => (
                  <li key={e.id}>
                    <span className="ts">{new Date(e.timestamp).toLocaleTimeString()}</span>
                    <span className={`evt sev-${e.severity ?? 0}`}>{e.event_type}</span>
                    <span className="src">{e.source}</span>
                    <span className="desc">{e.description}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      )}

      {tab === "settings" && (
        <Settings creds={creds} config={config} onSaved={refresh} />
      )}
    </main>
  );
}
