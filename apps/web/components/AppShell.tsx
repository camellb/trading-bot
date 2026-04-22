"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ToastKind } from "@/lib/format";
import { useDashboardData } from "@/hooks/use-dashboard-data";
import { Sidebar, type NavView } from "./Sidebar";
import { OverviewView } from "./views/OverviewView";
import { PositionsView } from "./views/PositionsView";
import { ScannerView } from "./views/ScannerView";
import { AnalyticsView } from "./views/AnalyticsView";
import { RiskView } from "./views/RiskView";
import { IntelligenceView } from "./views/IntelligenceView";
import { SettingsView } from "./views/SettingsView";

type ToastState = { id: number; msg: string; kind: ToastKind } | null;

export function AppShell() {
  const { data, loading, refresh, lastUpdated } = useDashboardData(30_000);
  const [view, setView] = useState<NavView>("overview");
  const [toast, setToast] = useState<ToastState>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const [sidebarOpen, setSidebarOpen] = useState(false);

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

  const mode = data.summary?.mode ?? data.health?.mode ?? "simulation";
  const handleNav = (v: NavView) => {
    setView(v);
    setSidebarOpen(false);
  };

  return (
    <div className="flex h-dvh overflow-hidden bg-black">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/60 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <Sidebar
        currentView={view}
        onNavigate={handleNav}
        mode={mode}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />

      <main className="flex-1 overflow-y-auto min-w-0">
        {/* Mobile top bar */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-[#1a1a1a] lg:hidden">
          <button
            onClick={() => setSidebarOpen(true)}
            className="p-1.5 hover:bg-[#0a0a0a] text-[#a0a0a0]"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
          </button>
          <span className="text-sm font-semibold text-white/90 font-headline uppercase tracking-wide">Delfi</span>
          <span className={`ml-auto text-[10px] uppercase tracking-widest font-medium font-body px-2 py-0.5 ${
            mode === "live"
              ? "bg-danger-dim text-red-400"
              : "bg-[#00ffff]/8 text-accent"
          }`}>
            {mode === "live" ? "live" : "simulation"}
          </span>
        </div>

        <div className="p-4 lg:p-6">
          {loading && !data.summary ? (
            <div className="flex items-center justify-center h-64 text-sm text-[#666] font-body">
              <div className="flex items-center gap-3">
                <svg className="animate-spin h-5 w-5 text-accent" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"/>
                </svg>
                Loading bot state...
              </div>
            </div>
          ) : (
            <>
              {view === "overview" && (
                <OverviewView
                  data={data}
                  refresh={refresh}
                  toast={showToast}
                  lastUpdated={lastUpdated}
                />
              )}
              {view === "positions" && (
                <PositionsView
                  open={data.positions?.open}
                  settled={data.positions?.settled}
                />
              )}
              {view === "scanner" && (
                <ScannerView
                  evaluations={data.evaluations?.evaluations}
                  toast={showToast}
                  refresh={refresh}
                />
              )}
              {view === "analytics" && (
                <AnalyticsView />
              )}
              {view === "risk" && (
                <RiskView toast={showToast} />
              )}
              {view === "intelligence" && (
                <IntelligenceView
                  data={data}
                  toast={showToast}
                  refresh={refresh}
                />
              )}
              {view === "settings" && (
                <SettingsView
                  config={data.config}
                  toast={showToast}
                  refresh={refresh}
                  mode={mode}
                />
              )}
            </>
          )}
        </div>
      </main>

      {toast && <Toast msg={toast.msg} kind={toast.kind} />}
    </div>
  );
}

function Toast({ msg, kind }: { msg: string; kind: ToastKind }) {
  return (
    <div
      className={`fixed bottom-4 right-4 z-50 max-w-sm px-4 py-3 border text-sm font-body shadow-xl backdrop-blur-sm animate-in ${
        kind === "error"
          ? "border-red-500/30 bg-red-950/90 text-red-200"
          : "border-[#00ffff]/30 bg-[#001a1a]/90 text-[#00ffff]/80"
      }`}
    >
      <div className="flex items-center gap-2">
        {kind === "error" ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/></svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/></svg>
        )}
        {msg}
      </div>
    </div>
  );
}
