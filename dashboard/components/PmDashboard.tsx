"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ToastKind } from "@/lib/format";
import { useDashboardData } from "@/hooks/use-dashboard-data";
import { HeaderBar }         from "./HeaderBar";
import { StatsStrip }        from "./StatsStrip";
import { PositionsTable }    from "./PositionsTable";
import { EvaluationsTable }  from "./EvaluationsTable";
import { CalibrationPanel }  from "./CalibrationPanel";
import { GoLiveGate }        from "./GoLiveGate";
import { ConfigPanel }       from "./ConfigPanel";
import { ControlStrip }      from "./ControlStrip";

type ToastState = { id: number; msg: string; kind: ToastKind } | null;

export function PmDashboard() {
  const { data, loading, refresh, lastUpdated } = useDashboardData(30_000);
  const [toast, setToast] = useState<ToastState>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => () => clearTimeout(toastTimer.current), []);

  const showToast = useCallback((msg: string, kind: ToastKind = "info") => {
    const id = Date.now();
    setToast({ id, msg, kind });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(
      () => setToast((cur) => (cur && cur.id === id ? null : cur)),
      4500,
    );
  }, []);

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-100 font-mono">
      <HeaderBar
        health={data.health}
        summary={data.summary}
        lastUpdated={lastUpdated}
      />
      <StatsStrip summary={data.summary} />

      <main className="w-full px-4 py-4 space-y-4 xl:px-5 2xl:px-6">
        {loading && !data.summary ? (
          <div className="text-center text-xs text-neutral-500 py-10">
            loading bot state…
          </div>
        ) : (
          <>
            <ControlStrip toast={showToast} refresh={refresh} mode={data.summary?.mode ?? data.health?.mode ?? null} />
            <GoLiveGate
              summary={data.summary}
              calibration={data.calibration}
              botMode={data.summary?.mode ?? data.health?.mode ?? null}
            />
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <PositionsTable
                open={data.positions?.open}
                settled={data.positions?.settled}
              />
              <CalibrationPanel
                data={data.calibration}
                brierTrend={data.brierTrend?.points ?? null}
                settledCount={data.summary?.settled_total ?? null}
              />
            </div>
            <EvaluationsTable evaluations={data.evaluations?.evaluations} />
            <ConfigPanel data={data.config} toast={showToast} />
          </>
        )}
      </main>

      {toast && <Toast msg={toast.msg} kind={toast.kind} />}
    </div>
  );
}

function Toast({ msg, kind }: { msg: string; kind: ToastKind }) {
  const color = kind === "error"
    ? "border-red-700 bg-red-950 text-red-200"
    : "border-amber-700 bg-amber-950 text-amber-200";
  return (
    <div className={`fixed bottom-4 right-4 max-w-sm px-3 py-2 border rounded-sm text-xs ${color}`}>
      {msg}
    </div>
  );
}
