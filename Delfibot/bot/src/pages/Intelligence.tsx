import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  isConnectionError,
  LearningReport,
  PendingSuggestion,
  ReportArchetypeRow,
  ReportPosition,
  VersusMarketReport,
} from "../api";
import { formatDate, formatDateTime } from "../lib/format";
import { archetypeLabel } from "../lib/archetypes";

/**
 * Intelligence - two tabs.
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

type Tab = "reviews" | "proposals" | "versus";

export default function Intelligence() {
  const [reports, setReports]         = useState<LearningReport[] | null>(null);
  const [suggestions, setSuggestions] = useState<PendingSuggestion[] | null>(null);
  const [history, setHistory]         = useState<PendingSuggestion[] | null>(null);
  const [versus, setVersus]           = useState<VersusMarketReport | null>(null);
  const [error, setError]             = useState<string | null>(null);
  const [busyId, setBusyId]           = useState<number | null>(null);
  const [tab, setTab]                 = useState<Tab>("reviews");
  const [historyOpen, setHistoryOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [r1, r2, r3, r4] = await Promise.all([
        api.learningReports(20).then((x) => x.reports),
        api.suggestions().then((x) => x.suggestions),
        api.suggestionsHistory(20).then((x) => x.suggestions),
        api.versusMarket().catch(() => null as VersusMarketReport | null),
      ]);
      setReports(r1);
      setSuggestions(r2);
      setHistory(r3);
      setVersus(r4);
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

  // Build the cycle ranges so each report can show "Trades N - M".
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
          <button
            type="button"
            role="tab"
            aria-selected={tab === "versus"}
            className={`intel-tab ${tab === "versus" ? "active" : ""}`}
            onClick={() => setTab("versus")}
          >
            Versus Market
            {versus && versus.n_disagreed > 0 && (
              <span className="intel-tab-badge">{versus.n_disagreed}</span>
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

      {loaded && hasAnything && tab === "versus" && (
        <VersusMarketPane data={versus} />
      )}
    </div>
  );
}

/* ─────────────────────────── Versus Market tab ────────────────────────────
 *
 * V1 doctrine: Delfi follows the market favourite and only skips when
 * its forecast disagrees with the market. This pane answers the
 * question that follows naturally from that doctrine — "is the
 * forecaster's veto actually helping us, or has it been costing us
 * money?" — by walking every settled evaluation and scoring both
 * sides.
 *
 * Layout:
 *
 *   1. Verdict card (filter helping / hurting / neutral, + $ saved)
 *   2. Top stat row (totals + scoreboard on disagreements)
 *   3. Counterfactual P&L grid (skip vs back-forecast vs follow-market)
 *   4. Brier comparison (Delfi vs market, all + disagreement-subset)
 *   5. Per-archetype scoreboard (only archetypes with >=3 disagreements)
 *   6. Recent disagreements table (last 12, with winner badge)
 */
