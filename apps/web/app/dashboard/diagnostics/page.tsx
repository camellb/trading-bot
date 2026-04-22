"use client";

import { useCallback, useEffect, useState } from "react";

type Scope = "all" | "traded" | "skipped";

type CalibrationBin = {
  lo: number;
  hi: number;
  n: number;
  mean_pred: number | null;
  mean_actual: number | null;
  usable: boolean;
};

type BrierRow = {
  archetype?: string;
  bucket?: string;
  n: number;
  brier: number | null;
  mean_pred: number | null;
  mean_actual: number | null;
  usable: boolean;
  flagged?: boolean;
};

type DiagnosticsReport = {
  scope: Scope;
  generated_at: number;
  forecaster: {
    calibration_curve: { scope: Scope; total: number; bins: CalibrationBin[] };
    brier: {
      n: number;
      brier: number | null;
      mean_pred: number | null;
      mean_actual: number | null;
      usable: boolean;
    };
    log_score: { n: number; log_loss: number | null; usable: boolean };
    brier_by_archetype: BrierRow[];
    brier_by_horizon: BrierRow[];
  };
  sizer: {
    selection_quality: {
      traded: {
        n: number;
        pnl: number;
        cost: number;
        roi: number | null;
        usable: boolean;
      };
      skipped_counterfactual: {
        n: number;
        hypothetical_pnl: number;
        hypothetical_cost: number;
        roi: number | null;
        usable: boolean;
        stake_usd: number;
      };
    };
    roi_by_ev_bucket: Array<{
      bucket: string;
      n: number;
      pnl: number;
      cost: number;
      roi: number | null;
      usable: boolean;
    }>;
    cost_validation: {
      n: number;
      assumed_cost: number;
      implied_cost: number | null;
      theoretical_pnl: number;
      realised_pnl: number;
      total_notional: number;
      usable: boolean;
    };
    theoretical_optimal: {
      n: number;
      pnl: number;
      cost: number;
      roi: number | null;
      usable: boolean;
    };
    archetype_attribution: Array<{
      archetype: string;
      n: number;
      pnl: number;
      cost: number;
      wins: number;
      win_rate: number | null;
      roi: number | null;
      usable: boolean;
    }>;
  };
  system: {
    bankroll_series: Array<{ ts: string | null; pnl: number; bankroll: number }>;
  };
};

const SCOPES: Scope[] = ["all", "traded", "skipped"];

function pct(x: number | null | undefined, digits = 1): string {
  if (x === null || x === undefined) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}

function num(x: number | null | undefined, digits = 3): string {
  if (x === null || x === undefined) return "—";
  return x.toFixed(digits);
}

function usd(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined) return "—";
  const sign = x >= 0 ? "+" : "−";
  return `${sign}$${Math.abs(x).toFixed(digits)}`;
}

function Caveat({ n }: { n: number }) {
  if (n >= 20) return null;
  return (
    <span className="ml-2 rounded bg-amber-900/40 px-1.5 py-0.5 text-xs text-amber-300">
      low-confidence (n={n} &lt; 20)
    </span>
  );
}

