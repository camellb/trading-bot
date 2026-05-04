import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  isConnectionError,
  LearningReport,
  PendingSuggestion,
} from "../api";
import { formatDate, formatDateTime } from "../lib/format";

/**
 * Intelligence - SaaS-parity layout, with desktop-only in-app
 * Apply / Snooze / Skip controls. The SaaS still routes those through
 * Telegram /apply and /reject; the desktop has buttons in the card.
 *
 * page-wrap with three sections:
 *   - Latest reviews: 50-trade narrative reports, expandable
 *   - Proposals queued: pending suggestions with Apply/Snooze/Skip
 *   - Snoozed: suggestions waiting for more samples
 */

// Local aliases delegating to the central tz-aware formatters in
// src/lib/format.ts so this page picks up the user's display
// timezone preference automatically.
const fmtDate = formatDate;
const fmtDateTime = formatDateTime;
function fmtNum(n: number | null, digits = 3): string {
  if (n == null) return "-";
  return n.toFixed(digits);
}
function fmtDelta(n: number | null): string {
  if (n == null) return "-";
  const sign = n >= 0 ? "+" : "";
  return `${sign}${(n * 100).toFixed(2)}%`;
}

/** Multiplier values render with an "x" suffix and two decimals so
 *  the dashboard reads "1.00x → 0.75x" instead of "1.000 → 0.750". */
function fmtMultiplier(n: number | null): string {
  if (n == null) return "-";
  return `${n.toFixed(2)}x`;
}

/** Renders the "current → proposed" diff for a pending suggestion.
 *
 *  The proposer pipeline produces three operation shapes (set in
 *  `metadata.operation` by the Python side at
 *  `engine.learning_cadence._propose_*`):
 *
 *    scalar_set    -> a single numeric param (e.g. max_stake_pct).
 *                     Render as "0.020 → 0.014".
 *    dict_set      -> set one key on a dict-valued param (e.g.
 *                     archetype_stake_multipliers["tennis"] = 0.75).
 *                     Render as "tennis: 1.00x → 0.75x".
 *    list_append   -> add items to a list-valued param (e.g.
 *                     archetype_skip_list += ["tennis"]).
 *                     Numeric current/proposed are NULL because the
 *                     Proposal dataclass only carries floats. Render
 *                     the items being added with a "+" prefix instead
 *                     of the meaningless "— → —" the prior renderer
 *                     produced.
 *
 *  Falls back to the numeric arrow for unknown / missing operations. */
function SuggestionDiff({ s }: { s: PendingSuggestion }) {
  const meta = (s.metadata ?? {}) as Record<string, unknown>;
  const op = typeof meta.operation === "string" ? meta.operation : null;

  if (op === "list_append") {
    const items = Array.isArray(meta.items)
      ? (meta.items.filter((x) => typeof x === "string") as string[])
      : [];
    if (items.length > 0) {
      return (
        <span className="intel-card-to">
          + {items.join(", ")}
        </span>
      );
    }
  }

  if (op === "dict_set") {
    const key = typeof meta.key === "string" ? meta.key : null;
    const isMultiplier = s.param_name === "archetype_stake_multipliers";
    const fmt = isMultiplier ? fmtMultiplier : (n: number | null) => fmtNum(n);
    return (
      <>
        {key && <span className="intel-card-from-key">{key}:&nbsp;</span>}
        <span className="intel-card-from">{fmt(s.current_value)}</span>
        <span className="intel-card-arrow">→</span>
        <span className="intel-card-to">{fmt(s.proposed_value)}</span>
      </>
    );
  }

  // scalar_set or unknown: numeric arrow.
  return (
    <>
      <span className="intel-card-from">{fmtNum(s.current_value)}</span>
      <span className="intel-card-arrow">→</span>
      <span className="intel-card-to">{fmtNum(s.proposed_value)}</span>
    </>
  );
}

function reportBodyText(body: LearningReport["body"]): string {
  if (body == null) return "";
  if (typeof body === "string") return body;
  try { return JSON.stringify(body, null, 2); } catch { return String(body); }
}

