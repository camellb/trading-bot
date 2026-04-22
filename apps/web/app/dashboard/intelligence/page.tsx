"use client";

import { useEffect, useMemo, useState } from "react";
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

type Payload = { user_id: string; suggestions: Suggestion[] };

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
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

function fmtNum(n: number | null, digits = 3): string {
  if (n == null) return "—";
  return n.toFixed(digits);
}

function fmtDelta(n: number | null): string {
  if (n == null) return "—";
  const sign = n >= 0 ? "+" : "";
  return `${sign}${(n * 100).toFixed(2)}%`;
}

export default function IntelligencePage() {
  const [data, setData]   = useState<Payload | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const res = await getJSON<Payload>("/api/suggestions?include_snoozed=1");
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

  const suggestions = data?.suggestions ?? [];
  const pending = useMemo(
    () => suggestions.filter((s) => s.status === "pending"),
    [suggestions],
  );
  const snoozed = useMemo(
    () => suggestions.filter((s) => s.status === "snoozed"),
    [suggestions],
  );

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Intelligence</h1>
            <p className="page-sub">
              Delfi runs a full strategy review every 50 closed trades — category ROI,
              calibration, skip-list candidates — and proposes changes with evidence.
              Each review is dated and shows the data behind the recommendation.
            </p>
          </div>
        </div>
      </div>

      {!loaded && <Empty label="Loading reviews..." />}

      {loaded && suggestions.length === 0 && (
        <section className="intel-empty">
          <div className="intel-empty-pill">NO REVIEWS YET</div>
          <h2 className="intel-empty-head">Delfi's first review is on the way</h2>
          <p className="intel-empty-body">
            Self-improvement cycles run once every 50 closed trades. Until then,
            Delfi keeps forecasting and collecting the data it needs to draw
            statistically meaningful conclusions.
          </p>
        </section>
      )}

      {loaded && pending.length > 0 && (
        <section className="intel-section">
          <h2 className="intel-section-title">Pending review</h2>
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
        <span className="intel-card-arrow">→</span>
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
          <dd>{s.backtest_trades ?? "—"}</dd>
        </div>
        <div className="intel-card-stat">
          <dt>Settled at review</dt>
          <dd>{s.settled_count ?? "—"}</dd>
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
