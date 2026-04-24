"use client";

import { useEffect, useMemo, useState } from "react";
import { getJSON } from "@/lib/fetch-json";
import "../../../styles/content.css";

type BotConfig = {
  min_p_win: number;
  base_stake_pct: number;
  max_stake_pct: number;
  daily_loss_limit_pct: number;
  weekly_loss_limit_pct: number;
  drawdown_halt_pct: number;
  streak_cooldown_losses: number;
  dry_powder_reserve_pct: number;
  archetype_skip_list: string[];
  archetype_stake_multipliers: Record<string, number>;
};

const ARCHETYPE_MULTIPLIER_MIN = 0.1;
const ARCHETYPE_MULTIPLIER_MAX = 10.0;

type Bounds = Record<string, { min: number; max: number }>;

type UserConfigPayload = {
  user_id: string;
  config: BotConfig;
  bounds: Bounds;
  descriptions: Record<string, string>;
};

type Summary = {
  bankroll: number | null;
  equity: number | null;
  starting_cash: number | null;
  open_cost: number | null;
};

type BankrollPoint = { date: string; bankroll: number };

type Diagnostics = {
  system?: { bankroll_series?: BankrollPoint[] };
};

type ArchetypeItem = { id: string; label: string; group: string };

const BUILTIN_ARCHETYPES: ArchetypeItem[] = [
  { id: "tennis",              label: "Tennis",                 group: "Sports" },
  { id: "basketball",          label: "Basketball",             group: "Sports" },
  { id: "baseball",            label: "Baseball",               group: "Sports" },
  { id: "football",            label: "Football",               group: "Sports" },
  { id: "hockey",              label: "Hockey",                 group: "Sports" },
  { id: "cricket",             label: "Cricket",                group: "Sports" },
  { id: "esports",             label: "Esports",                group: "Sports" },
  { id: "soccer",              label: "Soccer",                 group: "Sports" },
  { id: "sports_other",        label: "Other sports",           group: "Sports" },
  { id: "price_threshold",     label: "Price threshold",        group: "Markets" },
  { id: "activity_count",      label: "Activity count",         group: "Markets" },
  { id: "geopolitical_event",  label: "Geopolitical event",     group: "Markets" },
  { id: "binary_event",        label: "Binary event",           group: "Markets" },
];

type ArchetypesPayload = { canonical: string[]; discovered: string[] };

