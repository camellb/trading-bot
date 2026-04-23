"use client";

import { useEffect, useMemo, useState } from "react";
import { getJSON } from "@/lib/fetch-json";
import "../../styles/content.css";

type Suggestion = {
  id: number;
  created_at: string | null;
  param_name: string;
  current_value: number | null;
  proposed_value: number | null;
  evidence: string | null;
  backtest_delta: number | null;
  backtest_trades: number | null;
  status: "pending" | "snoozed" | string;
  settled_count: number | null;
  metadata: Record<string, unknown> | null;
};

type SuggestionsPayload = { user_id: string; suggestions: Suggestion[] };

type Report = {
  id:            number;
  created_at:    string | null;
  mode:          string | null;
  settled_count: number | null;
  thesis:        string | null;
  summary_user:  string | null;
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
  return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-US", {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtNum(n: number | null, digits = 3): string {
  if (n == null) return "-";
  return n.toFixed(digits);
}

function fmtDelta(n: number | null): string {
  if (n == null) return "-";
  const sign = n >= 0 ? "+" : "";
  return `${sign}${(n * 100).toFixed(2)}%`;
}

export default function IntelligencePage() {
  const [reports, setReports]           = useState<Report[] | null>(null);
  const [suggestions, setSuggestions]   = useState<Suggestion[] | null>(null);
  const [loaded, setLoaded]             = useState(false);
  const [openReportId, setOpenReportId] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [r1, r2] = await Promise.all([
        getJSON<ReportsPayload>("/api/learning-reports?limit=20"),
        getJSON<SuggestionsPayload>("/api/suggestions?include_snoozed=1"),
      ]);
      if (cancelled) return;
      setReports(r1?.reports ?? []);
      setSuggestions(r2?.suggestions ?? []);
      setLoaded(true);
    };
    load();
    const id = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const pending = useMemo(
    () => (suggestions ?? []).filter((s) => s.status === "pending"),
    [suggestions],
  );
  const snoozed = useMemo(
    () => (suggestions ?? []).filter((s) => s.status === "snoozed"),
    [suggestions],
  );

  const reportsList = reports ?? [];
  const hasAnything =
    reportsList.length > 0 || pending.length > 0 || snoozed.length > 0;

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Intelligence</h1>
            <p className="page-sub">
              Every 50 closed trades Delfi writes a review: what moved the book,
              where calibration held or drifted, and what the next cycle will
              focus on. Proposed config changes land below each review with
              evidence. Reports also arrive in your Telegram.
            </p>
          </div>
        </div>
      </div>

      {!loaded && <Empty label="Loading reviews..." />}

      {loaded && !hasAnything && (
        <section className="intel-empty">
          <div className="intel-empty-pill">NO REVIEWS YET</div>
          <h2 className="intel-empty-head">Delfi&apos;s first review is on the way</h2>
          <p className="intel-empty-body">
            Review cycles fire every 50 closed trades. Until then, Delfi keeps
            forecasting and collecting the sample it needs to write something
            statistically meaningful.
          </p>
        </section>
      )}

      {loaded && reportsList.length > 0 && (
        <section className="intel-section">
          <h2 className="intel-section-title">Latest review</h2>
          <div className="intel-list">
            {reportsList.map((r) => (
              <ReportCard
                key={r.id}
                report={r}
                expanded={openReportId === r.id}
                onToggle={() => setOpenReportId(openReportId === r.id ? null : r.id)}
              />
            ))}
          </div>
        </section>
      )}

      {loaded && pending.length > 0 && (
        <section className="intel-section">
          <h2 className="intel-section-title">Proposals queued</h2>
          <div className="intel-list">
            {pending.map((s) => (
              <SuggestionCard key={s.id} s={s} />
            ))}
          </div>
        </section>
      )}

      {loaded && snoozed.length > 0 && (
        <section className="intel-section">
          <h2 className="intel-section-title">Snoozed</h2>
          <div className="intel-list">
            {snoozed.map((s) => (
              <SuggestionCard key={s.id} s={s} />
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
  const modeRaw = (report.mode || "simulation").toLowerCase();
  const mode = modeRaw.toUpperCase();
  return (
    <article className="intel-card">
      <header className="intel-card-head">
        <div className="intel-card-date">{fmtDateTime(report.created_at)}</div>
        <div className={`intel-card-status mode-${modeRaw}`}>{mode}</div>
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

function SuggestionCard({ s }: { s: Suggestion }) {
  return (
    <article className="intel-card">
      <header className="intel-card-head">
        <div className="intel-card-date">{fmtDate(s.created_at)}</div>
        <div className={`intel-card-status ${s.status}`}>{s.status.toUpperCase()}</div>
      </header>

      <div className="intel-card-param">{s.param_name}</div>

      <div className="intel-card-move">
        <span className="intel-card-from">{fmtNum(s.current_value)}</span>
        <span className="intel-card-arrow">&rarr;</span>
        <span className="intel-card-to">{fmtNum(s.proposed_value)}</span>
      </div>

      {s.evidence && <p className="intel-card-evidence">{s.evidence}</p>}

      <dl className="intel-card-stats">
        <div className="intel-card-stat">
          <dt>Backtest delta</dt>
          <dd className={s.backtest_delta != null && s.backtest_delta >= 0 ? "profit" : ""}>
            {fmtDelta(s.backtest_delta)}
          </dd>
        </div>
        <div className="intel-card-stat">
          <dt>Backtest trades</dt>
          <dd>{s.backtest_trades ?? "-"}</dd>
        </div>
        <div className="intel-card-stat">
          <dt>Settled at review</dt>
          <dd>{s.settled_count ?? "-"}</dd>
        </div>
      </dl>

      <footer className="intel-card-foot">
        Apply or skip this suggestion from Telegram with{" "}
        <code>/apply {s.id}</code> or <code>/skip {s.id}</code>.
      </footer>
    </article>
  );
}

function Empty({ label }: { label: string }) {
  return <div className="dash-empty-inline">{label}</div>;
}
