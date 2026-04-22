"use client";

import type { ConfigData } from "@/hooks/use-dashboard-data";
import { humanizeIdentifier, type ToastFn } from "@/lib/format";
import { useCallback, useEffect, useRef, useState } from "react";

const RISK_KEYS = [
  "PM_SHADOW_MIN_CONFIDENCE",
  "PM_LIVE_MIN_CONFIDENCE",
  "PM_MAX_POSITION_PCT",
  "PM_MIN_TRADE_USD",
  "PM_MAX_TRADE_USD",
  "PM_MAX_CONCURRENT_POSITIONS",
];

const SCAN_KEYS = [
  "PM_SCAN_LIMIT",
  "PM_MIN_VOLUME_24H_USD",
  "PM_MAX_DAYS_TO_END",
  "PM_SKIP_EXISTING_DAYS",
  "PM_MIN_RESOLUTION_QUALITY",
  "PM_SHADOW_SPREAD_ESTIMATE",
  "PM_SHADOW_FEE_RATE",
];

const CONFIG_HELP: Record<string, string> = {
  PM_SHADOW_MIN_CONFIDENCE: "Minimum Claude confidence to trade in simulation",
  PM_LIVE_MIN_CONFIDENCE: "Minimum Claude confidence to trade in live mode",
  PM_MAX_POSITION_PCT: "Maximum % of bankroll per position",
  PM_MIN_TRADE_USD: "Minimum trade size in USD",
  PM_MAX_TRADE_USD: "Maximum trade size in USD",
  PM_MAX_CONCURRENT_POSITIONS: "Maximum number of open positions",
  PM_SCAN_LIMIT: "Number of markets to evaluate per scan",
  PM_MIN_VOLUME_24H_USD: "Minimum 24h volume to consider a market",
  PM_MAX_DAYS_TO_END: "Maximum days until market resolution",
  PM_SKIP_EXISTING_DAYS: "Days before re-evaluating a market",
  PM_MIN_RESOLUTION_QUALITY: "Minimum resolution quality score (0-1)",
  PM_SHADOW_SPREAD_ESTIMATE: "Estimated spread for simulated fill adjustment",
  PM_SHADOW_FEE_RATE: "Estimated fee rate for simulated fill adjustment",
};

type ControlsStatus = {
  paused: boolean;
  pause_reason: string | null;
};