function humanizeArchetypeId(id: string): string {
  return id
    .split("_")
    .map((w) => (w.length === 0 ? w : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

function mergeArchetypes(
  builtin: ArchetypeItem[],
  payload: ArchetypesPayload | null,
  skipList: string[],
  multipliers: Record<string, number> | undefined,
): ArchetypeItem[] {
  const seen = new Set<string>();
  const out: ArchetypeItem[] = [];
  for (const item of builtin) {
    seen.add(item.id);
    out.push(item);
  }
  const extras: string[] = [];
  if (payload) {
    for (const id of payload.canonical) if (!seen.has(id)) extras.push(id);
    for (const id of payload.discovered) if (!seen.has(id)) extras.push(id);
  }
  for (const id of skipList) if (!seen.has(id)) extras.push(id);
  for (const id of Object.keys(multipliers ?? {})) if (!seen.has(id)) extras.push(id);
  const uniqExtras = Array.from(new Set(extras));
  for (const id of uniqExtras) {
    seen.add(id);
    out.push({ id, label: humanizeArchetypeId(id), group: "Other" });
  }
  return out;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function clampMultiplier(n: number): number {
  if (!Number.isFinite(n)) return 1.0;
  return Math.max(ARCHETYPE_MULTIPLIER_MIN, Math.min(ARCHETYPE_MULTIPLIER_MAX, n));
}

export default function RiskPage() {
  const [payload, setPayload] = useState<UserConfigPayload | null>(null);
  const [draft, setDraft] = useState<BotConfig | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [archetypes, setArchetypes] = useState<ArchetypesPayload | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [cfg, sum, d, arch] = await Promise.all([
        getJSON<UserConfigPayload>("/api/user-config"),
        getJSON<Summary>("/api/summary"),
        getJSON<Diagnostics>("/api/diagnostics?scope=all"),
        getJSON<ArchetypesPayload>("/api/archetypes").catch(() => null),
      ]);
      if (cancelled) return;
      if (cfg) {
        setPayload(cfg);
        setDraft((prev) => prev ?? cfg.config);
      }
      if (sum) setSummary(sum);
      if (d) setDiag(d);
      if (arch) setArchetypes(arch);
      setLoaded(true);
    };
    load();
    const id = setInterval(() => {
      getJSON<Summary>("/api/summary").then((s) => {
        if (!cancelled && s) setSummary(s);
      });
      getJSON<Diagnostics>("/api/diagnostics?scope=all").then((x) => {
        if (!cancelled && x) setDiag(x);
      });
    }, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const upd = <K extends keyof BotConfig>(k: K, v: BotConfig[K]) =>
    setDraft((prev) => (prev ? { ...prev, [k]: v } : prev));

  const reset = () => {
    if (payload) setDraft(payload.config);
  };

  const dirty = useMemo(() => {
    if (!payload || !draft) return false;
    const keys = Object.keys(payload.config) as (keyof BotConfig)[];
    return keys.some((k) => {
      const a = payload.config[k];
      const b = draft[k];
      if (Array.isArray(a) && Array.isArray(b)) {
        return a.length !== b.length || a.some((x, i) => x !== b[i]);
      }
      if (isPlainObject(a) && isPlainObject(b)) {
        return JSON.stringify(a) !== JSON.stringify(b);
      }
      return a !== b;
    });
  }, [payload, draft]);

  const archetypeItems = useMemo(
    () => mergeArchetypes(
      BUILTIN_ARCHETYPES,
      archetypes,
      draft?.archetype_skip_list ?? [],
      draft?.archetype_stake_multipliers,
    ),
    [archetypes, draft?.archetype_skip_list, draft?.archetype_stake_multipliers],
  );

  const save = async () => {
    if (!payload || !draft || saving) return;
    setSaving(true);
    setSaveMsg(null);

    const changes: Record<string, unknown> = {};
    const keys = Object.keys(payload.config) as (keyof BotConfig)[];
    for (const k of keys) {
      const a = payload.config[k];
      const b = draft[k];
      let changed: boolean;
      if (Array.isArray(a) && Array.isArray(b)) {
        changed = a.length !== b.length || a.some((x, i) => x !== b[i]);
      } else if (isPlainObject(a) && isPlainObject(b)) {
        changed = JSON.stringify(a) !== JSON.stringify(b);
      } else {
        changed = a !== b;
      }
      if (changed) changes[k] = b;
    }

    try {
      const r = await fetch("/api/user-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        setSaveMsg(body?.error ?? `Save failed (${r.status}).`);
      } else {
        setSaveMsg("Saved.");
        const refreshed = await getJSON<UserConfigPayload>("/api/user-config");
        if (refreshed) {
          setPayload(refreshed);
          setDraft(refreshed.config);
        }
      }
    } catch (err) {
      setSaveMsg(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  const bounds = payload?.bounds ?? {};

  if (!draft) {
    return (
      <div className="page-wrap">
        <div className="page-head">
          <div className="page-head-row">
            <div>
              <h1 className="page-h1">Risk controls</h1>
              <p className="page-sub">Loading your current limits…</p>
            </div>
          </div>
        </div>
        <div className="panel">
          <div className="empty-state">{loaded ? "Couldn't reach the bot." : "Loading..."}</div>
        </div>
      </div>
    );
  }

  // The diagnostics endpoint returns `bankroll_series` as cumulative P&L
  // starting at 0 - NOT real bankroll dollars. Convert to actual bankroll by
  // adding starting_cash to every point, so peak / drawdown / daily-loss math
  // stays consistent with the bankroll shown everywhere else.
  const startingCash = summary?.starting_cash ?? 0;
  const pnlSeries = diag?.system?.bankroll_series ?? [];
  const bankrollHistory = pnlSeries.map((p) => startingCash + p.bankroll);

  const currentBankroll =
    summary?.bankroll ??
    (bankrollHistory.length ? bankrollHistory[bankrollHistory.length - 1] : startingCash);

  // Peak bankroll floored at starting_cash so "peak $0" can never happen.
  const peak = bankrollHistory.length
    ? Math.max(startingCash, ...bankrollHistory)
    : startingCash;

  const drawdownDollars =
    currentBankroll != null ? Math.max(0, peak - currentBankroll) : 0;
  const drawdown = peak > 0 ? drawdownDollars / peak : 0;

  // Yesterday's bankroll from the penultimate point in the series.
  const prevBankroll =
    bankrollHistory.length >= 2
      ? bankrollHistory[bankrollHistory.length - 2]
      : null;

  // Clamp to non-negative: a gain since yesterday is not a loss.
  const dailyLossDollars =
    prevBankroll != null && currentBankroll != null
      ? Math.max(0, prevBankroll - currentBankroll)
      : 0;
  const dailyLossPct =
    prevBankroll != null && prevBankroll > 0
      ? dailyLossDollars / prevBankroll
      : 0;
  const dailyCapDollars =
    prevBankroll != null ? prevBankroll * draft.daily_loss_limit_pct : null;

  const openCost = summary?.open_cost ?? 0;
  const exposurePct =
    currentBankroll && currentBankroll > 0 ? openCost / currentBankroll : 0;
  const exposureCapFraction = Math.max(0.0001, 1 - draft.dry_powder_reserve_pct);

  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Risk controls</h1>
            <p className="page-sub">
              Your protection envelope. These rules run identically in Simulation and Live, so what you see
              in simulation is what you get in live.
            </p>
          </div>
          <div className="page-head-right">
            <button className="btn-sm" onClick={reset} disabled={!dirty || saving}>
              Reset changes
            </button>
            <button className="btn-sm gold" onClick={save} disabled={!dirty || saving}>
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </div>
        {saveMsg && (
          <div style={{ marginTop: 8, fontSize: 13, color: "var(--vellum-60)" }}>{saveMsg}</div>
        )}
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Loss caps</h2>
        </div>

        <PctSlider
          label="Daily loss cap"
          desc="Delfi stops opening new positions after losing this much of bankroll in a single day."
          field="daily_loss_limit_pct"
          bounds={bounds}
          value={draft.daily_loss_limit_pct}
          onChange={(v) => upd("daily_loss_limit_pct", v)}
        />
        <PctSlider
          label="Weekly loss cap"
          desc="Halt trading for the rest of the week if cumulative losses exceed this share of bankroll."
          field="weekly_loss_limit_pct"
          bounds={bounds}
          value={draft.weekly_loss_limit_pct}
          onChange={(v) => upd("weekly_loss_limit_pct", v)}
        />
        <PctSlider
          label="Drawdown halt"
          desc="Total drawdown from peak that triggers a manual-review halt."
          field="drawdown_halt_pct"
          bounds={bounds}
          value={draft.drawdown_halt_pct}
          onChange={(v) => upd("drawdown_halt_pct", v)}
        />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Position sizing</h2>
        </div>

        <PctSlider
          label="Baseline stake"
          desc="Default position size when confidence is around 0.5."
          field="base_stake_pct"
          bounds={bounds}
          step={0.001}
          decimals={1}
          value={draft.base_stake_pct}
          onChange={(v) => upd("base_stake_pct", v)}
        />
        <PctSlider
          label="Maximum stake per trade"
          desc="Hard ceiling on any single position, regardless of confidence."
          field="max_stake_pct"
          bounds={bounds}
          step={0.005}
          value={draft.max_stake_pct}
          onChange={(v) => upd("max_stake_pct", v)}
        />
        <PctSlider
          label="Dry powder reserve"
          desc="Share of bankroll Delfi will never deploy. Held in reserve for exceptional opportunities."
          field="dry_powder_reserve_pct"
          bounds={bounds}
          value={draft.dry_powder_reserve_pct}
          onChange={(v) => upd("dry_powder_reserve_pct", v)}
        />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Trade selection</h2>
        </div>

        <PctSlider
          label="Minimum chosen-side probability"
          desc="Delfi's probability for the chosen side must clear this floor. Below it, the trade is skipped."
          field="min_p_win"
          bounds={bounds}
          value={draft.min_p_win}
          onChange={(v) => upd("min_p_win", v)}
        />
        <IntSlider
          label="Streak cooldown"
          desc="Halve stake for 5 trades after this many consecutive losses."
          field="streak_cooldown_losses"
          bounds={bounds}
          value={draft.streak_cooldown_losses}
          onChange={(v) => upd("streak_cooldown_losses", v)}
        />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Market categories</h2>
        </div>
        <p className="slider-desc" style={{ marginBottom: 16 }}>
          Delfi classifies every market into one of these archetypes.
          Disallowed archetypes are skipped before sizing.
        </p>
        <ArchetypeMatrix
          items={archetypeItems}
          skipList={draft.archetype_skip_list}
          onToggle={(id) =>
            setDraft((prev) => {
              if (!prev) return prev;
              const list = prev.archetype_skip_list.includes(id)
                ? prev.archetype_skip_list.filter((x) => x !== id)
                : [...prev.archetype_skip_list, id];
              return { ...prev, archetype_skip_list: list };
            })
          }
        />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Category stake multipliers</h2>
        </div>
        <p className="slider-desc" style={{ marginBottom: 16 }}>
          1.0x means Delfi uses its default stake for that category. Go above 1x
          to up-size categories that Delfi has proven profitable on; drop below
          1x to shrink stake on noisier categories. Delfi proposes changes after
          every 25 settled trades in a category; you can also adjust directly.
        </p>
        <ArchetypeMultipliersPanel
          items={archetypeItems}
          multipliers={draft.archetype_stake_multipliers ?? {}}
          skipList={draft.archetype_skip_list}
          onChange={(id, value) =>
            setDraft((prev) => {
              if (!prev) return prev;
              const next = { ...(prev.archetype_stake_multipliers ?? {}) };
              if (value === null) {
                delete next[id];
              } else {
                next[id] = clampMultiplier(value);
              }
              return { ...prev, archetype_stake_multipliers: next };
            })
          }
        />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Current usage</h2>
          <span className="panel-meta">Today</span>
        </div>

        <GaugeRow
          label="Daily loss"
          desc={
            prevBankroll == null
              ? "Starts counting once today's first full day closes."
              : dailyLossDollars > 0
              ? `Lost $${dailyLossDollars.toFixed(0)} since yesterday. Cap: ${(draft.daily_loss_limit_pct * 100).toFixed(0)}% of bankroll${dailyCapDollars != null ? ` ($${dailyCapDollars.toFixed(0)})` : ""}.`
              : `No losses today. Cap: ${(draft.daily_loss_limit_pct * 100).toFixed(0)}% of bankroll${dailyCapDollars != null ? ` ($${dailyCapDollars.toFixed(0)})` : ""}.`
          }
          pct={dailyLossPct / Math.max(0.0001, draft.daily_loss_limit_pct)}
          valueLabel={`${(dailyLossPct * 100).toFixed(1)}% of cap`}
          tone={dailyLossPct >= draft.daily_loss_limit_pct ? "warn" : "ok"}
        />
        <GaugeRow
          label="Drawdown from peak"
          desc={
            peak <= startingCash && drawdownDollars === 0
              ? `Peak $${peak.toFixed(0)} (no higher balance reached yet). Halt at ${(draft.drawdown_halt_pct * 100).toFixed(0)}%.`
              : drawdownDollars === 0
              ? `At peak bankroll $${peak.toFixed(0)}. Halt at ${(draft.drawdown_halt_pct * 100).toFixed(0)}%.`
              : `Down $${drawdownDollars.toFixed(0)} from peak $${peak.toFixed(0)}. Halt at ${(draft.drawdown_halt_pct * 100).toFixed(0)}%.`
          }
          pct={drawdown / Math.max(0.0001, draft.drawdown_halt_pct)}
          valueLabel={`${(drawdown * 100).toFixed(1)}% of halt`}
          tone={drawdown >= draft.drawdown_halt_pct ? "warn" : "ok"}
        />
        <GaugeRow
          label="Gross exposure"
          desc={
            openCost === 0 || !currentBankroll
              ? `No open positions. Reserve: ${(draft.dry_powder_reserve_pct * 100).toFixed(0)}% of bankroll.`
              : `$${openCost.toFixed(0)} deployed of $${currentBankroll.toFixed(0)} bankroll. Reserve: ${(draft.dry_powder_reserve_pct * 100).toFixed(0)}%.`
          }
          pct={exposurePct / exposureCapFraction}
          valueLabel={`${(exposurePct * 100).toFixed(0)}% of cap`}
          tone={exposurePct >= exposureCapFraction ? "warn" : "ok"}
        />
      </div>
    </div>
  );
}

function PctSlider({
  label, desc, field, bounds, value, onChange, step = 0.01, decimals = 0,
}: {
  label: string; desc: string; field: string; bounds: Bounds;
  value: number; onChange: (v: number) => void;
  step?: number; decimals?: number;
}) {
  const b = bounds[field];
  const min = b?.min ?? 0;
  const max = b?.max ?? 1;
  return (
    <div className="slider-row">
      <div>
        <div className="slider-label">{label}</div>
        <div className="slider-desc">{desc}</div>
      </div>
      <input
        type="range"
        className="slider-input"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <div className="slider-val">{(value * 100).toFixed(decimals)}%</div>
    </div>
  );
}

function IntSlider({
  label, desc, field, bounds, value, onChange,
}: {
  label: string; desc: string; field: string; bounds: Bounds;
  value: number; onChange: (v: number) => void;
}) {
  const b = bounds[field];
  const min = b?.min ?? 0;
  const max = b?.max ?? 10;
  return (
    <div className="slider-row">
      <div>
        <div className="slider-label">{label}</div>
        <div className="slider-desc">{desc}</div>
      </div>
      <input
        type="range"
        className="slider-input"
        min={min}
        max={max}
        step={1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <div className="slider-val">{value}</div>
    </div>
  );
}

function GaugeRow({
  label, desc, pct, valueLabel, tone,
}: {
  label: string; desc: string; pct: number; valueLabel: string;
  tone: "ok" | "warn";
}) {
  const clamped = Math.max(0, Math.min(1, pct));
  const color = tone === "warn" ? "var(--gold)" : "var(--teal)";
  return (
    <div className="slider-row">
      <div>
        <div className="slider-label">{label}</div>
        <div className="slider-desc">{desc}</div>
      </div>
      <div className="slider-val" style={{ width: 220 }}>
        <div style={{ height: 6, background: "rgba(232, 228, 216, 0.1)", borderRadius: 3, overflow: "hidden" }}>
          <div style={{ width: `${(clamped * 100).toFixed(1)}%`, height: "100%", background: color }}></div>
        </div>
      </div>
      <div className="slider-val">{valueLabel}</div>
    </div>
  );
}

function ArchetypeMatrix({
  items, skipList, onToggle,
}: {
  items: { id: string; label: string; group: string }[];
  skipList: string[];
  onToggle: (id: string) => void;
}) {
  const groups = Array.from(new Set(items.map((i) => i.group)));
  return (
    <div>
      {groups.map((g) => (
        <div key={g} style={{ marginBottom: 16 }}>
          <div
            className="slider-label"
            style={{ marginBottom: 8, color: "var(--vellum-60, rgba(232,228,216,0.6))", fontSize: 12, letterSpacing: 0.5, textTransform: "uppercase" }}
          >
            {g}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {items
              .filter((i) => i.group === g)
              .map((i) => {
                const disallowed = skipList.includes(i.id);
                return (
                  <button
                    key={i.id}
                    type="button"
                    onClick={() => onToggle(i.id)}
                    title={disallowed ? "Disallowed - click to allow" : "Allowed - click to disallow"}
                    style={{
                      padding: "6px 12px",
                      fontSize: 13,
                      borderRadius: 999,
                      cursor: "pointer",
                      border: "1px solid",
                      transition: "all 0.15s ease",
                      borderColor: disallowed
                        ? "rgba(232, 228, 216, 0.15)"
                        : "rgba(201, 162, 39, 0.5)",
                      background: disallowed
                        ? "transparent"
                        : "rgba(201, 162, 39, 0.08)",
                      color: disallowed
                        ? "var(--vellum-40, rgba(232,228,216,0.4))"
                        : "var(--vellum)",
                      textDecoration: disallowed ? "line-through" : "none",
                    }}
                  >
                    {i.label}
                  </button>
                );
              })}
          </div>
        </div>
      ))}
    </div>
  );
}

function ArchetypeMultipliersPanel({
  items, multipliers, skipList, onChange,
}: {
  items: { id: string; label: string; group: string }[];
  multipliers: Record<string, number>;
  skipList: string[];
  onChange: (id: string, value: number | null) => void;
}) {
  const groups = Array.from(new Set(items.map((i) => i.group)));
  return (
    <div>
      {groups.map((g) => (
        <div key={g} style={{ marginBottom: 20 }}>
          <div
            style={{
              marginBottom: 8,
              color: "var(--vellum-60, rgba(232,228,216,0.6))",
              fontSize: 12,
              letterSpacing: 0.5,
              textTransform: "uppercase",
            }}
          >
            {g}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {items
              .filter((i) => i.group === g)
              .map((i) => {
                const skipped = skipList.includes(i.id);
                const value = multipliers[i.id];
                const hasOverride = value !== undefined;
                const display = hasOverride ? value : 1.0;
                return (
                  <div
                    key={i.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 12,
                      opacity: skipped ? 0.5 : 1,
                    }}
                  >
                    <div style={{ minWidth: 200 }}>
                      <div style={{ fontSize: 14 }}>
                        {i.label}
                        {skipped && (
                          <span style={{ marginLeft: 8, fontSize: 11, color: "var(--vellum-40)" }}>
                            skipped - multiplier ignored
                          </span>
                        )}
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <input
                        type="number"
                        step={0.05}
                        min={ARCHETYPE_MULTIPLIER_MIN}
                        max={ARCHETYPE_MULTIPLIER_MAX}
                        value={display}
                        disabled={skipped}
                        onChange={(e) => {
                          const raw = e.target.value;
                          if (raw === "") {
                            onChange(i.id, null);
                            return;
                          }
                          const n = Number(raw);
                          if (Number.isFinite(n)) onChange(i.id, n);
                        }}
                        style={{
                          width: 90,
                          padding: "4px 8px",
                          background: "rgba(232, 228, 216, 0.05)",
                          border: "1px solid rgba(232, 228, 216, 0.15)",
                          borderRadius: 4,
                          color: "var(--vellum)",
                          fontSize: 13,
                          textAlign: "right",
                        }}
                      />
                      <span style={{ fontSize: 12, color: "var(--vellum-60)" }}>x</span>
                      {hasOverride && !skipped && (
                        <button
                          type="button"
                          onClick={() => onChange(i.id, null)}
                          style={{
                            background: "transparent",
                            border: "none",
                            color: "var(--vellum-40)",
                            cursor: "pointer",
                            fontSize: 11,
                            padding: "2px 6px",
                          }}
                          title="Reset to 1.0x default"
                        >
                          reset
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
          </div>
        </div>
      ))}
    </div>
  );
}
