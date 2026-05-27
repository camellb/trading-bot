import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  isConnectionError,
  LearningReport,
  PendingSuggestion,
  ReportArchetypeRow,
  ReportPosition,
} from "../api";
import { formatDate, formatDateTime } from "../lib/format";
import { archetypeLabel } from "../lib/archetypes";

/**
 * Intelligence — two tabs.
 *
 *   Reviews:    50-trade narrative reports rendered as structured
 *               cards (thesis + headline grid + per-archetype +
 *               top wins/losses + calibration). No raw text dump.
 *
 *   Proposals:  Pending suggestions at top (Apply / Snooze / Skip),
 *               snoozed inline, History collapsed at the bottom.
 *
 * The page only shows the brand-new-install empty state when there
 * is nothing in any column (reports, pending, snoozed, history).
 */

const fmtDate = formatDate;
const fmtDateTime = formatDateTime;

function fmtNum(n: number | null | undefined, digits = 3): string {
  if (n == null) return "-";
  return Number(n).toFixed(digits);
}
function fmtMultiplier(n: number | null | undefined): string {
  if (n == null) return "-";
  return `${Number(n).toFixed(2)}x`;
}
function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n == null) return "-";
  return `${(Number(n) * 100).toFixed(digits)}%`;
}
function fmtPctSigned(n: number | null | undefined, digits = 1): string {
  if (n == null) return "-";
  const v = Number(n) * 100;
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}%`;
}
function fmtUsdSigned(n: number | null | undefined): string {
  if (n == null) return "-";
  const v = Number(n);
  const sign = v >= 0 ? "+" : "-";
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}
function fmtBrier(n: number | null | undefined): string {
  if (n == null) return "-";
  return Number(n).toFixed(3);
}

// Three buckets only: Profitable / Breakeven / Unprofitable.
// Older verdict strings (saved to learning_reports.data before the
// collapse) map onto one of the three so old rows keep rendering.
const VERDICT_COPY: Record<string, { label: string; tone: "profit" | "ember" | "neutral" }> = {
  profitable:           { label: "Profitable",   tone: "profit"  },
  unprofitable:         { label: "Unprofitable", tone: "ember"   },
  breakeven:            { label: "Breakeven",    tone: "neutral" },
  // Legacy aliases (write-once compatibility for already-saved rows):
  strongly_profitable:  { label: "Profitable",   tone: "profit"  },
  mildly_unprofitable:  { label: "Unprofitable", tone: "ember"   },
  deeply_unprofitable:  { label: "Unprofitable", tone: "ember"   },
  mis_calibrated:       { label: "Unprofitable", tone: "ember"   },
  neutral:              { label: "Breakeven",    tone: "neutral" },
  insufficient_data:    { label: "Breakeven",    tone: "neutral" },
};

function verdictPresent(v: string | null | undefined) {
  if (!v) return { label: "Unknown", tone: "neutral" as const };
  return VERDICT_COPY[v] ?? { label: v.replace(/_/g, " "), tone: "neutral" as const };
}

/** Diff renderer for the three suggestion operation shapes. */
function SuggestionDiff({ s }: { s: PendingSuggestion }) {
  const meta = (s.metadata ?? {}) as Record<string, unknown>;
  const op = typeof meta.operation === "string" ? meta.operation : null;

  if (op === "list_append") {
    const items = Array.isArray(meta.items)
      ? (meta.items.filter((x) => typeof x === "string") as string[])
      : [];
    if (items.length > 0) {
      return <span className="intel-card-to">+ {items.join(", ")}</span>;
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

  return (
    <>
      <span className="intel-card-from">{fmtNum(s.current_value)}</span>
      <span className="intel-card-arrow">→</span>
      <span className="intel-card-to">{fmtNum(s.proposed_value)}</span>
    </>
  );
}

type Tab = "reviews" | "proposals";

export default function Intelligence() {
  const [reports, setReports]         = useState<LearningReport[] | null>(null);
  const [suggestions, setSuggestions] = useState<PendingSuggestion[] | null>(null);
  const [history, setHistory]         = useState<PendingSuggestion[] | null>(null);
  const [error, setError]             = useState<string | null>(null);
  const [busyId, setBusyId]           = useState<number | null>(null);
  const [tab, setTab]                 = useState<Tab>("reviews");
  const [historyOpen, setHistoryOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [r1, r2, r3] = await Promise.all([
        api.learningReports(20).then((x) => x.reports),
        api.suggestions().then((x) => x.suggestions),
        api.suggestionsHistory(20).then((x) => x.suggestions),
      ]);
      setReports(r1);
      setSuggestions(r2);
      setHistory(r3);
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
  const historyList = history ?? [];
  const loaded      = reports !== null && suggestions !== null && history !== null;
  const hasAnything =
    reportsList.length > 0 ||
    pending.length > 0 ||
    snoozed.length > 0 ||
    historyList.length > 0;

  // Auto-jump to Proposals when there's pending work and no reviews yet.
  useEffect(() => {
    if (!loaded) return;
    if (reportsList.length === 0 && (pending.length > 0 || snoozed.length > 0)) {
      setTab("proposals");
    }
  }, [loaded, reportsList.length, pending.length, snoozed.length]);

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

  // Build the cycle ranges so each report can show "Trades N — M".
  // The reports come back newest-first; the previous cycle's
  // bookmark is the one immediately after in the array.
  const reportRanges = useMemo(() => {
    const ranges = new Map<number, { from: number; to: number }>();
    for (let i = 0; i < reportsList.length; i++) {
      const r = reportsList[i];
      const to = r.settled_count ?? 0;
      const prev = reportsList[i + 1];
      const from = (prev?.settled_count ?? 0) + 1;
      ranges.set(r.id, { from: Math.max(1, from), to });
    }
    return ranges;
  }, [reportsList]);

  const cycleNumber = (id: number) => {
    const idx = reportsList.findIndex((r) => r.id === id);
    if (idx < 0) return null;
    return reportsList.length - idx;
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

      {loaded && hasAnything && (
        <div className="intel-tabs" role="tablist" aria-label="Intelligence sections">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "reviews"}
            className={`intel-tab ${tab === "reviews" ? "active" : ""}`}
            onClick={() => setTab("reviews")}
          >
            Reviews
            {reportsList.length > 0 && (
              <span className="intel-tab-badge">{reportsList.length}</span>
            )}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "proposals"}
            className={`intel-tab ${tab === "proposals" ? "active" : ""}`}
            onClick={() => setTab("proposals")}
          >
            Proposals
            {(pending.length + snoozed.length) > 0 && (
              <span className="intel-tab-badge gold">
                {pending.length + snoozed.length}
              </span>
            )}
          </button>
        </div>
      )}

      {loaded && hasAnything && tab === "reviews" && (
        <ReviewsPane
          reports={reportsList}
          ranges={reportRanges}
          cycleNumber={cycleNumber}
        />
      )}

      {loaded && hasAnything && tab === "proposals" && (
        <ProposalsPane
          pending={pending}
          snoozed={snoozed}
          history={historyList}
          historyOpen={historyOpen}
          onHistoryToggle={() => setHistoryOpen((v) => !v)}
          busyId={busyId}
          onApply={apply}
          onSnooze={snooze}
          onSkip={skip}
        />
      )}
    </div>
  );
}

/* ─────────────────────────── Reviews tab ─────────────────────────── */

function ReviewsPane({
  reports, ranges, cycleNumber,
}: {
  reports: LearningReport[];
  ranges: Map<number, { from: number; to: number }>;
  cycleNumber: (id: number) => number | null;
}) {
  if (reports.length === 0) {
    return (
      <section className="intel-empty">
        <div className="intel-empty-pill">NO REVIEWS YET</div>
        <h2 className="intel-empty-head">First review fires at 50 settled trades</h2>
        <p className="intel-empty-body">
          Delfi reviews its performance every 50 closed trades.
        </p>
      </section>
    );
  }
  return (
    <div className="intel-list">
      {reports.map((r, idx) => (
        <ReportCard
          key={r.id}
          report={r}
          range={ranges.get(r.id) ?? null}
          cycle={cycleNumber(r.id)}
          hero={idx === 0}
        />
      ))}
    </div>
  );
}

function ReportCard({
  report, range, cycle, hero,
}: {
  report: LearningReport;
  range: { from: number; to: number } | null;
  cycle: number | null;
  hero: boolean;
}) {
  const data = report.data;
  const headline = data?.headline;
  const verdict = verdictPresent(data?.verdict);
  const settledRow = report.settled_count ?? 0;

  const tradesLabel =
    range && range.to > 0
      ? `Trades ${range.from}–${range.to}`
      : `${settledRow} settled trades`;
  const dateLabel =
    data?.window_start && data?.window_end
      ? `${formatDate(data.window_start)} – ${formatDate(data.window_end)}`
      : null;
  const cycleLabel = cycle != null ? ` · Cycle ${cycle}` : "";

  return (
    <article className={`review-card ${hero ? "hero" : ""}`}>
      <header className="review-card-head">
        <div className="review-card-eyebrow">
          {tradesLabel}
          {dateLabel ? ` · ${dateLabel}` : ""}
          {cycleLabel}
        </div>
        <div className="review-card-meta">
          <span className="review-card-date">{fmtDateTime(report.created_at)}</span>
          <span className={`intel-card-status mode-${(data?.mode ?? "simulation").toLowerCase()}`}>
            {(data?.mode ?? "simulation").toUpperCase()}
          </span>
        </div>
      </header>

      {report.thesis && (
        <p className="review-thesis">{report.thesis}</p>
      )}

      <div className="review-stats cycle">
        <Stat
          label="Cycle ROI"
          value={fmtPctSigned(headline?.roi)}
          tone={(headline?.roi ?? 0) >= 0 ? "profit" : "ember"}
        />
        <Stat
          label="Cycle P&L"
          value={fmtUsdSigned(headline?.pnl_usd)}
          tone={(headline?.pnl_usd ?? 0) >= 0 ? "profit" : "ember"}
        />
        <Stat
          label="Win rate"
          value={fmtPct(headline?.win_rate, 0)}
        />
        <Stat
          label="Avg Brier"
          value={fmtBrier(headline?.brier)}
          tone={(headline?.brier ?? 0.25) > 0.25 ? "ember" : undefined}
        />
        <Stat
          label="Cycle verdict"
          value={verdict.label}
          tone={verdict.tone === "neutral" ? undefined : verdict.tone}
          text
        />
      </div>

      {data && data.per_archetype.length > 0 && (
        <PerArchetypeTable rows={data.per_archetype.slice(0, 6)} />
      )}

      {data && (data.top_wins.length > 0 || data.top_losses.length > 0) && (
        <TopMovesGrid wins={data.top_wins} losses={data.top_losses} />
      )}
    </article>
  );
}

function Stat({
  label, value, tone, text, sub,
}: {
  label: string;
  value: string;
  tone?: "profit" | "ember";
  /** Text-style values use a smaller display font so words like
   *  "Unprofitable" don't overflow the column at 22px mono. */
  text?: boolean;
  sub?: string;
}) {
  return (
    <div className="review-stat">
      <div className="review-stat-label">{label}</div>
      <div className={`review-stat-value ${tone ?? ""} ${text ? "text" : ""}`}>
        {value}
      </div>
      {sub && <div className="review-stat-sub">{sub}</div>}
    </div>
  );
}

function PerArchetypeTable({ rows }: { rows: ReportArchetypeRow[] }) {
  return (
    <section className="review-section">
      <h3 className="review-section-title">By archetype</h3>
      <div className="review-table">
        <div className="review-tr review-thead">
          <div>Archetype</div>
          <div className="num">Trades</div>
          <div className="num">P&amp;L</div>
          <div className="num">ROI</div>
          <div className="num">Brier</div>
        </div>
        {rows.map((r, i) => {
          const pnl = r.pnl_usd ?? 0;
          const roi = r.roi;
          return (
            <div className="review-tr" key={`${r.archetype}-${i}`}>
              <div className="archetype">{archetypeLabel(r.archetype)}</div>
              <div className="num muted">{r.n}</div>
              <div className={`num ${pnl >= 0 ? "profit" : "ember"}`}>{fmtUsdSigned(pnl)}</div>
              <div className={`num ${(roi ?? 0) >= 0 ? "profit" : "ember"}`}>
                {fmtPctSigned(roi)}
              </div>
              <div className="num muted">{fmtBrier(r.brier)}</div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function TopMovesGrid({
  wins, losses,
}: {
  wins: ReportPosition[];
  losses: ReportPosition[];
}) {
  return (
    <section className="review-section">
      <div className="review-moves">
        <div className="review-moves-col">
          <h3 className="review-section-title profit">Top wins</h3>
          {wins.length === 0 && (
            <div className="review-moves-empty">No winners this cycle.</div>
          )}
          {wins.map((w) => (
            <MoveRow key={w.id} pos={w} positive />
          ))}
        </div>
        <div className="review-moves-col">
          <h3 className="review-section-title ember">Top losses</h3>
          {losses.length === 0 && (
            <div className="review-moves-empty">No losers this cycle.</div>
          )}
          {losses.map((l) => (
            <MoveRow key={l.id} pos={l} positive={false} />
          ))}
        </div>
      </div>
    </section>
  );
}

function MoveRow({ pos, positive }: { pos: ReportPosition; positive: boolean }) {
  return (
    <div className="review-move">
      <div className={`review-move-pnl ${positive ? "profit" : "ember"}`}>
        {fmtUsdSigned(pos.pnl_usd)}
      </div>
      <div className="review-move-q" title={pos.question}>
        {pos.question}
      </div>
      <div className="review-move-meta">
        {archetypeLabel(pos.archetype)} · {pos.side ?? "-"} resolved {pos.outcome ?? "-"}
      </div>
    </div>
  );
}

/* ───────────────────────── Proposals tab ─────────────────────────── */

function ProposalsPane({
  pending, snoozed, history, historyOpen, onHistoryToggle,
  busyId, onApply, onSnooze, onSkip,
}: {
  pending: PendingSuggestion[];
  snoozed: PendingSuggestion[];
  history: PendingSuggestion[];
  historyOpen: boolean;
  onHistoryToggle: () => void;
  busyId: number | null;
  onApply: (id: number) => void;
  onSnooze: (id: number) => void;
  onSkip: (id: number) => void;
}) {
  const noActive = pending.length === 0 && snoozed.length === 0;

  return (
    <>
      {noActive && history.length === 0 && (
        <section className="intel-empty">
          <div className="intel-empty-pill">NO PROPOSALS</div>
          <h2 className="intel-empty-head">Delfi has nothing to propose right now</h2>
          <p className="intel-empty-body">
            When a category drifts (ROI, calibration, drawdown), Delfi will
            queue a config change here for your approval.
          </p>
        </section>
      )}

      {noActive && history.length > 0 && (
        <section className="intel-section">
          <h2 className="intel-section-title">Active proposals</h2>
          <div className="intel-empty-soft">
            No proposals at the moment
          </div>
        </section>
      )}

      {pending.length > 0 && (
        <section className="intel-section">
          <h2 className="intel-section-title">Pending your decision</h2>
          <div className="intel-list">
            {pending.map((s) => (
              <SuggestionCard
                key={s.id}
                s={s}
                busy={busyId === s.id}
                onApply={() => onApply(s.id)}
                onSnooze={() => onSnooze(s.id)}
                onSkip={() => onSkip(s.id)}
              />
            ))}
          </div>
        </section>
      )}

      {snoozed.length > 0 && (
        <section className="intel-section">
          <h2 className="intel-section-title">Snoozed</h2>
          <div className="intel-list">
            {snoozed.map((s) => (
              <SuggestionCard
                key={s.id}
                s={s}
                busy={busyId === s.id}
                onApply={() => onApply(s.id)}
                onSnooze={() => onSnooze(s.id)}
                onSkip={() => onSkip(s.id)}
              />
            ))}
          </div>
        </section>
      )}

      {history.length > 0 && (
        <section className="intel-section">
          <button
            type="button"
            className="intel-history-toggle"
            onClick={onHistoryToggle}
            aria-expanded={historyOpen}
          >
            {historyOpen ? "Hide history" : `Show history (${history.length})`}
          </button>
          {historyOpen && (
            <div className="intel-list intel-history">
              {history.map((s) => (
                <SuggestionCard
                  key={s.id}
                  s={s}
                  busy={false}
                  historical
                  onApply={() => onApply(s.id)}
                  onSnooze={() => onSnooze(s.id)}
                  onSkip={() => onSkip(s.id)}
                />
              ))}
            </div>
          )}
        </section>
      )}
    </>
  );
}

function SuggestionCard({
  s, busy, historical = false, onApply, onSnooze, onSkip,
}: {
  s: PendingSuggestion;
  busy: boolean;
  historical?: boolean;
  onApply: () => void;
  onSnooze: () => void;
  onSkip: () => void;
}) {
  const isPending = s.status === "pending";
  const isSnoozed = s.status === "snoozed";
  const showImpact =
    s.backtest_delta != null || s.backtest_trades != null;

  return (
    <article className={`intel-card ${historical ? "muted" : ""}`}>
      <header className="intel-card-head">
        <div className="intel-card-date">
          {fmtDate(s.created_at)}
          {historical && s.resolved_at && (
            <span className="intel-card-resolved">
              {" · "}
              {s.status === "applied" ? "Applied" : "Skipped"}{" "}
              {fmtDate(s.resolved_at)}
            </span>
          )}
        </div>
        <div className={`intel-card-status ${s.status}`}>
          {s.status.toUpperCase()}
        </div>
      </header>

      <div className="intel-card-param">{prettyParamName(s.param_name)}</div>

      <div className="intel-card-move">
        <SuggestionDiff s={s} />
      </div>

      {s.evidence && <p className="intel-card-evidence">{s.evidence}</p>}

      {showImpact && (
        <dl className="intel-card-stats">
          {s.backtest_delta != null && (
            <div className="intel-card-stat">
              <dt>Estimated impact</dt>
              <dd className={s.backtest_delta >= 0 ? "profit" : "ember"}>
                {fmtPctSigned(s.backtest_delta, 2)}
              </dd>
            </div>
          )}
          {s.backtest_trades != null && (
            <div className="intel-card-stat">
              <dt>Sample size</dt>
              <dd>{s.backtest_trades}</dd>
            </div>
          )}
          {s.settled_count != null && (
            <div className="intel-card-stat">
              <dt>Settled at review</dt>
              <dd>{s.settled_count}</dd>
            </div>
          )}
        </dl>
      )}

      {!historical && (isPending || isSnoozed) && (
        <div className="intel-card-actions">
          <button className="btn-sm gold" disabled={busy} onClick={onApply}>
            {busy ? "Applying..." : isSnoozed ? "Apply now" : "Apply"}
          </button>
          {isPending && (
            <button className="btn-sm" disabled={busy} onClick={onSnooze}>
              Snooze 25 trades
            </button>
          )}
          <button className="btn-sm danger" disabled={busy} onClick={onSkip}>
            Skip
          </button>
        </div>
      )}
    </article>
  );
}

const PARAM_LABELS: Record<string, string> = {
  max_stake_pct:               "Max stake (% of bankroll)",
  base_stake_pct:              "Base stake (% of bankroll)",
  archetype_skip_list:         "Archetype skip list",
  archetype_stake_multipliers: "Archetype stake multiplier",
  cost_assumption_override:    "Trading cost assumption",
};

function prettyParamName(p: string): string {
  if (PARAM_LABELS[p]) return PARAM_LABELS[p];
  // Fallback for any new param the server emits before this map is
  // updated: snake_case -> Title Case so the user never sees a raw
  // key like "stop_loss_threshold_pct" in the UI.
  return p
    .split("_")
    .map((w) => (w.length ? w[0].toUpperCase() + w.slice(1) : ""))
    .join(" ");
}