function VersusMarketPane({ data }: { data: VersusMarketReport | null }) {
  if (data == null) {
    return (
      <div className="intel-empty-soft">
        Loading the disagreement scoreboard…
      </div>
    );
  }

  if (data.n_disagreed === 0) {
    return (
      <section className="intel-empty">
        <div className="intel-empty-pill">NOTHING TO COMPARE YET</div>
        <h2 className="intel-empty-head">
          Delfi hasn&apos;t disagreed with the market on a settled trade yet
        </h2>
        <p className="intel-empty-body">
          V1 doctrine: Delfi follows the market favourite, skips when
          its forecast points the other way. This tab fills in as
          markets resolve where the forecaster and the market priced
          opposite sides of 50%.
        </p>
      </section>
    );
  }

  const sb = data.scoreboard;
  const cf = data.counterfactual;
  const b  = data.brier;

  return (
    <div className="versus-pane">
      {/* 1. Verdict card */}
      <section className={`versus-verdict tone-${data.verdict.tone}`}>
        <div className="versus-verdict-label">
          {data.verdict.label.toUpperCase()}
        </div>
        <div className="versus-verdict-headline">
          {cf.filter_saved_usd >= 0 ? (
            <>
              Skipping disagreements saved{" "}
              <span className="profit">${cf.filter_saved_usd.toFixed(2)}</span>
              {" "}per $1 staked over {cf.n_bets} bets
            </>
          ) : (
            <>
              Skipping disagreements cost{" "}
              <span className="ember">${Math.abs(cf.filter_saved_usd).toFixed(2)}</span>
              {" "}per $1 staked over {cf.n_bets} bets
            </>
          )}
        </div>
        <div className="versus-verdict-sub">
          Across {data.n_total_settled_evals} settled evaluations,
          Delfi and the market agreed{" "}
          {fmtPct(data.agreement_rate)} of the time. The {data.n_disagreed}{" "}
          disagreements are where the forecaster&apos;s veto matters.
        </div>
      </section>

      {/* 2. Top stat row */}
      <section className="versus-grid">
        <StatCell label="Settled evaluations" value={String(data.n_total_settled_evals)} />
        <StatCell label="Taken" value={String(data.n_taken)} sub={`actual P&L ${fmtUsdSigned(data.actual_taken_pnl_usd)}`} />
        <StatCell label="Skipped" value={String(data.n_skipped)} />
        <StatCell label="Disagreements" value={String(data.n_disagreed)} sub={`${fmtPct(data.agreement_rate ?? 0, 1)} agreement`} />
      </section>

      {/* 3. Scoreboard */}
      <section className="versus-card">
        <h3 className="versus-card-title">
          When they disagreed, who was right?
        </h3>
        <p className="versus-card-sub">
          Of the {sb.n_disagreed} settled markets where Delfi&apos;s
          forecast pointed away from the market favourite.
        </p>
        <div className="versus-vs-row">
          <div className="versus-vs-cell delfi">
            <div className="versus-vs-name">Delfi</div>
            <div className="versus-vs-rate">{fmtPct(sb.delfi_win_rate)}</div>
            <div className="versus-vs-count">
              {sb.delfi_right} of {sb.n_disagreed}
            </div>
          </div>
          <div className="versus-vs-divider">vs</div>
          <div className="versus-vs-cell market">
            <div className="versus-vs-name">Market</div>
            <div className="versus-vs-rate">{fmtPct(sb.market_win_rate)}</div>
            <div className="versus-vs-count">
              {sb.market_right} of {sb.n_disagreed}
            </div>
          </div>
        </div>
      </section>

      {/* 4. Counterfactual P&L */}
      <section className="versus-card">
        <h3 className="versus-card-title">
          Counterfactual P&L on disagreements
        </h3>
        <p className="versus-card-sub">
          At $1 notional per bet across {cf.n_bets} settled
          disagreements, what each strategy would have netted.
        </p>
        <div className="versus-cf-grid">
          <CounterfactualRow
            label="Skip (what Delfi does)"
            usd={cf.actual_usd}
            isActive
          />
          <CounterfactualRow
            label="Back the forecast"
            usd={cf.backed_forecast_usd}
          />
          <CounterfactualRow
            label="Follow the market favourite"
            usd={cf.followed_market_usd}
          />
        </div>
        <div className="versus-cf-footer">
          {cf.filter_saved_usd >= 0 ? (
            <>
              Net: skipping these {cf.n_bets} bets saved{" "}
              <span className="profit">{fmtUsdSigned(cf.filter_saved_usd)}</span>
              {" "}vs blindly following the market favourite.
            </>
          ) : (
            <>
              Net: skipping these {cf.n_bets} bets cost{" "}
              <span className="ember">{fmtUsdSigned(cf.filter_saved_usd)}</span>
              {" "}vs blindly following the market favourite.
            </>
          )}
        </div>
      </section>

      {/* 5. Brier comparison */}
      <section className="versus-card">
        <h3 className="versus-card-title">Forecaster calibration</h3>
        <p className="versus-card-sub">
          Brier score is the average squared distance between the
          predicted YES-probability and the actual outcome. Lower is
          better. 0.25 is a coin flip.
        </p>
        <div className="versus-brier-grid">
          <BrierCell
            label="Delfi (all settled)"
            value={b.delfi}
            n={b.n}
            comparison={b.market}
          />
          <BrierCell
            label="Market (all settled)"
            value={b.market}
            n={b.n}
            comparison={b.delfi}
          />
          <BrierCell
            label="Delfi (disagreements)"
            value={b.delfi_on_disagree}
            n={b.n_disagree}
            comparison={b.market_on_disagree}
          />
          <BrierCell
            label="Market (disagreements)"
            value={b.market_on_disagree}
            n={b.n_disagree}
            comparison={b.delfi_on_disagree}
          />
        </div>
      </section>

      {/* 6. By archetype */}
      {data.by_archetype.length > 0 && (
        <section className="versus-card">
          <h3 className="versus-card-title">By archetype</h3>
          <p className="versus-card-sub">
            Archetypes with at least 3 settled disagreements. Win rate
            is computed on the disagreement subset only.
          </p>
          <table className="versus-table">
            <thead>
              <tr>
                <th>Archetype</th>
                <th className="num">Disagreed</th>
                <th className="num">Delfi right</th>
                <th className="num">Market right</th>
                <th className="num">Edge</th>
              </tr>
            </thead>
            <tbody>
              {data.by_archetype.map((a) => {
                const delfiRate = a.n_disagreed > 0
                  ? a.n_delfi_right_on_disagree / a.n_disagreed
                  : 0;
                const marketRate = a.n_disagreed > 0
                  ? a.n_market_right_on_disagree / a.n_disagreed
                  : 0;
                const edge = delfiRate - marketRate;
                return (
                  <tr key={a.archetype}>
                    <td>{archetypeLabel(a.archetype)}</td>
                    <td className="num">{a.n_disagreed}</td>
                    <td className="num">
                      {a.n_delfi_right_on_disagree} ({fmtPct(delfiRate)})
                    </td>
                    <td className="num">
                      {a.n_market_right_on_disagree} ({fmtPct(marketRate)})
                    </td>
                    <td className={`num ${edge > 0 ? "profit" : edge < 0 ? "ember" : ""}`}>
                      {edge >= 0 ? "+" : ""}{(edge * 100).toFixed(1)} pts
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}

      {/* 7. Recent disagreements */}
      {data.recent_disagreements.length > 0 && (
        <section className="versus-card">
          <h3 className="versus-card-title">Recent disagreements</h3>
          <p className="versus-card-sub">
            Most recent {data.recent_disagreements.length} settled
            markets where Delfi and the market pointed opposite sides
            of 50%.
          </p>
          <ul className="versus-recent">
            {data.recent_disagreements.map((r) => (
              <li key={r.id} className="versus-recent-row">
                <div className="versus-recent-q">{r.question}</div>
                <div className="versus-recent-meta">
                  <span className="versus-recent-arch">
                    {archetypeLabel(r.archetype)}
                  </span>
                  <span className="versus-recent-prices">
                    Delfi {fmtPct(r.delfi_p_yes)} YES vs Market{" "}
                    {fmtPct(r.market_p_yes)} YES
                  </span>
                  <span className="versus-recent-outcome">
                    Resolved {r.outcome}
                  </span>
                  <span className={`versus-winner ${r.winner}`}>
                    {r.winner === "delfi"  && "Delfi right"}
                    {r.winner === "market" && "Market right"}
                    {r.winner === "tie"    && "Tie"}
                  </span>
                  {r.taken && <span className="versus-taken">TAKEN</span>}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function StatCell({
  label, value, sub,
}: { label: string; value: string; sub?: string }) {
  return (
    <div className="versus-stat">
      <div className="versus-stat-label">{label}</div>
      <div className="versus-stat-value">{value}</div>
      {sub && <div className="versus-stat-sub">{sub}</div>}
    </div>
  );
}

function CounterfactualRow({
  label, usd, isActive = false,
}: { label: string; usd: number; isActive?: boolean }) {
  const tone = usd > 0 ? "profit" : usd < 0 ? "ember" : "neutral";
  return (
    <div className={`versus-cf-row ${isActive ? "active" : ""}`}>
      <div className="versus-cf-label">
        {label}
        {isActive && <span className="versus-cf-pill">CURRENT</span>}
      </div>
      <div className={`versus-cf-value ${tone}`}>
        {fmtUsdSigned(usd)}
      </div>
    </div>
  );
}

function BrierCell({
  label, value, n, comparison,
}: {
  label: string;
  value: number | null;
  n: number;
  comparison: number | null;
}) {
  // Lower Brier is better, so the side with the lower value
  // gets the "better" pill.
  const better = value != null && comparison != null && value < comparison;
  return (
    <div className={`versus-brier-cell ${better ? "winning" : ""}`}>
      <div className="versus-brier-label">{label}</div>
      <div className="versus-brier-value">{fmtBrier(value)}</div>
      <div className="versus-brier-n">n={n}</div>
      {better && <div className="versus-brier-pill">BETTER</div>}
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
