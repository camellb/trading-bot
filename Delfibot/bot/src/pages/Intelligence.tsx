import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  LearningReport,
  PendingSuggestion,
} from "../api";

/**
 * Intelligence — Delfi's review reports and the proposals it queues
 * from each cycle.
 *
 * Mirrors the SaaS `/dashboard/intelligence` surface, but with two
 * differences:
 *
 *  1. Proposals carry inline action buttons (Apply, Snooze, Skip)
 *     instead of asking the user to send `/apply <id>` from Telegram.
 *     Telegram is one delivery channel, not the only one.
 *
 *  2. Review reports carry a "Show full report" toggle that renders
 *     `body` (which the local API returns as either pretty JSON or a
 *     pre-rendered string) so the user can see what Delfi actually
 *     wrote without leaving the desktop app.
 *
 * Refresh cadence: 60s. Reports change rarely (one per 50 settled
 * trades), suggestions change on apply/skip/snooze and on each new
 * cycle, so 60s is the right amount of liveness without thrashing.
 */

export default function Intelligence() {
  const [reports, setReports] = useState<LearningReport[] | null>(null);
  const [suggestions, setSuggestions] = useState<PendingSuggestion[] | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [openReportId, setOpenReportId] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      const [rRes, sRes] = await Promise.all([
        api.learningReports(20),
        api.suggestions(),
      ]);
      setReports(rRes.reports ?? []);
      setSuggestions(sRes.suggestions ?? []);
      setLoaded(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, [load]);

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

  const apply = async (id: number) => {
    if (busyId) return;
    setBusyId(id);
    try {
      await api.applySuggestion(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };
  const skip = async (id: number) => {
    if (busyId) return;
    setBusyId(id);
    try {
      await api.skipSuggestion(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };
  const snooze = async (id: number) => {
    if (busyId) return;
    setBusyId(id);
    try {
      await api.snoozeSuggestion(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div>
      <div className="page-header">
        <h1>Intelligence</h1>
      </div>

      <p className="hint" style={{ maxWidth: 760, marginBottom: 24 }}>
        Every 50 closed trades Delfi writes a review: what moved the book,
        where calibration held or drifted, and what the next cycle will focus
        on. Proposed config changes land below each review with evidence.
        Apply, snooze, or skip each proposal here.
      </p>

      {error && <div className="error">{error}</div>}

      {!loaded && <div className="empty">Loading reviews...</div>}

      {loaded && !hasAnything && <EmptyState />}

      {loaded && reportsList.length > 0 && (
        <Section title="Latest reviews">
          <ul className="reports">
            {reportsList.map((r) => (
              <ReportRow
                key={r.id}
                report={r}
                expanded={openReportId === r.id}
                onToggle={() =>
                  setOpenReportId(openReportId === r.id ? null : r.id)
                }
              />
            ))}
          </ul>
        </Section>
      )}

      {loaded && pending.length > 0 && (
        <Section title="Proposals queued">
          <ul className="proposals">
            {pending.map((s) => (
              <SuggestionRow
                key={s.id}
                s={s}
                busy={busyId === s.id}
                onApply={() => apply(s.id)}
                onSnooze={() => snooze(s.id)}
                onSkip={() => skip(s.id)}
              />
            ))}
          </ul>
        </Section>
      )}

      {loaded && snoozed.length > 0 && (
        <Section title="Snoozed">
          <ul className="proposals">
            {snoozed.map((s) => (
              <SuggestionRow
                key={s.id}
                s={s}
                busy={busyId === s.id}
                onApply={() => apply(s.id)}
                onSnooze={() => snooze(s.id)}
                onSkip={() => skip(s.id)}
              />
            ))}
          </ul>
        </Section>
      )}

      {loaded && hasAnything && (
        <p
          className="hint"
          style={{ marginTop: 32, textAlign: "center", color: "var(--vellum-40)" }}
        >
          Learning accumulates. The more Delfi runs, the better it gets.
        </p>
      )}
    </div>
  );
}

// ── Sections / rows ──────────────────────────────────────────────────────

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section style={{ marginBottom: 28 }}>
      <h2
        className="t-caption"
        style={{ margin: "0 0 12px", color: "var(--vellum-60)" }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

function EmptyState() {
  return (
    <div className="card" style={{ alignItems: "flex-start" }}>
      <div className="t-caption" style={{ color: "var(--gold)" }}>
        No reviews yet
      </div>
      <h3
        style={{
          margin: "4px 0 0",
          fontFamily: "var(--font-display)",
          fontSize: 22,
          fontWeight: 400,
          color: "var(--vellum)",
        }}
      >
        Delfi&apos;s first review is on the way
      </h3>
      <p className="hint" style={{ maxWidth: 600 }}>
        Review cycles fire every 50 closed trades. Until then, Delfi keeps
        forecasting and collecting the sample it needs to write something
        statistically meaningful.
      </p>
    </div>
  );
}

function ReportRow({
  report,
  expanded,
  onToggle,
}: {
  report: LearningReport;
  expanded: boolean;
  onToggle: () => void;
}) {
  const settled = report.settled_count_at_creation ?? 0;
  return (
    <li>
      <div className="report-head">
        <span>{fmtDateTime(report.created_at)}</span>
        <span>{settled} settled trades</span>
      </div>

      {report.thesis ? (
        <p className="report-thesis">{report.thesis}</p>
      ) : (
        <p className="report-thesis text-muted">
          (No thesis recorded for this cycle.)
        </p>
      )}

      <div style={{ marginTop: 10 }}>
        <button
          type="button"
          onClick={onToggle}
          className="btn ghost small"
          style={{ padding: "5px 12px", fontSize: 12 }}
        >
          {expanded ? "Hide full report" : "Show full report"}
        </button>
      </div>

      {expanded && (
        <div className="report-body">
          <ReportBody body={report.body} />
        </div>
      )}
    </li>
  );
}

function ReportBody({ body }: { body: LearningReport["body"] }) {
  if (body == null) {
    return <p className="empty">No detail captured.</p>;
  }
  if (typeof body === "string") {
    return <pre style={{ whiteSpace: "pre-wrap" }}>{body}</pre>;
  }
  return <pre>{JSON.stringify(body, null, 2)}</pre>;
}

function SuggestionRow({
  s,
  busy,
  onApply,
  onSnooze,
  onSkip,
}: {
  s: PendingSuggestion;
  busy: boolean;
  onApply: () => void;
  onSnooze: () => void;
  onSkip: () => void;
}) {
  return (
    <li>
      <div className="proposal-head">
        <span className="proposal-name">{formatParam(s)}</span>
        <span
          className="t-caption"
          style={{
            color:
              s.status === "snoozed" ? "var(--warn)" : "var(--vellum-60)",
          }}
        >
          {s.status}
        </span>
      </div>

      <div className="proposal-change">
        <ChangeDisplay s={s} />
      </div>

      {s.evidence && <p className="proposal-evidence">{s.evidence}</p>}

      <p className="proposal-backtest">
        <BacktestLine s={s} />
      </p>

      <div className="proposal-actions">
        <button
          type="button"
          className="btn small"
          onClick={onApply}
          disabled={busy}
        >
          Apply
        </button>
        {s.status !== "snoozed" && (
          <button
            type="button"
            className="btn ghost small"
            onClick={onSnooze}
            disabled={busy}
          >
            Snooze
          </button>
        )}
        <button
          type="button"
          className="btn ghost small"
          onClick={onSkip}
          disabled={busy}
        >
          Skip
        </button>
      </div>
    </li>
  );
}

// ── Formatting helpers ─────────────────────────────────────────────────

function formatParam(s: PendingSuggestion): string {
  const op = (s.metadata?.operation as string | undefined) ?? "scalar_set";
  const key = (s.metadata?.key as string | undefined) ?? null;
  if (op === "dict_set" && key) {
    return `${s.param_name}['${key}']`;
  }
  return s.param_name;
}

function ChangeDisplay({ s }: { s: PendingSuggestion }) {
  const op = (s.metadata?.operation as string | undefined) ?? "scalar_set";

  if (op === "list_append") {
    const adds = (s.metadata?.adds as unknown[] | undefined) ?? [];
    if (adds.length === 0) {
      return <span className="text-muted">(empty list change)</span>;
    }
    return (
      <span>
        {adds.map((x, i) => (
          <span key={i} style={{ marginRight: 8 }}>
            <span className="arrow">+</span>
            <span style={{ color: "var(--profit)" }}>{String(x)}</span>
          </span>
        ))}
      </span>
    );
  }

  // scalar_set, dict_set both render as prev → new
  return (
    <span>
      <span>{fmtVal(s.current_value)}</span>
      <span className="arrow">→</span>
      <span style={{ color: "var(--gold)" }}>{fmtVal(s.proposed_value)}</span>
    </span>
  );
}

function BacktestLine({ s }: { s: PendingSuggestion }) {
  const parts: string[] = [];
  if (s.backtest_delta != null) {
    const sign = s.backtest_delta >= 0 ? "+" : "";
    parts.push(`Backtest delta ${sign}${(s.backtest_delta * 100).toFixed(2)}%`);
  }
  if (s.backtest_trades != null) {
    parts.push(`${s.backtest_trades} trades`);
  }
  if (s.settled_count != null) {
    parts.push(`Settled at review: ${s.settled_count}`);
  }
  if (parts.length === 0) {
    return <span className="text-muted">No backtest provided.</span>;
  }
  return <>{parts.join(" • ")}</>;
}

function fmtVal(v: number | null): string {
  if (v == null) return "-";
  if (Math.abs(v) < 10) return v.toFixed(3);
  if (Math.abs(v) < 1000) return v.toFixed(2);
  return v.toLocaleString();
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