export default function Intelligence() {
  const [reports, setReports]         = useState<LearningReport[] | null>(null);
  const [suggestions, setSuggestions] = useState<PendingSuggestion[] | null>(null);
  const [error, setError]             = useState<string | null>(null);
  const [openReportId, setOpenReportId] = useState<number | null>(null);
  const [busyId, setBusyId]             = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [r1, r2] = await Promise.all([
        api.learningReports(20).then((x) => x.reports),
        api.suggestions().then((x) => x.suggestions),
      ]);
      setReports(r1);
      setSuggestions(r2);
      // Clear error only on confirmed success (anti-flash pattern,
      // see App.tsx::refresh).
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 60_000);
    return () => clearInterval(id);
  }, [refresh]);

  const pending = useMemo(
    () => (suggestions ?? []).filter((s) => s.status === "pending"),
    [suggestions],
  );
  const snoozed = useMemo(
    () => (suggestions ?? []).filter((s) => s.status === "snoozed"),
    [suggestions],
  );

  const reportsList = reports ?? [];
  const loaded = reports !== null && suggestions !== null;
  const hasAnything = reportsList.length > 0 || pending.length > 0 || snoozed.length > 0;

  const apply = async (id: number) => {
    setBusyId(id);
    try { await api.applySuggestion(id); await refresh(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusyId(null); }
  };
  const skip = async (id: number) => {
    setBusyId(id);
    try { await api.skipSuggestion(id); await refresh(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusyId(null); }
  };
  const snooze = async (id: number) => {
    setBusyId(id);
    try { await api.snoozeSuggestion(id, 25); await refresh(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusyId(null); }
  };

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Intelligence</h1>
          </div>
        </div>
      </div>

      {error && !isConnectionError(error) && (
        <div className="error">{error}</div>
      )}

      {!loaded && <div className="empty-state">Loading reviews...</div>}

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
          <h2 className="intel-section-title">Latest reviews</h2>
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
              <SuggestionCard
                key={s.id}
                s={s}
                busy={busyId === s.id}
                onApply={() => apply(s.id)}
                onSnooze={() => snooze(s.id)}
                onSkip={() => skip(s.id)}
              />
            ))}
          </div>
        </section>
      )}

      {loaded && snoozed.length > 0 && (
        <section className="intel-section">
          <h2 className="intel-section-title">Snoozed</h2>
          <div className="intel-list">
            {snoozed.map((s) => (
              <SuggestionCard
                key={s.id}
                s={s}
                busy={busyId === s.id}
                onApply={() => apply(s.id)}
                onSnooze={() => snooze(s.id)}
                onSkip={() => skip(s.id)}
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
  report: LearningReport;
  expanded: boolean;
  onToggle: () => void;
}) {
  const body = reportBodyText(report.body);
  const settled = (report as { settled_count_at_creation: number | null }).settled_count_at_creation ?? 0;
  return (
    <article className="intel-card">
      <header className="intel-card-head">
        <div className="intel-card-date">{fmtDateTime(report.created_at)}</div>
        <div className="intel-card-status mode-simulation">SIMULATION</div>
      </header>

      <div className="intel-card-param">
        {settled} settled trades at creation
      </div>

      {report.thesis && (
        <p className="intel-card-evidence" style={{ marginTop: 8 }}>
          {report.thesis}
        </p>
      )}

      <button
        type="button"
        onClick={onToggle}
        style={{
          background: "none", border: 0, padding: 0, cursor: "pointer",
          textAlign: "left", width: "100%", color: "var(--gold)",
          fontFamily: "var(--font-mono)", fontSize: 12, letterSpacing: "0.12em",
          textTransform: "uppercase", marginTop: 6,
        }}
      >
        {expanded ? "Hide full report" : "Show full report"}
      </button>

      {expanded && body && (
        <pre className="intel-card-body">{body}</pre>
      )}
    </article>
  );
}

function SuggestionCard({
  s, busy, onApply, onSnooze, onSkip,
}: {
  s: PendingSuggestion;
  busy: boolean;
  onApply: () => void;
  onSnooze: () => void;
  onSkip: () => void;
}) {
  const isPending = s.status === "pending";
  return (
    <article className="intel-card">
      <header className="intel-card-head">
        <div className="intel-card-date">{fmtDate(s.created_at)}</div>
        <div className={`intel-card-status ${s.status}`}>{s.status.toUpperCase()}</div>
      </header>

      <div className="intel-card-param">{s.param_name}</div>

      <div className="intel-card-move">
        <SuggestionDiff s={s} />
      </div>

      {s.evidence && <p className="intel-card-evidence">{s.evidence}</p>}

      <dl className="intel-card-stats">
        <div className="intel-card-stat">
          <dt>Backtest delta</dt>
          <dd className={s.backtest_delta != null && s.backtest_delta >= 0 ? "profit" : "ember"}>
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

      <div className="intel-card-actions">
        {isPending && (
          <>
            <button className="btn-sm gold" disabled={busy} onClick={onApply}>
              {busy ? "Applying..." : "Apply"}
            </button>
            <button className="btn-sm" disabled={busy} onClick={onSnooze}>
              Snooze 25 trades
            </button>
            <button className="btn-sm danger" disabled={busy} onClick={onSkip}>
              Skip
            </button>
          </>
        )}
        {s.status === "snoozed" && (
          <>
            <button className="btn-sm gold" disabled={busy} onClick={onApply}>
              Apply now
            </button>
            <button className="btn-sm danger" disabled={busy} onClick={onSkip}>
              Skip
            </button>
          </>
        )}
      </div>
    </article>
  );
}
