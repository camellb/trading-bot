"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { getJSON } from "@/lib/fetch-json";

type AdminUser = {
  user_id:             string;
  display_name:        string | null;
  email:               string | null;
  mode:                string | null;
  starting_cash:       number | null;
  onboarded_at:        string | null;
  created_at:          string | null;
  is_admin:            boolean;
  bot_enabled:         boolean;
  subscription_status: string | null;
  subscription_plan:   string | null;
  total_positions:     number | null;
  realized_pnl:        number | null;
};

type UsersPayload = { users: AdminUser[] };

function fmtDate(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleDateString("en-US", {
    year: "numeric", month: "short", day: "numeric",
  });
}

function fmtMoney(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

function planLabel(plan: string | null): string {
  if (plan === "monthly") return "Monthly";
  if (plan === "annual") return "Annual";
  if (plan === "legacy") return "Legacy";
  return "-";
}

function subStatusPill(status: string | null): { label: string; klass: string } {
  switch (status) {
    case "active":    return { label: "active",    klass: "pill-won"  };
    case "past_due":  return { label: "past due",  klass: "pill-no"   };
    case "canceled":  return { label: "canceled",  klass: "pill-skip" };
    case "none":
    default:          return { label: "none",      klass: "pill-skip" };
  }
}

export default function AdminUsersPage() {
  const [users, setUsers]     = useState<AdminUser[] | null>(null);
  const [loaded, setLoaded]   = useState(false);
  const [error, setError]     = useState<string | null>(null);
  const [q, setQ]             = useState("");
  const [subFilter, setSub]   = useState<"all" | "active" | "none">("all");

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await fetch("/api/admin/users", { cache: "no-store" });
        if (cancelled) return;
        if (!r.ok) {
          setError(`HTTP ${r.status}: ${await r.text().catch(() => "request failed")}`);
          setUsers([]);
          return;
        }
        const res = (await r.json()) as UsersPayload;
        setUsers(res?.users ?? []);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load users");
        setUsers([]);
      } finally {
        if (!cancelled) setLoaded(true);
      }
    };
    load();
    return () => { cancelled = true; };
  }, []);

  const summary = useMemo(() => {
    const list = users ?? [];
    const active    = list.filter((u) => u.subscription_status === "active").length;
    const pastDue   = list.filter((u) => u.subscription_status === "past_due").length;
    const onboarded = list.filter((u) => !!u.onboarded_at).length;
    const admins    = list.filter((u) => u.is_admin).length;
    return { total: list.length, active, pastDue, onboarded, admins };
  }, [users]);

  const filtered = useMemo(() => {
    const list = users ?? [];
    const lq = q.trim().toLowerCase();
    return list.filter((u) => {
      if (subFilter !== "all") {
        const s = (u.subscription_status ?? "none").toLowerCase();
        if (subFilter === "active" && s !== "active") return false;
        if (subFilter === "none" && s === "active") return false;
      }
      if (!lq) return true;
      return (
        (u.email?.toLowerCase().includes(lq) ?? false) ||
        (u.display_name?.toLowerCase().includes(lq) ?? false) ||
        u.user_id.toLowerCase().includes(lq)
      );
    });
  }, [users, q, subFilter]);

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Users</h1>
            <p className="page-sub">All accounts, their subscription, and their current state.</p>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div style={{ flex: 1, maxWidth: 360 }}>
          <input
            className="ob-input"
            placeholder="Search by email, name, or id"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <div className="tab-bar" style={{ padding: 0, margin: 0 }}>
          {(["all", "active", "none"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setSub(k)}
              className={`tab ${subFilter === k ? "on" : ""}`}
              style={{ textTransform: "capitalize" }}
            >
              {k === "none" ? "Unsubscribed" : k}
            </button>
          ))}
        </div>
      </div>

      {loaded && !error ? (
        <div className="stat-row">
          <div className="stat-cell">
            <div className="stat-cell-label">Total</div>
            <div className="stat-cell-val">{summary.total.toLocaleString()}</div>
          </div>
          <div className="stat-cell">
            <div className="stat-cell-label">Active</div>
            <div className="stat-cell-val">{summary.active.toLocaleString()}</div>
          </div>
          <div className="stat-cell">
            <div className="stat-cell-label">Past due</div>
            <div className="stat-cell-val">{summary.pastDue.toLocaleString()}</div>
          </div>
          <div className="stat-cell">
            <div className="stat-cell-label">Onboarded</div>
            <div className="stat-cell-val">{summary.onboarded.toLocaleString()}</div>
          </div>
          <div className="stat-cell">
            <div className="stat-cell-label">Admins</div>
            <div className="stat-cell-val">{summary.admins.toLocaleString()}</div>
          </div>
        </div>
      ) : null}

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">
            {loaded
              ? `${filtered.length} of ${(users ?? []).length} users`
              : "Loading users..."}
          </h2>
        </div>

        {error ? (
          <div className="split-row">
            <div className="split-body">
              <div className="split-title">Could not load users</div>
              <div className="split-desc">{error}</div>
              <div className="split-desc" style={{ marginTop: 6, fontSize: 12 }}>
                The bot API may be waking up (Railway cold start). Refresh in a few seconds.
              </div>
            </div>
          </div>
        ) : !loaded ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">Loading...</div></div></div>
        ) : (users ?? []).length === 0 ? (
          <div className="split-row">
            <div className="split-body">
              <div className="split-title">No users yet</div>
              <div className="split-desc">
                The query returned zero rows. Either no one has signed up, or the bot API is still waking up. If the overview shows users but this page does not, force-refresh in a moment.
              </div>
            </div>
          </div>
        ) : filtered.length === 0 ? (
          <div className="split-row"><div className="split-body"><div className="split-desc">No users match your filter.</div></div></div>
        ) : (
          <table className="table-simple">
            <thead>
              <tr>
                <th>User</th>
                <th>Plan</th>
                <th>Subscription</th>
                <th>Mode</th>
                <th>Bankroll</th>
                <th>P&amp;L</th>
                <th>Trades</th>
                <th>Joined</th>
                <th>Role</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((u) => {
                const sub = subStatusPill(u.subscription_status);
                const name = u.display_name || u.email || u.user_id.slice(0, 8);
                return (
                  <tr key={u.user_id}>
                    <td>
                      <Link
                        href={`/admin/users/${encodeURIComponent(u.user_id)}`}
                        className="row-link"
                      >
                        <div>{name}</div>
                        {u.email && u.display_name ? (
                          <div className="split-desc">{u.email}</div>
                        ) : null}
                      </Link>
                    </td>
                    <td>{planLabel(u.subscription_plan)}</td>
                    <td>
                      <span className={`pill ${sub.klass}`}>{sub.label}</span>
                    </td>
                    <td>
                      {u.mode ? (
                        <span className={`pill ${u.mode === "live" ? "pill-no" : "pill-open"}`}>
                          {u.mode === "live" ? "LIVE" : "SIM"}
                        </span>
                      ) : "-"}
                    </td>
                    <td className="mono">
                      {u.starting_cash !== null && u.starting_cash !== undefined
                        ? `$${u.starting_cash.toLocaleString()}`
                        : "-"}
                    </td>
                    <td className={`mono ${
                      (u.realized_pnl ?? 0) > 0 ? "cell-up"
                      : (u.realized_pnl ?? 0) < 0 ? "cell-down"
                      : ""
                    }`}>
                      {fmtMoney(u.realized_pnl)}
                    </td>
                    <td className="mono">{u.total_positions ?? 0}</td>
                    <td className="mono">{fmtDate(u.created_at)}</td>
                    <td>
                      {u.is_admin ? <span className="pill pill-open">admin</span> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </main>
  );
}
