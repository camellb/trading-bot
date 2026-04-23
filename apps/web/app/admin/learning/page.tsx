"use client";

import { useEffect, useState } from "react";

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

type Payload = {
  user_id:       string;
  include_admin: boolean;
  reports:       Report[];
};

async function getJSON<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

function fmtDate(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-US", {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export default function AdminLearningPage() {
  const [uidInput, setUidInput] = useState("");
  const [activeUid, setActiveUid] = useState<string | null>(null);
  const [data, setData] = useState<Payload | null>(null);
  const [loading, setLoading] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);

  useEffect(() => {
    if (!activeUid) return;
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      const res = await getJSON<Payload>(
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

  const reports = data?.reports ?? [];

  return (
    <main className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Learning reports (admin)</h1>
            <p className="page-sub">
              Inspect a user's review reports with model reasoning excerpts.
              Non-admin callers silently downgrade to the user view.
            </p>
          </div>
        </div>
      </div>

      <div className="page-toolbar">
        <div style={{ flex: 1, maxWidth: 420 }}>
          <input
            className="ob-input"
            placeholder="Enter user_id (UUID)"
            value={uidInput}
            onChange={(e) => setUidInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setActiveUid(uidInput.trim() || null);
            }}
          />
        </div>
        <button
          className="btn-sm"
          onClick={() => setActiveUid(uidInput.trim() || null)}
        >
          Load
        </button>
      </div>

      {!activeUid && (
        <div className="dash-empty-inline">
          Paste a user_id above and press Enter to load their reports.
        </div>
      )}

      {activeUid && loading && (
        <div className="dash-empty-inline">Loading reports for {activeUid}...</div>
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