export function SettingsView({
  config,
  toast,
  refresh,
  mode,
}: {
  config: ConfigData | null;
  toast: ToastFn;
  refresh: () => void;
  mode: string;
}) {
  const [switching, setSwitching] = useState(false);
  const [controls, setControls] = useState<ControlsStatus | null>(null);
  const [toggling, setToggling] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const fetchControls = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const res = await fetch("/api/controls/status", { signal: controller.signal, cache: "no-store" });
      if (res.ok && !controller.signal.aborted) {
        setControls(await res.json());
      }
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    fetchControls();
    return () => abortRef.current?.abort();
  }, [fetchControls]);

  const handleModeSwitch = async () => {
    const next = mode === "live" ? "shadow" : "live";
    setSwitching(true);
    try {
      const res = await fetch("/api/switch-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: next }),
      });
      if (res.ok) {
        toast(`Mode switched to ${next === "live" ? "Live" : "Simulation"}`);
        setTimeout(refresh, 1500);
      } else {
        toast("Mode switch failed", "error");
      }
    } catch {
      toast("Request failed", "error");
    } finally {
      setSwitching(false);
    }
  };

  const toggleBotStatus = async () => {
    const endpoint = controls?.paused ? "/api/controls/resume" : "/api/controls/pause";
    setToggling(true);
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "Dashboard toggle" }),
      });
      if (res.ok) {
        toast(controls?.paused ? "Bot resumed" : "Bot paused");
        fetchControls();
      } else {
        toast("Action failed", "error");
      }
    } catch {
      toast("Request failed", "error");
    } finally {
      setToggling(false);
    }
  };

  const botActive = !controls?.paused;

  return (
    <div className="space-y-6 max-w-[1400px]">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white">Strategy Settings</h1>
          <p className="text-xs text-[#666] mt-1">
            Configure core operational parameters and risk limits.
          </p>
        </div>
      </div>

      {config?.restart_pending && (
        <div className="bg-danger-dim border border-red-500/20 px-4 py-3">
          <div className="text-xs uppercase tracking-widest text-red-400 font-medium">Restart Required</div>
          <div className="text-sm text-[#ccc] mt-1">
            Mode changed to <span className="font-body text-red-300">{config.configured_mode}</span> on disk — run <code className="text-[#a0a0a0] bg-surface-3 px-1.5 py-0.5 text-xs">./bot.sh restart</code> to apply.
          </div>
        </div>
      )}

      {config?.pending && (
        <div className="bg-warn-dim border border-amber-500/20 px-4 py-3">
          <div className="text-xs uppercase tracking-widest text-amber-400 font-medium">Pending Change</div>
          <div className="text-sm text-[#ccc] mt-1">
            {humanizeIdentifier(String(config.pending.key))}: {String(config.pending.previous)} → {String(config.pending.value)}
          </div>
          <div className="text-xs text-[#666] mt-1">Awaiting Telegram /confirm-config</div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left column: Risk + Scanning */}
        <div className="lg:col-span-2 space-y-6">
          {/* Doctrine Risk Parameters (per-user, DB-backed) */}
          <SettingsSection
            title="Risk Parameters"
            icon={<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><path d="M12 2l9 4v6c0 5-4 9-9 10-5-1-9-5-9-10V6l9-4z"/></svg>}
          >
            <UserConfigEditor toast={toast} />
          </SettingsSection>

          {/* Risk Management */}
          <SettingsSection
            title="Risk Management"
            icon={<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>}
          >
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-px bg-[#1a1a1a]">
              {RISK_KEYS.filter((k) => config?.allowed_keys.includes(k)).map((key) => (
                <ConfigField
                  key={key}
                  configKey={key}
                  current={config?.config[key]}
                  help={CONFIG_HELP[key]}
                  toast={toast}
                />
              ))}
            </div>
          </SettingsSection>

          {/* Scanning Parameters */}
          <SettingsSection
            title="Scanning Parameters"
            icon={<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>}
          >
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-px bg-[#1a1a1a]">
              {SCAN_KEYS.filter((k) => config?.allowed_keys.includes(k)).map((key) => (
                <ConfigField
                  key={key}
                  configKey={key}
                  current={config?.config[key]}
                  help={CONFIG_HELP[key]}
                  toast={toast}
                />
              ))}
            </div>
          </SettingsSection>
        </div>

        {/* Right column: Bot config */}
        <div className="space-y-6">
          {/* Bot Configuration */}
          <SettingsSection
            title="Bot Configuration"
            icon={<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>}
          >
            <div className="divide-y divide-[#1a1a1a]">
              <ToggleRow
                label="Bot Status"
                desc="Enable or disable all trading activity"
                checked={botActive}
                onClick={toggleBotStatus}
                disabled={toggling}
              />
              <ToggleRow
                label="Simulation Mode"
                desc={mode === "live" ? "Off — real execution active" : "On — simulated fills"}
                checked={mode !== "live"}
                onClick={handleModeSwitch}
                disabled={switching}
              />
              <ToggleRow label="Auto-Restart" desc="Restart on crash via launchd" checked={true} readOnly />
              <ToggleRow label="Watchdog" desc="Force-kill on event loop freeze" checked={true} readOnly />
            </div>
          </SettingsSection>

          {/* API Configuration */}
          <SettingsSection
            title="API Configuration"
            icon={<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>}
          >
            <ApiField label="Polymarket API Key" envKey="POLYMARKET_API_KEY" />
            <ApiField label="Wallet Private Key" envKey="PRIVATE_KEY" sensitive />
            <ApiField label="Telegram Bot Token" envKey="TELEGRAM_BOT_TOKEN" />
            <div className="px-4 py-2 text-[10px] text-[#444]">
              API keys are read from .env file. Edit the file directly and restart the bot to apply changes.
            </div>
          </SettingsSection>
        </div>
      </div>
    </div>
  );
}

/* ── Sub-components ─────────────────────────────────────────────────── */

function SettingsSection({
  title, icon, children,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-surface-2 border border-[#1a1a1a] overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-[#1a1a1a]">
        <span className="text-[#666]">{icon}</span>
        <h3 className="text-sm font-medium text-white">{title}</h3>
      </div>
      {children}
    </div>
  );
}

function ApiField({ label, envKey, sensitive }: { label: string; envKey: string; sensitive?: boolean }) {
  return (
    <div className="px-4 py-3 flex items-center gap-3 border-b border-[#1a1a1a] last:border-0">
      <div className="flex-1">
        <div className="text-sm text-[#ccc]">{label}</div>
        {sensitive && (
          <div className="text-[10px] text-red-400/70 mt-0.5 flex items-center gap-1">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 9v4M12 17h.01"/><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>
            Highly Sensitive
          </div>
        )}
      </div>
      <div className="w-48">
        <input
          type="password"
          value="••••••••••••••••"
          disabled
          className="w-full bg-surface-3 border-b border-[#1a1a1a] px-3 py-1.5 text-xs
                     font-body text-[#666] cursor-not-allowed"
        />
      </div>
    </div>
  );
}

function ConfigField({
  configKey, current, help, toast,
}: {
  configKey: string;
  current: number | string | null | undefined;
  help?: string;
  toast: ToastFn;
}) {
  const [value, setValue] = useState<string>(current != null ? String(current) : "");
  const [submitting, setSub] = useState(false);
  const prevServer = useRef(current);

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
      if (res.ok && (body.status === "applied" || body.status === "pending")) {
        toast(`${humanizeIdentifier(configKey)} → ${body.value ?? value}`);
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
    <div className="bg-surface-2 px-4 py-3">
      <div className="text-xs text-[#ccc] mb-0.5">{humanizeIdentifier(configKey)}</div>
      {help && <div className="text-[10px] text-[#444] mb-2">{help}</div>}
      <div className="flex gap-2 items-center">
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="flex-1 min-w-0 bg-surface-3 border-b border-[#1a1a1a]
                     px-3 py-1.5 text-xs font-body text-white
                     focus:outline-none focus:border-accent/50 transition-colors"
        />
        <button
          onClick={submit}
          disabled={!dirty || submitting}
          className="px-3 py-1.5 text-[10px] uppercase tracking-widest
                     bg-surface-3 text-[#a0a0a0] border border-[#1a1a1a]
                     hover:bg-accent-dim hover:text-accent hover:border-accent/30
                     disabled:opacity-30 disabled:cursor-not-allowed
                     transition-colors"
        >
          {submitting ? "..." : "Save"}
        </button>
      </div>
    </div>
  );
}

type UserConfigResponse = {
  user_id: string;
  config: Record<string, number>;
  bounds: Record<string, [number, number]>;
  descriptions: Record<string, string>;
};

function UserConfigEditor({ toast }: { toast: ToastFn }) {
  const [data, setData] = useState<UserConfigResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/user-config", { cache: "no-store" });
      if (res.ok) setData(await res.json());
    } catch { /* ignore */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return <div className="px-4 py-4 text-xs text-[#666]">Loading risk parameters…</div>;
  }
  if (!data) {
    return <div className="px-4 py-4 text-xs text-red-400/70">Failed to load /api/user-config</div>;
  }

  const keys = Object.keys(data.config);
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-px bg-[#1a1a1a]">
      {keys.map((k) => (
        <UserConfigField
          key={k}
          configKey={k}
          current={data.config[k]}
          bounds={data.bounds[k]}
          description={data.descriptions[k]}
          toast={toast}
          onSaved={load}
        />
      ))}
    </div>
  );
}

function UserConfigField({
  configKey, current, bounds, description, toast, onSaved,
}: {
  configKey: string;
  current: number;
  bounds?: [number, number];
  description?: string;
  toast: ToastFn;
  onSaved: () => void;
}) {
  const [value, setValue] = useState<string>(String(current));
  const [submitting, setSub] = useState(false);
  useEffect(() => { setValue(String(current)); }, [current]);

  const dirty = value !== String(current);

  const submit = async () => {
    if (!dirty || submitting) return;
    const num = Number(value);
    if (!Number.isFinite(num)) {
      toast("invalid number", "error");
      return;
    }
    if (bounds && (num < bounds[0] || num > bounds[1])) {
      toast(`out of range [${bounds[0]}, ${bounds[1]}]`, "error");
      return;
    }
    setSub(true);
    try {
      const res = await fetch("/api/user-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [configKey]: num }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        toast(`${humanizeIdentifier(configKey)} → ${num}`);
        onSaved();
      } else {
        toast(body.error ?? `HTTP ${res.status}`, "error");
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "request failed", "error");
    } finally {
      setSub(false);
    }
  };

  return (
    <div className="bg-surface-2 px-4 py-3">
      <div className="text-xs text-[#ccc] mb-0.5">{humanizeIdentifier(configKey)}</div>
      {description && <div className="text-[10px] text-[#444] mb-1">{description}</div>}
      {bounds && (
        <div className="text-[10px] text-[#555] mb-2 font-body">
          range: {bounds[0]} – {bounds[1]}
        </div>
      )}
      <div className="flex gap-2 items-center">
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="flex-1 min-w-0 bg-surface-3 border-b border-[#1a1a1a]
                     px-3 py-1.5 text-xs font-body text-white
                     focus:outline-none focus:border-accent/50 transition-colors"
        />
        <button
          onClick={submit}
          disabled={!dirty || submitting}
          className="px-3 py-1.5 text-[10px] uppercase tracking-widest
                     bg-surface-3 text-[#a0a0a0] border border-[#1a1a1a]
                     hover:bg-accent-dim hover:text-accent hover:border-accent/30
                     disabled:opacity-30 disabled:cursor-not-allowed
                     transition-colors"
        >
          {submitting ? "..." : "Save"}
        </button>
      </div>
    </div>
  );
}

function ToggleRow({ label, desc, checked, onClick, disabled, readOnly }: {
  label: string;
  desc: string;
  checked: boolean;
  onClick?: () => void;
  disabled?: boolean;
  readOnly?: boolean;
}) {
  const interactive = !readOnly && onClick;
  return (
    <div className="px-4 py-3 flex items-center justify-between">
      <div>
        <div className="text-sm text-[#ccc]">{label}</div>
        <div className="text-[10px] text-[#444]">{desc}</div>
      </div>
      <button
        onClick={interactive ? onClick : undefined}
        disabled={disabled || readOnly}
        className={`relative w-10 h-[22px] rounded-full transition-colors ${
          checked ? "bg-accent" : "bg-[#333]"
        } ${disabled ? "opacity-50" : ""} ${readOnly ? "cursor-default" : "cursor-pointer"} disabled:cursor-default`}
      >
        <div className={`absolute top-[3px] w-4 h-4 rounded-full bg-white transition-transform ${
          checked ? "left-[21px]" : "left-[3px]"
        }`} />
      </button>
    </div>
  );
}
