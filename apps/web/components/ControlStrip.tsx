"use client";

import type { ToastFn } from "@/lib/format";
import { useRef, useState } from "react";

export function ControlStrip({
  toast, refresh, mode,
}: { toast: ToastFn; refresh: () => void; mode: string | null }) {
  const [scanning, setScanning]   = useState(false);
  const [resolving, setResolving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const refreshTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const fire = async (
    path: string,
    label: string,
    lock: () => void,
    unlock: () => void,
    body?: Record<string, unknown>,
  ) => {
    lock();
    try {
      const res = await fetch(path, {
        method: "POST",
        ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        toast(`${label} triggered`);
        clearTimeout(refreshTimer.current);
        refreshTimer.current = setTimeout(refresh, 1500);
      } else {
        toast(data.error ?? `HTTP ${res.status}`, "error");
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "request failed", "error");
    } finally {
      unlock();
    }
  };

  const handleReset = () => {
    if (!confirmReset) {
      setConfirmReset(true);
      setTimeout(() => setConfirmReset(false), 5000);
      return;
    }
    setConfirmReset(false);
    fire("/api/reset-test", "reset test", () => setResetting(true), () => setResetting(false));
  };

  const handleModeSwitch = () => {
    const next = mode === "live" ? "simulation" : "live";
    fire(
      "/api/switch-mode",
      `mode → ${next === "live" ? "Live" : "Simulation"}`,
      () => setSwitching(true),
      () => setSwitching(false),
      { mode: next },
    );
  };

  const currentMode = mode ?? "simulation";

  return (
    <section className="border border-[#1a1a1a] bg-[#050505] px-3 py-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Btn label="scan" disabled={scanning}
            onClick={() => fire("/api/scan-now", "scan", () => setScanning(true), () => setScanning(false))} />
          <Btn label="resolve" disabled={resolving}
            onClick={() => fire("/api/resolve-now", "resolve", () => setResolving(true), () => setResolving(false))} />
          <Btn label="refresh" onClick={refresh} />
        </div>

        <div className="flex items-center gap-2">
          <Btn
            label={confirmReset ? "confirm?" : "reset"}
            disabled={resetting}
            onClick={handleReset}
            variant={confirmReset ? "danger" : "muted"}
          />
          <div className="w-px h-4 bg-[#1a1a1a]" />
          <Btn
            label={currentMode === "live" ? "go sim" : "go live"}
            disabled={switching}
            onClick={handleModeSwitch}
            variant={currentMode === "live" ? "muted" : "live"}
          />
        </div>
      </div>
    </section>
  );
}

function Btn({
  label, onClick, disabled, variant = "default",
}: {
  label: string; onClick: () => void; disabled?: boolean;
  variant?: "default" | "danger" | "muted" | "live";
}) {
  const colors = {
    default: "bg-[#0a0a0a] text-[#ccc] hover:bg-[#1a1a1a] hover:text-white",
    muted:   "bg-transparent text-[#666] hover:text-[#ccc] hover:bg-[#0a0a0a]/60",
    danger:  "bg-red-900/50 text-red-300 hover:bg-red-800 hover:text-red-100",
    live:    "bg-transparent text-[#00ffff]/80 border border-[#00ffff]/30 hover:bg-[#00ffff]/10 hover:text-[#00ffff]",
  };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`px-3 py-1 text-[11px] uppercase tracking-widest
                 ${colors[variant]}
                 disabled:opacity-40 disabled:cursor-not-allowed transition`}
    >
      {label}
    </button>
  );
}
