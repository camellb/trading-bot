"use client";

import { useEffect, useMemo, useState } from "react";
import { getJSON } from "@/lib/fetch-json";

type AdminUser = {
  user_id:         string;
  display_name:    string | null;
  email:           string | null;
  mode:            string | null;
  onboarded_at:    string | null;
  total_positions: number | null;
  realized_pnl:    number | null;
};

type UsersPayload = { users: AdminUser[] };

type Report = {
  id:            number;
  created_at:    string | null;
  mode:          string | null;
  settled_count: number | null;
  thesis:        string | null;
  summary_user:  string | null;
  summary_admin: string | null;
  data:          Record<string, unknown> | null;
};

type ReportsPayload = {
  user_id:       string;
  include_admin: boolean;
  reports:       Report[];
};

function fmtDate(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-US", {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function userLabel(u: AdminUser): string {
  const bits: string[] = [];
  if (u.display_name) bits.push(u.display_name);
  if (u.email) bits.push(u.email);
  if (bits.length === 0) bits.push(u.user_id.slice(0, 8));
  const trades = u.total_positions ?? 0;
  return `${bits.join(" - ")}  (${trades} trades)`;
}

export default function AdminLearningPage() {
  const [users, setUsers]       = useState<AdminUser[] | null>(null);
  const [usersLoaded, setUL]    = useState(false);
  const [activeUid, setActive]  = useState<string | null>(null);
  const [data, setData]         = useState<ReportsPayload | null>(null);
  const [loading, setLoading]   = useState(false);
  const [openId, setOpenId]     = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const res = await getJSON<UsersPayload>("/api/admin/users");
      if (cancelled) return;
      setUsers(res?.users ?? []);
      setUL(true);
    };
    load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!activeUid) {
      setData(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      const res = await getJSON<ReportsPayload>(
        `/api/learning-reports?user_id=${encodeURIComponent(activeUid)}` +
        `&include_admin=1&limit=20`,
      );
      if (cancelled) return;
      setData(res);
      setLoading(false);
    };
    load();
    return () => { cancelled = true; };
  }, [activeUid]);

  const sortedUsers = useMemo(() => {
    const list = users ?? [];
    return [...list].sort((a, b) => {
      const ap = a.total_positions ?? 0;
      const bp = b.total_positions ?? 0;
      if (ap !== bp) return bp - ap;
      const ae = (a.email || a.display_name || a.user_id).toLowerCase();
      const be = (b.email || b.display_name || b.user_id).toLowerCase();
      return ae.localeCompare(be);
    });
  }, [users]);

  const reports = data?.reports ?? [];

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Learning reports (admin)</h1>
            <p className="page-sub">
              Pick a user to inspect their 50-trade review reports, including
              model reasoning excerpts. Non-admin callers silently downgrade
              to the user view.
            </p>
          </div>
        </div>
      </div>

      <div className="page-toolbar" style={{ gap: 12, alignItems: "center" }}>
        <label className="page-sub" style={{ margin: 0 }}>
          User:
        </label>
        <select
          className="ob-input"
          style={{ flex: 1, maxWidth: 520 }}
          value={activeUid ?? ""}
          onChange={(e) => setActive(e.target.value || null)}
          disabled={!usersLoaded}
        >
          <option value="">
            {usersLoaded
              ? (sortedUsers.length > 0
                  ? "Select a user..."
                  : "No users found")
              : "Loading users..."}
          </option>
          {sortedUsers.map((u) => (
            <option key={u.user_id} value={u.user_id}>
              {userLabel(u)}
            </option>
          ))}
        </select>
      </div>

      {activeUid && loading && (
        <div className="dash-empty-inline">Loading reports...</div>
      )}

      {activeUid && !loading && data && !data.include_admin && (
        <div className="dash-empty-inline" style={{ color: "#b45309" }}>
          Warning: include_admin is false. Your account is not flagged as
          admin, so reasoning excerpts are hidden.
        </div>
      )}

      {activeUid && !loading && reports.length === 0 && (
        <div className="dash-empty-inline">
          No reports on file for this user.
        </div>
      )}

      {activeUid && !loading && reports.length > 0 && (
        <section className="intel-section">
          <div className="intel-list">
            {reports.map((r) => (
              <AdminReportCard
                key={r.id}
                report={r}
                expanded={openId === r.id}
                onToggle={() => setOpenId(openId === r.id ? null : r.id)}
              />
            ))}
          </div>
        </section>
      )}
    </main>
  );
}

function AdminReportCard({
  report, expanded, onToggle,
}: {
  report:   Report;
  expanded: boolean;
  onToggle: () => void;
}) {
  const mode = (report.mode || "simulation").toUpperCase();
  const body = report.summary_admin || report.summary_user || "";
  return (
    <article className="intel-card">
      <header className="intel-card-head">
        <div className="intel-card-date">{fmtDate(report.created_at)}</div>
        <div className="intel-card-status pending">{mode}</div>
      </header>

      <div className="intel-card-param">
        #{report.id} - {report.settled_count ?? 0} settled trades
      </div>

      {report.thesis && (
        <p className="intel-card-evidence" style={{ marginTop: 8 }}>
          {report.thesis}
        </p>
      )}

      <button
        type="button"
        onClick={onToggle}
        className="intel-card-foot"
        style={{
          background: "none", border: 0, padding: 0, cursor: "pointer",
          textAlign: "left", width: "100%",
        }}
      >
        {expanded ? "Hide full report" : "Show full report (with reasoning)"}
      </button>

      {expanded && body && (
        <pre
          style={{
            whiteSpace: "pre-wrap",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 12,
            marginTop: 12,
            padding: 12,
            background: "rgba(0,0,0,0.04)",
            borderRadius: 6,
            overflowX: "auto",
          }}
        >
          {body}
        </pre>
      )}
    </article>
  );
}
