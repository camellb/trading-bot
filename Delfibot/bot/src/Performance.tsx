import { useCallback, useEffect, useState } from "react";
import {
  api,
  BrierTrendPoint,
  CalibrationReport,
  LearningReport,
  PendingSuggestion,
  PerformanceSummary,
} from "./api";

/**
 * Performance + Learning tab.
 *
 * Four sections:
 *   1. Headline (bankroll, ROI, win rate, Brier).
 *   2. Pending suggestions (apply / skip / snooze).
 *   3. Brier trend - shrinks from a chart to a single number until we
 *      have enough settled trades to make a chart worth drawing. We
 *      stay clinical: no decorative caption explaining "why".
 *   4. Recent learning reports (50-trade narrative reviews).
 *
 * Empty states are clean ("No proposals yet.", "No reports yet.")
 * because doctrine forbids scaffolding/roadmap footnotes in product UI.
 */
export default function Performance() {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [trend, setTrend] = useState<BrierTrendPoint[]>([]);
  const [calibration, setCalibration] = useState<CalibrationReport | null>(null);
  const [suggestions, setSuggestions] = useState<PendingSuggestion[]>([]);
  const [reports, setReports] = useState<LearningReport[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [s, t, c, sg, r] = await Promise.all([
        api.summary(),
        api.brierTrend().then((x) => x.points),
        api.calibration({ source: "polymarket" }),
        api.suggestions().then((x) => x.suggestions),
        api.learningReports(10).then((x) => x.reports),
      ]);
      setSummary(s);
      setTrend(t);
      setCalibration(c);
      setSuggestions(sg);
      setReports(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15_000);
    return () => clearInterval(id);
  }, [refresh]);

  const onAction = async (
    id: number,
    action: "apply" | "skip" | "snooze",
  ) => {
    setBusyId(id);
    try {
      if (action === "apply") await api.applySuggestion(id);
      else if (action === "skip") await api.skipSuggestion(id);
      else await api.snoozeSuggestion(id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="grid">
      {error && <div className="error wide">{error}</div>}

      {/* Headline numbers */}
      <div className="card wide">
        <h2>Headline</h2>
        <div className="metrics">
          <Metric
            label="Bankroll"
            value={fmtUsd(summary?.bankroll)}
          />
          <Metric
            label="ROI"
            value={fmtPct(summary?.roi)}
            tone={(summary?.roi ?? 0) >= 0 ? "good" : "bad"}
          />
          <Metric
            label="Realized P&L"
            value={fmtUsd(summary?.realized_pnl)}
            tone={(summary?.realized_pnl ?? 0) >= 0 ? "good" : "bad"}
          />
          <Metric
            label="Settled trades"
            value={
              summary?.settled_total !== null && summary?.settled_total !== undefined
                ? summary.settled_total.toString()
                : "-"
            }
          />
          <Metric
            label="Win rate"
            value={fmtPct(summary?.win_rate)}
          />
          <Metric
            label="Brier"
            value={
              summary?.brier !== null && summary?.brier !== undefined
                ? summary.brier.toFixed(3)
                : "-"
            }
          />
        </div>
      </div>

      {/* Pending suggestions */}
      <div className="card wide">
        <h2>Proposals ({suggestions.length})</h2>
        {suggestions.length === 0 ? (
          <p className="empty">No proposals yet.</p>
        ) : (
          <ul className="proposals">
            {suggestions.map((s) => (
              <li key={s.id}>
                <div className="proposal-head">
                  <span className="proposal-name">{prettyParam(s)}</span>
                  <span className="proposal-change">
                    {fmtVal(s.current_value)} <span className="arrow">-&gt;</span>{" "}
                    {fmtVal(s.proposed_value)}
                  </span>
                </div>
                {s.evidence && <p className="proposal-evidence">{s.evidence}</p>}
                {(s.backtest_delta !== null && s.backtest_delta !== undefined) ||
                (s.backtest_trades !== null && s.backtest_trades !== undefined) ? (
                  <p className="proposal-backtest">
                    Backtest:{" "}
                    {s.backtest_delta !== null && s.backtest_delta !== undefined
                      ? `${(s.backtest_delta * 100).toFixed(2)}% ROI delta`
                      : "no delta"}{" "}
                    {s.backtest_trades !== null && s.backtest_trades !== undefined
                      ? `over ${s.backtest_trades} trades`
                      : ""}
                  </p>
                ) : null}
                <div className="proposal-actions">
                  <button
                    onClick={() => onAction(s.id, "apply")}
                    disabled={busyId === s.id}
                  >
                    Apply
                  </button>
                  <button
                    className="ghost"
                    onClick={() => onAction(s.id, "snooze")}
                    disabled={busyId === s.id}
                  >
                    Snooze
                  </button>
                  <button
                    className="ghost"
                    onClick={() => onAction(s.id, "skip")}
                    disabled={busyId === s.id}
                  >
                    Skip
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Calibration breakdown */}
      <div className="card">
        <h2>Calibration</h2>
        {!calibration || calibration.resolved === 0 ? (
          <p className="empty">No resolved predictions yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Probability</th>
                <th>N</th>
                <th>Predicted</th>
                <th>Actual</th>
              </tr>
            </thead>
            <tbody>
              {calibration.bins.map((b, idx) => (
                <tr key={idx}>
                  <td>
                    {b.lo.toFixed(1)}-{b.hi.toFixed(1)}
                  </td>
                  <td>{b.n}</td>
                  <td>{b.mean_pred !== null ? b.mean_pred.toFixed(3) : "-"}</td>
                  <td>
                    {b.mean_actual !== null ? b.mean_actual.toFixed(3) : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Brier trend */}
      <div className="card">
        <h2>Brier trend</h2>
        {trend.length === 0 ? (
          <p className="empty">No settled positions yet.</p>
        ) : (
          <BrierSpark points={trend} />
        )}
      </div>

      {/* Learning reports */}
      <div className="card wide">
        <h2>Learning reports ({reports.length})</h2>
        {reports.length === 0 ? (
          <p className="empty">No reports yet.</p>
        ) : (
          <ul className="reports">
            {reports.map((r) => (
              <li key={r.id}>
                <div className="report-head">
                  <span className="report-when">
                    {r.created_at
                      ? new Date(r.created_at).toLocaleString()
                      : "(unknown date)"}
                  </span>
                  {r.settled_count_at_creation !== null &&
                    r.settled_count_at_creation !== undefined && (
                      <span className="report-n">
                        {r.settled_count_at_creation} settled
                      </span>
                    )}
                </div>
                {r.thesis && <p className="report-thesis">{r.thesis}</p>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "bad";
}) {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${tone ?? ""}`.trim()}>{value}</div>
    </div>
  );
}

/**
 * Tiny inline sparkline. We avoid adding a chart library to keep the
 * bundle small; a 240x60 SVG is enough to show direction at a glance.
 */
function BrierSpark({ points }: { points: BrierTrendPoint[] }) {
  if (points.length < 2) {
    const last = points[0];
    return (
      <div className="brier-single">
        Brier <strong>{last.brier.toFixed(3)}</strong> on {last.n} trade
        {last.n === 1 ? "" : "s"}.
      </div>
    );
  }
  const w = 240;
  const h = 60;
  const xs = points.map((_, i) => (i / (points.length - 1)) * w);
  const ys = points.map((p) => p.brier);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const span = yMax - yMin || 1;
  const path = points
    .map((p, i) => {
      const y = h - ((p.brier - yMin) / span) * h;
      return `${i === 0 ? "M" : "L"} ${xs[i].toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
  const last = points[points.length - 1];
  return (
    <div className="brier-spark">
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height="60">
        <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      </svg>
      <p className="brier-foot">
        Latest <strong>{last.brier.toFixed(3)}</strong> on {last.n} trades.
        Lower is better; perfect calibration is 0.
      </p>
    </div>
  );
}

// Small format helpers
function fmtUsd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  return `${sign}$${abs.toFixed(2)}`;
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtVal(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return v.toFixed(3).replace(/\.?0+$/, "");
}

/**
 * Prettify the param-name for display. Dict ops surface the target key
 * (e.g. archetype_stake_multipliers['tennis']); scalar ops just show
 * the field name.
 */
function prettyParam(s: PendingSuggestion): string {
  const op = (s.metadata as { operation?: string } | null)?.operation;
  const target = (s.metadata as { target_key?: string; target_field?: string } | null);
  if (op === "dict_set" && target?.target_key) {
    return `${s.param_name}['${target.target_key}']`;
  }
  if (op === "list_append" && target?.target_field) {
    return `${target.target_field}: append`;
  }
  return s.param_name;
}
