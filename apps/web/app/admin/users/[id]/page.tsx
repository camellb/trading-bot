"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

type UserPayload = {
  user: {
    user_id:                 string;
    display_name:            string | null;
    email:                   string | null;
    mode:                    string | null;
    starting_cash:           number | null;
    onboarded_at:            string | null;
    created_at:              string | null;
    is_admin:                boolean;
    bot_enabled:             boolean;
    subscription_status:     string | null;
    subscription_plan:       string | null;
    subscription_started_at: string | null;
    has_telegram:            boolean;
    has_polymarket:          boolean;
  };
  summary: {
    open_positions: number;
    settled:        number;
    wins:           number;
    losses:         number;
    win_rate:       number;
    realized_pnl:   number;
    open_cost:      number;
  };
  positions: Array<{
    id:                 number;
    created_at:         string | null;
    market_id:          string | null;
    slug:               string | null;
    question:           string | null;
    category:           string | null;
    market_archetype:   string | null;
    side:               string | null;
    cost_usd:           number | null;
    entry_price:        number | null;
    claude_probability: number | null;
    status:             string | null;
    realized_pnl_usd:   number | null;
    settled_at:         string | null;
  }>;
  events: Array<{
    timestamp:   string | null;
    event_type:  string | null;
    description: string | null;
    severity:    number;
    source:      string | null;
  }>;
};

type Action = "pause_bot" | "resume_bot" | "grant_admin" | "revoke_admin";

function fmtDate(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleDateString("en-US", {
    year: "numeric", month: "short", day: "numeric",
  });
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return `${(v * 100).toFixed(1)}%`;
}

function severityPill(s: number): string {
  if (s >= 3) return "pill-no";
  if (s === 2) return "pill-skip";
  return "pill-open";
}

