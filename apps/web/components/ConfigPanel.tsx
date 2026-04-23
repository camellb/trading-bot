"use client";

import type { ConfigData } from "@/hooks/use-dashboard-data";
import { humanizeIdentifier, type ToastFn } from "@/lib/format";
import { useEffect, useRef, useState } from "react";

const CONFIG_HELP: Record<string, string> = {
  PM_SIMULATION_MIN_CONFIDENCE: "Simulation confidence gate",
  PM_LIVE_MIN_CONFIDENCE: "Live confidence gate",
  PM_MAX_POSITION_PCT: "Max bankroll per bet",
  PM_MIN_TRADE_USD: "Minimum stake",
  PM_MAX_TRADE_USD: "Maximum stake",
  PM_MAX_CONCURRENT_POSITIONS: "Max open bets",
  PM_SCAN_LIMIT: "Markets per scan",
  PM_MIN_VOLUME_24H_USD: "Minimum liquidity",
  PM_MAX_DAYS_TO_END: "Max days to resolve",
  PM_SKIP_EXISTING_DAYS: "Days before re-check",
};

export function ConfigPanel({
  data, toast,
}: {
  data:  ConfigData | null;
  toast: ToastFn;
}) {
  if (!data) {
    return (
      <section className="border border-[#1a1a1a] bg-[#050505]">
        <header className="px-3 py-2 border-b border-[#1a1a1a]">
          <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0]">config</h2>
        </header>
        <div className="px-3 py-6 text-center text-xs text-[#666]">loading…</div>
      </section>
    );
  }

  return (
    <section className="border border-[#1a1a1a] bg-[#050505]">
      <header className="flex items-center justify-between px-3 py-2 border-b border-[#1a1a1a]">
        <h2 className="text-xs uppercase tracking-widest text-[#a0a0a0]">config</h2>
        <span className="text-[10px] text-[#666]">
          mode: <span className="text-[#a0a0a0]">{data.active_mode ?? String(data.config.PM_MODE ?? "-")}</span>
        </span>
      </header>
      {data.restart_pending && (
        <div className="px-3 py-2 border-b border-[#1a1a1a] bg-red-950/30">
          <div className="text-[10px] uppercase tracking-widest text-red-400">restart required</div>
          <div className="text-xs text-[#ccc]">
            Mode changed to <span className="font-body text-red-300">{data.configured_mode}</span> on disk - run <code className="text-[#a0a0a0]">./bot.sh restart</code> to apply
          </div>
        </div>
      )}
      {data.pending && (
        <div className="px-3 py-2 border-b border-[#1a1a1a] bg-amber-950/30">
          <div className="text-[10px] uppercase tracking-widest text-amber-400">pending</div>
          <div className="text-xs text-white">
            {humanizeIdentifier(String(data.pending.key))}: {String(data.pending.previous)} → {String(data.pending.value)}
          </div>
          <div className="text-[10px] text-[#666]">awaiting Telegram /confirm-config</div>
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-px bg-[#0a0a0a]">
        {data.allowed_keys.map((key) => (
          <ConfigRow
            key={key}
            configKey={key}
            current={data.config[key]}
            toast={toast}
          />
        ))}
      </div>
    </section>
  );
}

function ConfigRow({
  configKey, current, toast,
}: {
  configKey: string;
  current:   number | string | null | undefined;
  toast:     ToastFn;
}) {
  const [value, setValue]       = useState<string>(current != null ? String(current) : "");
  const [submitting, setSub]   = useState(false);
  const prevServer             = useRef(current);

  useEffect(() => {
    const oldStr = prevServer.current != null ? String(prevServer.current) : "";
    const curStr = current != null ? String(current) : "";
    if (oldStr !== curStr && value === oldStr) {
      setValue(curStr);
    }
    prevServer.current = current;
  }, [current, value]);

  const dirty = value !== (current != null ? String(current) : "");

  const submit = async () => {
    if (!dirty || submitting) return;
    setSub(true);
    try {
      const res = await fetch("/api/update-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: configKey, value }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok && body.status === "pending") {
        toast(`requested ${humanizeIdentifier(configKey)} → ${body.value}. confirm in Telegram with /confirm-config`);
      } else {
        toast(body.reason ?? body.error ?? `HTTP ${res.status}`, "error");
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "request failed", "error");
    } finally {
      setSub(false);
    }
  };

  return (
    <div className="bg-[#050505] px-3 py-2">
      <div className="text-[10px] text-[#a0a0a0] mb-0.5">{humanizeIdentifier(configKey)}</div>
      <div className="text-[10px] text-[#444] mb-1">{CONFIG_HELP[configKey] ?? "Trading setting"}</div>
      <div className="flex gap-2 items-center">
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="flex-1 min-w-0 bg-[#0a0a0a] border border-[#1a1a1a]
                     px-2 py-1 text-xs font-body text-white
                     focus:outline-none focus:border-[#00ffff]"
        />
        <button
          onClick={submit}
          disabled={!dirty || submitting}
          className="px-2 py-1 text-[10px] uppercase tracking-widest
                     bg-[#0a0a0a] text-[#ccc]
                     hover:bg-[#00ffff]/10 hover:text-[#00ffff]
                     disabled:opacity-40 disabled:cursor-not-allowed
                     transition"
        >
          {submitting ? "…" : "request"}
        </button>
      </div>
    </div>
  );
}