export default function DiagnosticsPage() {
  const [scope, setScope] = useState<Scope>("all");
  const [data, setData] = useState<DiagnosticsReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (s: Scope) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/diagnostics?scope=${s}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`status ${res.status}`);
      const json = (await res.json()) as DiagnosticsReport;
      setData(json);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(scope);
  }, [scope, load]);

  return (
    <main className="min-h-screen bg-neutral-950 p-8 text-neutral-100">
      <div className="mx-auto max-w-6xl space-y-8">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">Diagnostics</h1>
            <p className="mt-1 text-sm text-neutral-400">
              Forecaster, sizer, and system-health metrics — read-only. Feeds
              the learning cadence. 5-minute cache.
            </p>
          </div>
          <div className="flex gap-2">
            {SCOPES.map((s) => (
              <button
                key={s}
                onClick={() => setScope(s)}
                className={`rounded px-3 py-1.5 text-sm font-medium ${
                  scope === s
                    ? "bg-blue-600 text-white"
                    : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </header>

        {loading && <p className="text-neutral-400">Loading…</p>}
        {error && (
          <p className="rounded bg-red-900/40 p-3 text-red-300">
            Failed to load: {error}
          </p>
        )}

        {data && (
          <>
            <ForecasterSection data={data.forecaster} />
            <SizerSection data={data.sizer} />
            <SystemSection data={data.system} />
          </>
        )}
      </div>
    </main>
  );
}

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-6">
      <h2 className="text-lg font-semibold">{title}</h2>
      {subtitle && <p className="mt-1 text-sm text-neutral-400">{subtitle}</p>}
      <div className="mt-4 space-y-5">{children}</div>
    </section>
  );
}

function ForecasterSection({
  data,
}: {
  data: DiagnosticsReport["forecaster"];
}) {
  return (
    <Section
      title="Forecaster health"
      subtitle="Brier, log-loss, calibration curve, and per-archetype / per-horizon breakdowns."
    >
      <div className="grid grid-cols-3 gap-4">
        <Stat
          label="Brier score"
          value={num(data.brier.brier, 4)}
          n={data.brier.n}
          hint={`${data.brier.n} resolved (baseline 0.25)`}
        />
        <Stat
          label="Log loss"
          value={num(data.log_score.log_loss, 4)}
          n={data.log_score.n}
          hint="Lower is better"
        />
        <Stat
          label="Mean pred / actual"
          value={`${pct(data.brier.mean_pred)} / ${pct(data.brier.mean_actual)}`}
          n={data.brier.n}
        />
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-neutral-300">
          Calibration curve
        </h3>
        <table className="w-full text-sm">
          <thead className="text-left text-neutral-400">
            <tr>
              <th className="py-1">Bin</th>
              <th className="py-1 text-right">n</th>
              <th className="py-1 text-right">Mean pred</th>
              <th className="py-1 text-right">Mean actual</th>
              <th className="py-1 text-right">Gap</th>
            </tr>
          </thead>
          <tbody>
            {data.calibration_curve.bins.map((b) => {
              const gap =
                b.mean_pred !== null && b.mean_actual !== null
                  ? b.mean_pred - b.mean_actual
                  : null;
              return (
                <tr
                  key={`${b.lo}-${b.hi}`}
                  className="border-t border-neutral-800"
                >
                  <td className="py-1">
                    {pct(b.lo, 0)}–{pct(b.hi, 0)}
                  </td>
                  <td className="py-1 text-right">
                    {b.n}
                    <Caveat n={b.n} />
                  </td>
                  <td className="py-1 text-right">{pct(b.mean_pred)}</td>
                  <td className="py-1 text-right">{pct(b.mean_actual)}</td>
                  <td
                    className={`py-1 text-right ${
                      gap !== null && Math.abs(gap) > 0.05
                        ? "text-amber-400"
                        : ""
                    }`}
                  >
                    {pct(gap)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <BreakdownTable
        title="Brier by archetype"
        rows={data.brier_by_archetype}
        keyField="archetype"
      />
      <BreakdownTable
        title="Brier by horizon"
        rows={data.brier_by_horizon}
        keyField="bucket"
      />
    </Section>
  );
}

function BreakdownTable({
  title,
  rows,
  keyField,
}: {
  title: string;
  rows: BrierRow[];
  keyField: "archetype" | "bucket";
}) {
  if (rows.length === 0) {
    return (
      <div>
        <h3 className="mb-2 text-sm font-semibold text-neutral-300">{title}</h3>
        <p className="text-sm text-neutral-500">No data.</p>
      </div>
    );
  }
  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-neutral-300">{title}</h3>
      <table className="w-full text-sm">
        <thead className="text-left text-neutral-400">
          <tr>
            <th className="py-1">
              {keyField === "archetype" ? "Archetype" : "Horizon"}
            </th>
            <th className="py-1 text-right">n</th>
            <th className="py-1 text-right">Brier</th>
            <th className="py-1 text-right">Mean pred</th>
            <th className="py-1 text-right">Mean actual</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={(r[keyField] as string) ?? ""}
              className={`border-t border-neutral-800 ${
                r.flagged ? "bg-red-950/30" : ""
              }`}
            >
              <td className="py-1">{r[keyField]}</td>
              <td className="py-1 text-right">
                {r.n}
                <Caveat n={r.n} />
              </td>
              <td className="py-1 text-right">{num(r.brier, 4)}</td>
              <td className="py-1 text-right">{pct(r.mean_pred)}</td>
              <td className="py-1 text-right">{pct(r.mean_actual)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SizerSection({ data }: { data: DiagnosticsReport["sizer"] }) {
  const sq = data.selection_quality;
  const cv = data.cost_validation;
  const to = data.theoretical_optimal;

  return (
    <Section
      title="Sizer health"
      subtitle="Selection gate, EV bucket ROI, cost validation, theoretical optimum."
    >
      <div className="grid grid-cols-2 gap-4">
        <Stat
          label="Traded ROI"
          value={pct(sq.traded.roi)}
          n={sq.traded.n}
          hint={`${usd(sq.traded.pnl)} on ${usd(sq.traded.cost)} cost`}
        />
        <Stat
          label={`Skipped counterfactual ($${sq.skipped_counterfactual.stake_usd} flat)`}
          value={pct(sq.skipped_counterfactual.roi)}
          n={sq.skipped_counterfactual.n}
          hint={`${usd(sq.skipped_counterfactual.hypothetical_pnl)} on ${usd(
            sq.skipped_counterfactual.hypothetical_cost,
          )} cost`}
        />
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-neutral-300">
          ROI by EV bucket
        </h3>
        <table className="w-full text-sm">
          <thead className="text-left text-neutral-400">
            <tr>
              <th className="py-1">Bucket</th>
              <th className="py-1 text-right">n</th>
              <th className="py-1 text-right">P&amp;L</th>
              <th className="py-1 text-right">Cost</th>
              <th className="py-1 text-right">ROI</th>
            </tr>
          </thead>
          <tbody>
            {data.roi_by_ev_bucket.map((r) => (
              <tr key={r.bucket} className="border-t border-neutral-800">
                <td className="py-1">{r.bucket}</td>
                <td className="py-1 text-right">
                  {r.n}
                  <Caveat n={r.n} />
                </td>
                <td
                  className={`py-1 text-right ${
                    r.pnl > 0
                      ? "text-green-400"
                      : r.pnl < 0
                      ? "text-red-400"
                      : ""
                  }`}
                >
                  {usd(r.pnl)}
                </td>
                <td className="py-1 text-right">{usd(r.cost)}</td>
                <td className="py-1 text-right">{pct(r.roi)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-neutral-300">
          Cost validation
        </h3>
        <div className="grid grid-cols-4 gap-4">
          <Stat label="Assumed" value={pct(cv.assumed_cost, 2)} n={cv.n} />
          <Stat label="Implied" value={pct(cv.implied_cost, 2)} n={cv.n} />
          <Stat label="Theoretical P&L" value={usd(cv.theoretical_pnl)} n={cv.n} />
          <Stat label="Realised P&L" value={usd(cv.realised_pnl)} n={cv.n} />
        </div>
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-neutral-300">
          Theoretical optimum (flat $10, zero fees)
        </h3>
        <div className="grid grid-cols-3 gap-4">
          <Stat label="ROI" value={pct(to.roi)} n={to.n} />
          <Stat label="P&L" value={usd(to.pnl)} n={to.n} />
          <Stat label="Cost" value={usd(to.cost)} n={to.n} />
        </div>
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-neutral-300">
          Archetype P&amp;L attribution
        </h3>
        <table className="w-full text-sm">
          <thead className="text-left text-neutral-400">
            <tr>
              <th className="py-1">Archetype</th>
              <th className="py-1 text-right">n</th>
              <th className="py-1 text-right">Wins</th>
              <th className="py-1 text-right">Win rate</th>
              <th className="py-1 text-right">P&amp;L</th>
              <th className="py-1 text-right">ROI</th>
            </tr>
          </thead>
          <tbody>
            {data.archetype_attribution.map((r) => (
              <tr key={r.archetype} className="border-t border-neutral-800">
                <td className="py-1">{r.archetype}</td>
                <td className="py-1 text-right">
                  {r.n}
                  <Caveat n={r.n} />
                </td>
                <td className="py-1 text-right">{r.wins}</td>
                <td className="py-1 text-right">{pct(r.win_rate)}</td>
                <td
                  className={`py-1 text-right ${
                    r.pnl > 0
                      ? "text-green-400"
                      : r.pnl < 0
                      ? "text-red-400"
                      : ""
                  }`}
                >
                  {usd(r.pnl)}
                </td>
                <td className="py-1 text-right">{pct(r.roi)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function SystemSection({ data }: { data: DiagnosticsReport["system"] }) {
  const series = data.bankroll_series;
  const first = series[0]?.bankroll ?? 0;
  const last = series[series.length - 1]?.bankroll ?? 0;
  const delta = last - first;

  return (
    <Section
      title="System health"
      subtitle="Realised bankroll trajectory (daily)."
    >
      <div className="grid grid-cols-3 gap-4">
        <Stat label="Days with activity" value={String(series.length)} n={series.length} />
        <Stat label="First day bankroll" value={usd(first)} n={series.length} />
        <Stat label="Latest bankroll" value={usd(last)} n={series.length} hint={`Δ ${usd(delta)}`} />
      </div>
      {series.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-neutral-400">Raw series</summary>
          <table className="mt-2 w-full">
            <thead className="text-left text-neutral-400">
              <tr>
                <th className="py-1">Timestamp</th>
                <th className="py-1 text-right">Daily P&amp;L</th>
                <th className="py-1 text-right">Cumulative bankroll</th>
              </tr>
            </thead>
            <tbody>
              {series.map((r, i) => (
                <tr key={i} className="border-t border-neutral-800">
                  <td className="py-1">{r.ts?.slice(0, 10) ?? "—"}</td>
                  <td className="py-1 text-right">{usd(r.pnl)}</td>
                  <td className="py-1 text-right">{usd(r.bankroll)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </Section>
  );
}

function Stat({
  label,
  value,
  n,
  hint,
}: {
  label: string;
  value: string;
  n: number;
  hint?: string;
}) {
  return (
    <div className="rounded border border-neutral-800 bg-neutral-950 p-3">
      <div className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </div>
      <div className="mt-1 text-lg font-mono">{value}</div>
      {hint && <div className="mt-1 text-xs text-neutral-500">{hint}</div>}
      <div className="mt-1">
        <Caveat n={n} />
      </div>
    </div>
  );
}
