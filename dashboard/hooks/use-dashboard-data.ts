"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Recommendation, Side } from "@/lib/format";

export type SummaryData = {
  mode: string;
  bankroll: number;
  equity: number;
  starting_cash: number;
  open_positions: number;
  open_cost: number;
  settled_total: number;
  settled_wins: number;
  win_rate: number | null;
  realized_pnl: number;
  brier: number | null;
  resolved_predictions: number;
  total_predictions: number;
  test_end: string | null;
};

export type HealthData = {
  status: string;
  mode: string;
  started_at: string | null;
};

export type OpenPosition = {
  id: number;
  market_id: string;
  question: string;
  category: string | null;
  side: Side;
  shares: number;
  entry_price: number;
  cost_usd: number;
  claude_probability: number | null;
  ev_bps: number | null;
  confidence: number | null;
  expected_resolution_at: string | null;
  created_at: string | null;
  prediction_id: number | null;
  reasoning: string | null;
  slug: string | null;
  event_slug: string | null;
};

export type SettledPosition = {
  id: number;
  market_id: string;
  question: string;
  category: string | null;
  side: Side;
  shares: number;
  entry_price: number;
  cost_usd: number;
  claude_probability: number | null;
  ev_bps: number | null;
  confidence: number | null;
  settlement_outcome: string | null;
  settlement_price: number | null;
  realized_pnl_usd: number | null;
  created_at: string | null;
  settled_at: string | null;
  slug: string | null;
  event_slug: string | null;
};

export type PositionsData = {
  open: OpenPosition[];
  settled: SettledPosition[];
};

export type EvaluationRow = {
  id: number;
  evaluated_at: string | null;
  market_id: string;
  question: string;
  category: string | null;
  market_price_yes: number | null;
  claude_probability: number | null;
  confidence: number | null;
  ev_bps: number | null;
  recommendation: Recommendation;
  reasoning: string | null;
  pm_position_id: number | null;
  slug: string | null;
  research_sources: string[] | null;
  event_slug: string | null;
  skip_reason: string | null;
};

export type CalibrationBin = {
  lo: number;
  hi: number;
  n: number;
  mean_pred: number;
  mean_actual: number;
};

export type CalibrationData = {
  source: string | null;
  since_days: number | null;
  total: number;
  resolved: number;
  unresolved: number;
  brier: number | null;
  mean_prob: number | null;
  mean_outcome: number | null;
  realized_pnl_usd: number | null;
  bins: CalibrationBin[];
  by_category: {
    category: string; n: number;
    brier: number | null;
    mean_pred: number | null;
    mean_actual: number | null;
  }[];
};

export type BrierTrendPoint = {
  date: string | null;
  brier: number;
  n: number;
};

export type BrierTrendData = {
  points: BrierTrendPoint[];
};

export type ConfigData = {
  config: Record<string, number | string | null>;
  active_mode: string;
  configured_mode: string;
  restart_pending: boolean;
  allowed_keys: string[];
  pending: { key: string; value: unknown; previous: unknown } | null;
};

export type DashboardSnapshot = {
  health:      HealthData       | null;
  summary:     SummaryData      | null;
  positions:   PositionsData    | null;
  evaluations: { evaluations: EvaluationRow[] } | null;
  calibration: CalibrationData  | null;
  brierTrend:  BrierTrendData   | null;
  config:      ConfigData       | null;
};

const EMPTY: DashboardSnapshot = {
  health: null, summary: null, positions: null,
  evaluations: null, calibration: null, brierTrend: null, config: null,
};

async function fetchOne<T>(url: string, signal: AbortSignal): Promise<T | null> {
  try {
    const res = await fetch(url, { signal, cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export function useDashboardData(intervalMs = 30_000) {
  const [data, setData]       = useState<DashboardSnapshot>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const lastJson  = useRef<string>("");
  const inFlight  = useRef<AbortController | null>(null);
  const aliveRef  = useRef(true);

  const fetchAll = useCallback(async () => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    const [health, summary, positions, evaluations, calibration, brierTrend, config] =
      await Promise.all([
        fetchOne<HealthData>      ("/api/health",                               controller.signal),
        fetchOne<SummaryData>     ("/api/summary",                              controller.signal),
        fetchOne<PositionsData>   ("/api/positions",                            controller.signal),
        fetchOne<{ evaluations: EvaluationRow[] }>(
                                   "/api/evaluations?limit=50",                 controller.signal),
        fetchOne<CalibrationData> ("/api/calibration?source=polymarket&since_days=30",
                                                                                controller.signal),
        fetchOne<BrierTrendData>  ("/api/brier-trend?source=polymarket",        controller.signal),
        fetchOne<ConfigData>      ("/api/config",                               controller.signal),
      ]);
    if (!aliveRef.current || controller.signal.aborted) return;

    const next: DashboardSnapshot = { health, summary, positions, evaluations, calibration, brierTrend, config };
    const json = JSON.stringify(next);
    if (json !== lastJson.current) {
      lastJson.current = json;
      setData(next);
      setLastUpdated(new Date());
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    void fetchAll();
    const id = setInterval(() => { void fetchAll(); }, intervalMs);
    return () => {
      aliveRef.current = false;
      clearInterval(id);
      inFlight.current?.abort();
    };
  }, [intervalMs, fetchAll]);

  return { data, loading, refresh: fetchAll, lastUpdated };
}
