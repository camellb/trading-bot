"use client";

/**
 * useScanReveal - real-data scan progress, polled from /api/scan-status.
 *
 * Backend phases: idle → fetching → analyzing → complete | error.
 * While a scan is active, this hook polls every ~1s so the UI shows
 * real processed/total counts instead of a fake timer-driven animation.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type ScanPhase =
  | "idle"
  | "fetching"
  | "analyzing"
  | "complete"
  | "error";

const PHASE_LABELS: Record<ScanPhase, string> = {
  idle: "Ready",
  fetching: "Fetching markets",
  analyzing: "Analyzing markets",
  complete: "Analysis complete",
  error: "Scan failed",
};

const POLL_INTERVAL_MS = 1000;

type ScanStatus = {
  phase: ScanPhase;
  total?: number;
  processed?: number;
  opened?: number;
  current_market?: string | null;
  started_at?: string;
  updated_at?: string;
  error?: string;
};

function normalizePhase(raw: unknown): ScanPhase {
  if (
    raw === "fetching" ||
    raw === "analyzing" ||
    raw === "complete" ||
    raw === "error"
  ) {
    return raw;
  }
  return "idle";
}

export function useScanReveal() {
  const [status, setStatus] = useState<ScanStatus>({ phase: "idle" });
  const timerRef = useRef<ReturnType<typeof setInterval>>(undefined);
  const activeRef = useRef(false);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = undefined;
    }
  }, []);

  const poll = useCallback(async () => {
    try {
      const res = await fetch("/api/scan-status", { cache: "no-store" });
      if (!res.ok) return;
      const data = (await res.json()) as Partial<ScanStatus> & { phase?: unknown };
      const phase = normalizePhase(data.phase);
      setStatus({
        phase,
        total: typeof data.total === "number" ? data.total : undefined,
        processed: typeof data.processed === "number" ? data.processed : undefined,
        opened: typeof data.opened === "number" ? data.opened : undefined,
        current_market: data.current_market ?? null,
        started_at: data.started_at,
        updated_at: data.updated_at,
        error: data.error,
      });
      if (phase === "complete" || phase === "error") {
        if (activeRef.current) {
          activeRef.current = false;
          clearTimer();
        }
      }
    } catch {
      // Ignore transient fetch errors; the next tick will retry.
    }
  }, [clearTimer]);

  const startReveal = useCallback(() => {
    activeRef.current = true;
    setStatus({ phase: "fetching" });
    clearTimer();
    void poll();
    timerRef.current = setInterval(() => {
      void poll();
    }, POLL_INTERVAL_MS);
  }, [clearTimer, poll]);

  const markDataReady = useCallback(() => {
    // Kept for API compatibility - completion is driven by the
    // backend status file, so this is a no-op.
  }, []);

  const reset = useCallback(() => {
    activeRef.current = false;
    clearTimer();
    setStatus({ phase: "idle" });
  }, [clearTimer]);

  useEffect(() => clearTimer, [clearTimer]);

  const phase = status.phase;
  return {
    phase,
    phaseLabel: PHASE_LABELS[phase],
    isRevealing: phase === "fetching" || phase === "analyzing",
    isComplete: phase === "complete",
    total: status.total ?? null,
    processed: status.processed ?? null,
    opened: status.opened ?? null,
    currentMarket: status.current_market ?? null,
    error: status.error ?? null,
    startReveal,
    markDataReady,
    reset,
  };
}
