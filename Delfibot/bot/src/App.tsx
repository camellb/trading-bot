import { useCallback, useEffect, useState } from "react";
import { api, BotState, Credentials, PMPosition, EventLogRow } from "./api";

/**
 * Root component. Intentionally minimal: this is the scaffold that
 * proves the Tauri to Python sidecar handshake works end-to-end.
 * Real product UI lands in dedicated components later.
 */
export default function App() {
  const [state, setState] = useState<BotState | null>(null);
  const [creds, setCreds] = useState<Credentials | null>(null);
  const [positions, setPositions] = useState<PMPosition[]>([]);
  const [events, setEvents] = useState<EventLogRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [s, c, p, e] = await Promise.all([
        api.state(),
        api.credentials(),
        api.positions(50).then((r) => r.positions),
        api.events(50).then((r) => r.events),
      ]);
      setState(s);
      setCreds(c);
      setPositions(p);
      setEvents(e);
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

  return (
    <main className="app">
      <header className="app-header">
        <h1>Delfi</h1>
        <span className="mode-pill" data-mode={state?.mode ?? "unknown"}>
          {state?.mode ?? "..."}
        </span>
      </header>

      {error && <div className="error">{error}</div>}

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
    </main>
  );
}
