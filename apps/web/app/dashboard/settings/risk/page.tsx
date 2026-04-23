"use client";

import { useEffect, useMemo, useState } from "react";
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
};

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

const ALL_ARCHETYPES: { id: string; label: string; group: string }[] = [
  { id: "tennis_qualifier",    label: "Tennis - qualifier",     group: "Sports" },
  { id: "tennis_main_draw",    label: "Tennis - main draw",     group: "Sports" },
  { id: "tennis_lower_tier",   label: "Tennis - lower tier",    group: "Sports" },
  { id: "basketball_game",     label: "Basketball - game",      group: "Sports" },
  { id: "basketball_prop",     label: "Basketball - prop",      group: "Sports" },
  { id: "baseball_game",       label: "Baseball - game",        group: "Sports" },
  { id: "football_game",       label: "Football - game",        group: "Sports" },
  { id: "hockey_game",         label: "Hockey - game",          group: "Sports" },
  { id: "cricket_match",       label: "Cricket - match",        group: "Sports" },
  { id: "esports_match",       label: "Esports - match",        group: "Sports" },
  { id: "soccer_match",        label: "Soccer - match",         group: "Sports" },
  { id: "sports_other",        label: "Sports - other",         group: "Sports" },
  { id: "price_threshold",     label: "Price threshold",        group: "Markets" },
  { id: "activity_count",      label: "Activity count",         group: "Markets" },
  { id: "geopolitical_event",  label: "Geopolitical event",     group: "Markets" },
  { id: "binary_event",        label: "Binary event",           group: "Markets" },
];

async function getJSON<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export default function RiskPage() {
  const [payload, setPayload] = useState<UserConfigPayload | null>(null);
  const [draft, setDraft] = useState<BotConfig | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [cfg, sum, d] = await Promise.all([
        getJSON<UserConfigPayload>("/api/user-config"),
        getJSON<Summary>("/api/summary"),
        getJSON<Diagnostics>("/api/diagnostics?scope=all"),
      ]);
      if (cancelled) return;
      if (cfg) {
        setPayload(cfg);
        setDraft((prev) => prev ?? cfg.config);
      }
      if (sum) setSummary(sum);
      if (d) setDiag(d);
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
      return a !== b;
    });
  }, [payload, draft]);

  const save = async () => {
    if (!payload || !draft || saving) return;
    setSaving(true);
    setSaveMsg(null);

    const changes: Record<string, unknown> = {};
    const keys = Object.keys(payload.config) as (keyof BotConfig)[];
    for (const k of keys) {
      const a = payload.config[k];
      const b = draft[k];
      const changed = Array.isArray(a) && Array.isArray(b)
        ? (a.length !== b.length || a.some((x, i) => x !== b[i]))
        : a !== b;
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

  const series = diag?.system?.bankroll_series ?? [];
  const currentBankroll = summary?.bankroll ?? (series.length ? series[series.length - 1].bankroll : null);
  const peak = series.reduce((m, p) => Math.max(m, p.bankroll), 0);
  const drawdown = peak > 0 && currentBankroll != null
    ? Math.max(0, (peak - currentBankroll) / peak)
    : 0;

  const prevBankroll = series.length >= 2 ? series[series.length - 2].bankroll : null;
  const dailyLossPct = prevBankroll && prevBankroll > 0 && currentBankroll != null
    ? Math.max(0, (prevBankroll - currentBankroll) / prevBankroll)
    : 0;

  const exposurePct = currentBankroll && currentBankroll > 0 && summary?.open_cost != null
    ? summary.open_cost / currentBankroll
    : 0;

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
          <span className="panel-meta">Stop trading when losses hit a ceiling</span>
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
          <span className="panel-meta">How big each trade can be</span>
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
          <span className="panel-meta">What Delfi considers worth betting</span>
        </div>

        <PctSlider
          label="Minimum p_win"
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
          <span className="panel-meta">What Delfi is allowed to forecast</span>
        </div>
        <p className="slider-desc" style={{ marginBottom: 16 }}>
          Delfi classifies every market into one of these archetypes.
          Disallowed archetypes are skipped before sizing. Short-horizon
          tennis categories default off because resolution data shows they
          lose money at Delfi&apos;s current calibration.
        </p>
        <ArchetypeMatrix
          items={ALL_ARCHETYPES}
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
          <h2 className="panel-title">Current usage</h2>
          <span className="panel-meta">Today</span>
        </div>

        <GaugeRow
          label="Daily loss used"
          desc={
            prevBankroll && currentBankroll != null
              ? `$${(prevBankroll - currentBankroll).toFixed(0)} since yesterday · cap ${(draft.daily_loss_limit_pct * 100).toFixed(0)}%`
              : "Waiting for at least one closed day of bankroll history."
          }
          pct={dailyLossPct / Math.max(0.0001, draft.daily_loss_limit_pct)}
          valueLabel={`${(dailyLossPct * 100).toFixed(1)}%`}
          tone={dailyLossPct >= draft.daily_loss_limit_pct ? "warn" : "ok"}
        />
        <GaugeRow
          label="Drawdown"
          desc={
            peak > 0
              ? `${(drawdown * 100).toFixed(1)}% from peak $${peak.toFixed(0)} · halt at ${(draft.drawdown_halt_pct * 100).toFixed(0)}%`
              : "Bankroll series not yet available."
          }
          pct={drawdown / Math.max(0.0001, draft.drawdown_halt_pct)}
          valueLabel={`${(drawdown * 100).toFixed(1)}%`}
          tone={drawdown >= draft.drawdown_halt_pct ? "warn" : "ok"}
        />
        <GaugeRow
          label="Gross exposure"
          desc={
            summary?.open_cost != null && currentBankroll
              ? `$${summary.open_cost.toFixed(0)} deployed of $${currentBankroll.toFixed(0)} bankroll`
              : "No open positions."
          }
          pct={exposurePct / Math.max(0.0001, 1 - draft.dry_powder_reserve_pct)}
          valueLabel={summary?.open_cost != null ? `$${summary.open_cost.toFixed(0)}` : "$0"}
          tone="ok"
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
