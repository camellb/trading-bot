"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ToastFn } from "@/lib/format";
import { usd } from "@/lib/format";

/* ── Types ─────────────────────────────────────────────────────────── */

/** Raw shape from /api/risk — matches risk_manager.get_risk_state() */
type RawRiskState = {
  mode: string;
  starting_cash: number;
  current_bankroll: number;
  peak_bankroll: number;
  daily_pnl: number;
  daily_limit_usd: number;
  daily_limit_pct: number;
  daily_limit_breached: boolean;
  weekly_pnl: number;
  weekly_limit_usd: number;
  weekly_limit_pct: number;
  weekly_limit_breached: boolean;
  open_cost: number;
  heat_limit_usd: number;
  heat_limit_pct: number;
  heat_pct: number;
  heat_breached: boolean;
  drawdown_pct: number;
  drawdown_halt_pct: number;
  drawdown_halted: boolean;
  consecutive_losses: number;
  cooldown_trades_remaining: number;
  loss_streak_threshold: number;
};

/* ── Fetch helper ──────────────────────────────────────────────────── */

async function fetchJson<T>(url: string, signal: AbortSignal): Promise<T | null> {
  try {
    const res = await fetch(url, { signal, cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

/* ── Risk control descriptions ────────────────────────────────────── */

const RISK_DESCRIPTIONS: Record<string, { title: string; description: string; configKey: string; disableValue: string }> = {
  heat: {
    title: "Portfolio Heat",
    description:
      "Percentage of your bankroll currently deployed in open positions. " +
      "When heat exceeds the limit, the bot won't open new positions until " +
      "existing ones settle. This prevents over-exposure.",
    configKey: "PM_MAX_PORTFOLIO_HEAT_PCT",
    disableValue: "1.0",
  },
  drawdown: {
    title: "Drawdown Circuit Breaker",
    description:
      "Tracks how far the portfolio has fallen from its peak value. " +
      "If drawdown exceeds the threshold, ALL trading is halted until " +
      "you manually resume. This is a last-resort safety net.",
    configKey: "PM_DRAWDOWN_HALT_PCT",
    disableValue: "0.01",
  },
  daily: {
    title: "Daily Loss Limit",
    description:
      "Maximum loss allowed in a single day as a percentage of starting bankroll. " +
      "Once breached, no new trades open until the next calendar day.",
    configKey: "PM_DAILY_LOSS_LIMIT_PCT",
    disableValue: "1.0",
  },
  weekly: {
    title: "Weekly Loss Limit",
    description:
      "Maximum loss allowed in a rolling 7-day window as a percentage of starting bankroll. " +
      "Once breached, no new trades open until losses recover.",
    configKey: "PM_WEEKLY_LOSS_LIMIT_PCT",
    disableValue: "1.0",
  },
};

/* ── Component ─────────────────────────────────────────────────────── */

export function RiskView({ toast }: { toast: ToastFn }) {
  const [loading, setLoading] = useState(true);
  const [risk, setRisk] = useState<RawRiskState | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval>>(undefined);

  const fetchData = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const r = await fetchJson<RawRiskState>("/api/risk", controller.signal);

    if (controller.signal.aborted) return;
    setRisk(r);
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchData();
    intervalRef.current = setInterval(fetchData, 10_000);
    return () => {
      abortRef.current?.abort();
      clearInterval(intervalRef.current);
    };
  }, [fetchData]);

  const toggleRiskControl = async (configKey: string, currentPct: number, disableValue: string) => {
    const isDisabled = isControlDisabled(configKey, currentPct);
    const defaults: Record<string, string> = {
      PM_MAX_PORTFOLIO_HEAT_PCT: "0.30",
      PM_DRAWDOWN_HALT_PCT: "0.60",
      PM_DAILY_LOSS_LIMIT_PCT: "0.10",
      PM_WEEKLY_LOSS_LIMIT_PCT: "0.20",
    };
    const newValue = isDisabled ? defaults[configKey] ?? "0.30" : disableValue;

    try {
      const res = await fetch("/api/update-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: configKey, value: parseFloat(newValue) }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data.status === "applied") {
          toast(isDisabled ? "Control re-enabled" : "Control disabled");
        } else if (data.status === "pending") {
          toast("Change pending — confirm via Telegram");
        }
        fetchData();
      } else {
        const data = await res.json().catch(() => null);
        toast(data?.reason ?? "Failed to update config", "error");
      }
    } catch {
      toast("Request failed", "error");
    }
  };

  /* ── Alert state ──────────────────────────────────────────────────── */

  const hasRedAlert = risk?.drawdown_halted;
  const hasYellowAlert = risk?.daily_limit_breached || risk?.weekly_limit_breached || risk?.heat_breached;
  const statusColor = hasRedAlert ? "bg-red-400" : hasYellowAlert ? "bg-yellow-400" : "bg-accent";

  return (
    <div className="space-y-6 max-w-[1400px]">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold text-white">Risk Management</h1>
          <span className={`w-2.5 h-2.5 rounded-full ${statusColor}`} />
        </div>
      </div>

      {/* Alert Banners */}
      {risk?.drawdown_halted && (
        <AlertBanner
          level="red"
          message={`DRAWDOWN HALT TRIGGERED — Portfolio drawdown at ${pctStr(risk.drawdown_pct)} (limit: ${pctStr(risk.drawdown_halt_pct)}). All trading is suspended.`}
        />
      )}
      {risk?.daily_limit_breached && (
        <AlertBanner
          level="yellow"
          message={`Daily loss limit breached: ${usd(risk.daily_pnl, { sign: true })} (limit: -${usd(risk.daily_limit_usd)})`}
        />
      )}
      {risk?.weekly_limit_breached && (
        <AlertBanner
          level="yellow"
          message={`Weekly loss limit breached: ${usd(risk.weekly_pnl, { sign: true })} (limit: -${usd(risk.weekly_limit_usd)})`}
        />
      )}
      {risk?.heat_breached && (
        <AlertBanner
          level="yellow"
          message={`Portfolio heat breached: ${pctStr(risk.heat_pct)} deployed (limit: ${pctStr(risk.heat_limit_pct)})`}
        />
      )}

      {/* Risk Gauges */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <RiskGauge
          label="Daily P&L"
          current={risk?.daily_pnl ?? null}
          limit={risk?.daily_limit_usd ?? null}
          limitPct={risk?.daily_limit_pct ?? null}
          formatValue={(v) => usd(v, { sign: true })}
          loading={loading}
          breached={risk?.daily_limit_breached}
          invert
        />
        <RiskGauge
          label="Weekly P&L"
          current={risk?.weekly_pnl ?? null}
          limit={risk?.weekly_limit_usd ?? null}
          limitPct={risk?.weekly_limit_pct ?? null}
          formatValue={(v) => usd(v, { sign: true })}
          loading={loading}
          breached={risk?.weekly_limit_breached}
          invert
        />
        <RiskGauge
          label="Portfolio Heat"
          current={risk?.heat_pct ?? null}
          limit={risk?.heat_limit_pct ?? null}
          limitPct={risk?.heat_limit_pct ?? null}
          formatValue={pctStr}
          loading={loading}
          breached={risk?.heat_breached}
        />
        <RiskGauge
          label="Drawdown"
          current={risk?.drawdown_pct ?? null}
          limit={risk?.drawdown_halt_pct ?? null}
          limitPct={risk?.drawdown_halt_pct ?? null}
          formatValue={pctStr}
          loading={loading}
          breached={risk?.drawdown_halted}
        />
      </div>

      {/* Risk Controls with Descriptions & Toggles */}
      <div className="bg-surface-2 border border-[#1a1a1a] p-5">
        <h3 className="text-[11px] uppercase tracking-widest text-[#666] mb-4">Risk Limits</h3>
        <p className="text-xs text-[#666] mb-5">
          These guardrails prevent excessive losses. You can disable any control,
          but the bot will trade without that safety net. Changes require Telegram confirmation.
        </p>
        {loading ? (
          <LoadingSkeleton rows={4} />
        ) : (
          <div className="space-y-4">
            <RiskControlRow
              meta={RISK_DESCRIPTIONS.daily}
              currentPct={risk?.daily_limit_pct ?? 0.10}
              extraInfo={risk ? `(${usd(risk.daily_limit_usd)})` : ""}
              toast={toast}
              onRefresh={fetchData}
              onToggle={toggleRiskControl}
            />
            <RiskControlRow
              meta={RISK_DESCRIPTIONS.weekly}
              currentPct={risk?.weekly_limit_pct ?? 0.20}
              extraInfo={risk ? `(${usd(risk.weekly_limit_usd)})` : ""}
              toast={toast}
              onRefresh={fetchData}
              onToggle={toggleRiskControl}
            />
            <RiskControlRow
              meta={RISK_DESCRIPTIONS.heat}
              currentPct={risk?.heat_limit_pct ?? 0.30}
              extraInfo=""
              toast={toast}
              onRefresh={fetchData}
              onToggle={toggleRiskControl}
            />
            <RiskControlRow
              meta={RISK_DESCRIPTIONS.drawdown}
              currentPct={risk?.drawdown_halt_pct ?? 0.40}
              extraInfo=""
              toast={toast}
              onRefresh={fetchData}
              onToggle={toggleRiskControl}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Sub-components ─────────────────────────────────────────────────── */

function AlertBanner({ level, message }: { level: "red" | "yellow"; message: string }) {
  return (
    <div className={`px-4 py-3 text-sm border ${
      level === "red"
        ? "bg-red-950/80 border-red-500/30 text-red-200"
        : "bg-yellow-950/80 border-yellow-500/30 text-yellow-200"
    }`}>
      <div className="flex items-center gap-2">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
        {message}
      </div>
    </div>
  );
}

function RiskGauge({
  label,
  current,
  limit,
  limitPct,
  formatValue,
  loading,
  breached,
  invert,
}: {
  label: string;
  current: number | null;
  limit: number | null;
  limitPct: number | null;
  formatValue: (v: number) => string;
  loading: boolean;
  breached?: boolean;
  invert?: boolean;
}) {
  // Determine if this control is effectively disabled (OFF).
  const isOff = limitPct != null && limitPct >= 0.95;

  let pct = 0;
  if (!isOff && current != null && limit != null && limit !== 0) {
    if (invert) {
      pct = Math.min(Math.abs(current / limit) * 100, 100);
    } else {
      pct = Math.min((current / limit) * 100, 100);
    }
  }

  const barColor = breached
    ? "bg-red-400"
    : pct > 75
      ? "bg-yellow-400"
      : "bg-accent";

  return (
    <div className="bg-surface-2 border border-[#1a1a1a] p-4">
      <div className="text-[11px] uppercase tracking-widest text-[#666] mb-2">{label}</div>
      {loading ? (
        <div className="h-8 w-20 bg-surface-3 animate-pulse" />
      ) : (
        <>
          <div className={`text-lg font-semibold font-body mb-2 ${
            breached ? "text-red-400" : "text-white"
          }`}>
            {current != null ? formatValue(current) : "--"}
          </div>
          {isOff ? (
            /* When the control is OFF, show no progress bar */
            <div className="text-[10px] text-[#444] mt-1">
              Limit: <span className="text-yellow-400/70">OFF</span>
            </div>
          ) : (
            <>
              <div className="w-full h-2 bg-surface-3 overflow-hidden">
                <div
                  className={`h-full transition-all duration-500 ${barColor}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <div className="text-[10px] text-[#444] mt-1">
                Limit: {limit != null ? formatValue(limit) : "--"}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Determines if a risk control is effectively disabled.
 * Heat/daily/weekly at 100% or drawdown below 5% = effectively off.
 */
function isControlDisabled(configKey: string, currentPct: number): boolean {
  if (configKey === "PM_DRAWDOWN_HALT_PCT") {
    return currentPct >= 0.95;
  }
  return currentPct >= 0.95;
}

function RiskControlRow({
  meta,
  currentPct,
  extraInfo,
  toast,
  onRefresh,
  onToggle,
}: {
  meta: { title: string; description: string; configKey: string; disableValue: string };
  currentPct: number;
  extraInfo: string;
  toast: ToastFn;
  onRefresh: () => void;
  onToggle: (configKey: string, currentPct: number, disableValue: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [inputVal, setInputVal] = useState("");
  const [saving, setSaving] = useState(false);
  const disabled = isControlDisabled(meta.configKey, currentPct);

  const startEditing = () => {
    setInputVal((currentPct * 100).toFixed(0));
    setEditing(true);
  };

  const cancelEditing = () => {
    setEditing(false);
    setInputVal("");
  };

  const saveValue = async () => {
    const num = parseFloat(inputVal);
    if (isNaN(num) || num <= 0 || num > 100) {
      toast("Enter a value between 1 and 100", "error");
      return;
    }
    const decimal = num / 100;
    if (Math.abs(decimal - currentPct) < 0.001) {
      cancelEditing();
      return;
    }
    setSaving(true);
    try {
      const res = await fetch("/api/update-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: meta.configKey, value: decimal }),
      });
      if (res.ok) {
        toast(`${meta.title} updated to ${num}%`);
        cancelEditing();
        onRefresh();
      } else {
        toast("Failed to update", "error");
      }
    } catch {
      toast("Request failed", "error");
    } finally {
      setSaving(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") saveValue();
    if (e.key === "Escape") cancelEditing();
  };

  return (
    <div className="bg-surface-3 p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm text-white font-medium">{meta.title}</span>
            <span className={`text-[10px] px-1.5 py-0.5 font-medium ${
              disabled ? "bg-yellow-500/10 text-yellow-400" : "bg-accent-dim text-accent"
            }`}>
              {disabled ? "OFF" : "ON"}
            </span>
            <button
              onClick={() => setExpanded(!expanded)}
              className="text-[#444] hover:text-[#a0a0a0] transition-colors"
              title="What is this?"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
            </button>
          </div>

          {/* Editable value */}
          <div className="mt-1.5 flex items-center gap-2">
            {editing ? (
              <div className="flex items-center gap-1.5">
                <input
                  type="number"
                  min="1"
                  max="100"
                  step="1"
                  value={inputVal}
                  onChange={(e) => setInputVal(e.target.value)}
                  onKeyDown={handleKeyDown}
                  autoFocus
                  disabled={saving}
                  className="w-16 px-2 py-1 text-xs bg-surface-0 border border-accent/40 text-white font-body focus:outline-none focus:border-accent"
                />
                <span className="text-[11px] text-[#666]">%</span>
                <button
                  onClick={saveValue}
                  disabled={saving}
                  className="px-2 py-1 text-[10px] font-medium bg-accent-dim text-accent hover:bg-accent/20 disabled:opacity-50"
                >
                  Save
                </button>
                <button
                  onClick={cancelEditing}
                  className="px-2 py-1 text-[10px] font-medium text-[#666] hover:text-[#ccc]"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                onClick={startEditing}
                className="group flex items-center gap-1.5 text-[11px] text-[#a0a0a0] hover:text-white transition-colors"
              >
                <span className="font-body">
                  {pctStr(currentPct)}
                </span>
                {extraInfo && <span>{extraInfo}</span>}
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="opacity-0 group-hover:opacity-100 transition-opacity">
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                  <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                </svg>
              </button>
            )}
          </div>
        </div>

        <button
          onClick={() => onToggle(meta.configKey, currentPct, meta.disableValue)}
          className={`shrink-0 px-3 py-1.5 text-[10px] font-medium transition-colors ${
            disabled
              ? "bg-accent-dim text-accent border border-accent/20 hover:bg-accent/20"
              : "bg-danger-dim text-red-400 border border-red-500/20 hover:bg-red-500/20"
          }`}
        >
          {disabled ? "Enable" : "Disable"}
        </button>
      </div>
      {expanded && (
        <p className="text-xs text-[#a0a0a0] mt-3 leading-relaxed border-t border-[#1a1a1a]/50 pt-3">
          {meta.description}
        </p>
      )}
    </div>
  );
}

function LoadingSkeleton({ rows }: { rows: number }) {
  return (
    <div className="space-y-3">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-4 bg-surface-3 animate-pulse" style={{ width: `${70 + Math.random() * 30}%` }} />
      ))}
    </div>
  );
}

function pctStr(v: number | null | undefined): string {
  if (v == null) return "--";
  return `${(v * 100).toFixed(1)}%`;
}
