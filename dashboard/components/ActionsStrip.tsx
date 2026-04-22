"use client";

import type { ToastFn } from "@/lib/format";
import { useState } from "react";

export function ActionsStrip({
  toast, refresh,
}: { toast: ToastFn; refresh: () => void }) {
  const [scanning,  setScanning]  = useState(false);
  const [resolving, setResolving] = useState(false);

  const fire = async (
    path: string,
    label: string,
    lock: () => void,
    unlock: () => void,
  ) => {
    lock();
    try {
      const res  = await fetch(path, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        toast(`${label} triggered`);
        setTimeout(refresh, 1500);
      } else {
        toast(body.error ?? `HTTP ${res.status}`, "error");
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "request failed", "error");
    } finally {
      unlock();
    }
  };

  return (
    <section className="border border-[#1a1a1a] bg-[#050505] px-3 py-2">
      <div className="flex items-center gap-3">
        <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0] mr-2">actions</h2>
        <ActionButton
          label="scan now"
          disabled={scanning}
          onClick={() => fire("/api/scan-now", "scan", () => setScanning(true), () => setScanning(false))}
        />
        <ActionButton
          label="resolve now"
          disabled={resolving}
          onClick={() => fire("/api/resolve-now", "resolve", () => setResolving(true), () => setResolving(false))}
        />
        <ActionButton label="refresh" onClick={refresh} />
      </div>
    </section>
  );
}

function ActionButton({
  label, onClick, disabled,
}: {
  label: string; onClick: () => void; disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="px-3 py-1 text-[11px] uppercase tracking-widest
                 bg-[#0a0a0a] text-[#ccc] hover:bg-[#00ffff]/10 hover:text-[#00ffff]
                 disabled:opacity-40 disabled:cursor-not-allowed transition"
    >
      {label}
    </button>
  );
}