export default function AdminUserDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [data, setData]       = useState<UserPayload | null>(null);
  const [loaded, setLoaded]   = useState(false);
  const [error, setError]     = useState<string | null>(null);
  const [busy, setBusy]       = useState<Action | null>(null);
  const [note, setNote]       = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await fetch(
          `/api/admin/users/${encodeURIComponent(id)}`,
          { cache: "no-store" },
        );
        if (cancelled) return;
        if (!r.ok) {
          setError(`HTTP ${r.status}: ${await r.text().catch(() => "request failed")}`);
          setData(null);
          return;
        }
        const res = (await r.json()) as UserPayload;
        setData(res);
        setError(null);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load user");
      } finally {
        if (!cancelled) setLoaded(true);
      }
    };
    load();
    return () => { cancelled = true; };
  }, [id, refreshTick]);

  const runAction = useCallback(
    async (action: Action, confirmText: string) => {
      if (!confirm(confirmText)) return;
      setBusy(action);
      setNote(null);
      try {
        const r = await fetch(
          `/api/admin/users/${encodeURIComponent(id)}/action`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ action }),
          },
        );
        if (!r.ok) {
          const txt = await r.text().catch(() => "request failed");
          setNote(`Failed: ${txt}`);
        } else {
          setNote(`${action} applied.`);
          setRefreshTick((n) => n + 1);
        }
      } catch (e: unknown) {
        setNote(e instanceof Error ? e.message : "request failed");
      } finally {
        setBusy(null);
      }
    },
    [id],
  );

  if (!loaded) {
    return (
      <main className="page-wrap">
        <div className="page-head">
          <Link href="/admin/users" className="back-link">← Users</Link>
          <h1 className="page-h1">Loading...</h1>
        </div>
      </main>
    );
  }

  if (error || !data) {
    return (
      <main className="page-wrap">
        <div className="page-head">
          <Link href="/admin/users" className="back-link">← Users</Link>
          <h1 className="page-h1">Could not load user</h1>
          <p className="page-sub">{error || "Unknown error"}</p>
        </div>
      </main>
    );
  }

  const { user, summary, positions, events } = data;
  const name = user.display_name || user.email || user.user_id.slice(0, 8);

  return (
    <main className="page-wrap">
      <div className="page-head">
        <Link href="/admin/users" className="back-link">← Users</Link>
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">{name}</h1>
            <p className="page-sub mono">{user.user_id}</p>
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
            {user.is_admin ? <span className="pill pill-open">admin</span> : null}
            {user.bot_enabled ? (
              <span className="pill pill-open">bot on</span>
            ) : (
              <span className="pill pill-no">bot paused</span>
            )}
            {user.mode ? (
              <span className={`pill ${user.mode === "live" ? "pill-no" : "pill-open"}`}>
                {user.mode === "live" ? "LIVE" : "SIM"}
              </span>
            ) : null}
            {user.subscription_status ? (
              <span className={`pill ${
                user.subscription_status === "active" ? "pill-won"
                : user.subscription_status === "past_due" ? "pill-no"
                : "pill-skip"
              }`}>
                {user.subscription_status}
              </span>
            ) : null}
          </div>
        </div>
      </div>

      <div className="stat-row">
        <div className="stat-cell">
          <div className="stat-cell-label">Realized P&amp;L</div>
          <div className={`stat-cell-val ${
            summary.realized_pnl > 0 ? "cell-up"
            : summary.realized_pnl < 0 ? "cell-down" : ""
          }`}>{fmtMoney(summary.realized_pnl)}</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Open cost</div>
          <div className="stat-cell-val">{fmtMoney(summary.open_cost)}</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Open positions</div>
          <div className="stat-cell-val">{summary.open_positions}</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Settled</div>
          <div className="stat-cell-val">{summary.settled}</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">Win rate</div>
          <div className="stat-cell-val">{fmtPct(summary.win_rate)}</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">W / L</div>
          <div className="stat-cell-val">{summary.wins} / {summary.losses}</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Account</h2>
        </div>
        <div className="split-row">
          <div className="split-body">
            <div className="split-title">Email</div>
            <div className="split-desc">{user.email || "-"}</div>
          </div>
          <div className="split-body">
            <div className="split-title">Joined</div>
            <div className="split-desc">{fmtDate(user.created_at)}</div>
          </div>
          <div className="split-body">
            <div className="split-title">Onboarded</div>
            <div className="split-desc">{fmtDate(user.onboarded_at)}</div>
          </div>
          <div className="split-body">
            <div className="split-title">Plan</div>
            <div className="split-desc">{user.subscription_plan || "-"}</div>
          </div>
          <div className="split-body">
            <div className="split-title">Sub started</div>
            <div className="split-desc">{fmtDate(user.subscription_started_at)}</div>
          </div>
          <div className="split-body">
            <div className="split-title">Starting bankroll</div>
            <div className="split-desc mono">
              {user.starting_cash !== null && user.starting_cash !== undefined
                ? `$${user.starting_cash.toLocaleString()}`
                : "-"}
            </div>
          </div>
          <div className="split-body">
            <div className="split-title">Polymarket</div>
            <div className="split-desc">{user.has_polymarket ? "connected" : "not connected"}</div>
          </div>
          <div className="split-body">
            <div className="split-title">Telegram</div>
            <div className="split-desc">{user.has_telegram ? "connected" : "not connected"}</div>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Admin actions</h2>
        </div>
        <div className="split-row">
          <div className="split-body" style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {user.bot_enabled ? (
              <button
                className="btn-sm danger"
                disabled={busy !== null}
                onClick={() => runAction(
                  "pause_bot",
                  `Pause trading for ${name}? This stops new entries and monitoring.`,
                )}
              >
                {busy === "pause_bot" ? "Pausing..." : "Pause bot"}
              </button>
            ) : (
              <button
                className="btn-sm gold"
                disabled={busy !== null}
                onClick={() => runAction(
                  "resume_bot",
                  `Resume trading for ${name}?`,
                )}
              >
                {busy === "resume_bot" ? "Resuming..." : "Resume bot"}
              </button>
            )}
            {user.is_admin ? (
              <button
                className="btn-sm"
                disabled={busy !== null}
                onClick={() => runAction(
                  "revoke_admin",
                  `Revoke admin from ${name}?`,
                )}
              >
                {busy === "revoke_admin" ? "Revoking..." : "Revoke admin"}
              </button>
            ) : (
              <button
                className="btn-sm"
                disabled={busy !== null}
                onClick={() => runAction(
                  "grant_admin",
                  `Grant admin to ${name}?`,
                )}
              >
                {busy === "grant_admin" ? "Granting..." : "Grant admin"}
              </button>
            )}
          </div>
          {note ? (
            <div className="split-body">
              <div className="split-desc">{note}</div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Recent positions (25)</h2>
        </div>
        {positions.length === 0 ? (
          <div className="split-row">
            <div className="split-body">
              <div className="split-desc">No positions yet.</div>
            </div>
          </div>
        ) : (
          <table className="table-simple">
            <thead>
              <tr>
                <th>When</th>
                <th>Market</th>
                <th>Category</th>
                <th>Side</th>
                <th>Cost</th>
                <th>M YES %</th>
                <th>D YES %</th>
                <th>Status</th>
                <th>P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.id}>
                  <td className="mono">{fmtDateTime(p.created_at)}</td>
                  <td>
                    <div>{p.question || p.slug || p.market_id || "-"}</div>
                    {p.market_archetype ? (
                      <div className="split-desc">{p.market_archetype}</div>
                    ) : null}
                  </td>
                  <td>{p.category || "-"}</td>
                  <td>{p.side || "-"}</td>
                  <td className="mono">{fmtMoney(p.cost_usd)}</td>
                  <td className="mono">
                    {p.entry_price != null && p.side
                      ? `${Math.round((p.side === "YES" ? p.entry_price : 1 - p.entry_price) * 100)}%`
                      : "-"}
                  </td>
                  <td className="mono">
                    {p.claude_probability != null
                      ? `${Math.round(p.claude_probability * 100)}%`
                      : "-"}
                  </td>
                  <td>
                    <span className={`pill ${
                      p.status === "open" ? "pill-open"
                      : (p.realized_pnl_usd ?? 0) > 0 ? "pill-won"
                      : (p.realized_pnl_usd ?? 0) < 0 ? "pill-no"
                      : "pill-skip"
                    }`}>{p.status || "-"}</span>
                  </td>
                  <td className={`mono ${
                    (p.realized_pnl_usd ?? 0) > 0 ? "cell-up"
                    : (p.realized_pnl_usd ?? 0) < 0 ? "cell-down"
                    : ""
                  }`}>
                    {p.status === "settled" ? fmtMoney(p.realized_pnl_usd) : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Recent events (7d, 25)</h2>
        </div>
        {events.length === 0 ? (
          <div className="split-row">
            <div className="split-body">
              <div className="split-desc">No events in the last 7 days.</div>
            </div>
          </div>
        ) : (
          <table className="table-simple">
            <thead>
              <tr>
                <th>When</th>
                <th>Type</th>
                <th>Severity</th>
                <th>Source</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e, i) => (
                <tr key={i}>
                  <td className="mono">{fmtDateTime(e.timestamp)}</td>
                  <td>{e.event_type || "-"}</td>
                  <td><span className={`pill ${severityPill(e.severity)}`}>{e.severity}</span></td>
                  <td>{e.source || "-"}</td>
                  <td>{e.description || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </main>
  );
}
