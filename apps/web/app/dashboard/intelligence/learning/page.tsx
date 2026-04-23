"use client";

import { useEffect, useState } from "react";
import "../../../styles/content.css";

type Report = {
  id:            number;
  created_at:    string | null;
  mode:          string | null;
  settled_count: number | null;
  thesis:        string | null;
  summary_user:  string | null;
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

export default function LearningReportsPage() {
  const [data, setData]     = useState<Payload | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const res = await getJSON<Payload>("/api/learning-reports?limit=20");
      if (cancelled) return;
      setData(res);
      setLoaded(true);
    };
    load();
    const id = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const reports = data?.reports ?? [];

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Learning reports</h1>
            <p className="page-sub">
              Every 50 closed trades Delfi writes a review: what moved the book,
              where calibration held or drifted, and what the next cycle will
              focus on. Reports arrive here automatically and in your Telegram.
            </p>
          </div>
        </div>
      </div>

      {!loaded && <div className="dash-empty-inline">Loading reports...</div>}

      {loaded && reports.length === 0 && (
        <section className="intel-empty">
          <div className="intel-empty-pill">NO REPORTS YET</div>
          <h2 className="intel-empty-head">Delfi's first review is on the way</h2>
          <p className="intel-empty-body">
            Review cycles fire every 50 closed trades. Until then, Delfi keeps
            forecasting and collecting the sample it needs to write something
            statistically meaningful.
          </p>
        </section>
      )}

      {loaded && reports.length > 0 && (
        <section className="intel-section">
          <div className="intel-list">
            {reports.map((r) => (
              <ReportCard
                key={r.id}
                report={r}
                expanded={openId === r.id}
                onToggle={() => setOpenId(openId === r.id ? null : r.id)}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function ReportCard({
  report, expanded, onToggle,
}: {
  report:    Report;
  expanded:  boolean;
  onToggle:  () => void;
}) {
  const mode = (report.mode || "simulation").toUpperCase();
  return (
    <article className="intel-card">
      <header className="intel-card-head">
        <div className="intel-card-date">{fmtDate(report.created_at)}</div>
        <div className="intel-card-status pending">{mode}</div>
      </header>

      <div className="intel-card-param">
        {report.settled_count ?? 0} settled trades
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
        {expanded ? "Hide full report" : "Show full report"}
      </button>

      {expanded && report.summary_user && (
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
          {report.summary_user}
        </pre>
      )}
    </article>
  );
}
