import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ArchetypeCatalogue,
  ArchetypeEntry,
  isConnectionError,
} from "../api";

/**
 * Risk and sizing - top-level page.
 *
 * Promoted out of Settings on 2026-05-02 because it's the second-most
 * touched panel after Dashboard (every config change to stake or
 * archetype routes here) and Settings nesting was friction.
 *
 * Panels:
 *   1. Sizing and limits - the seven scalar risk knobs (base/max stake,
 *      loss limits, drawdown halt, streak cooldown, dry powder reserve).
 *   2. Archetypes - the per-archetype skip + multiplier grid.
 */

const BOUNDS = {
  base_stake_pct:        [0.005, 0.05] as const,
  max_stake_pct:         [0.01,  0.10] as const,
  daily_loss_limit_pct:  [0.01,  1.00] as const,
  weekly_loss_limit_pct: [0.01,  1.00] as const,
  drawdown_halt_pct:     [0.01,  1.00] as const,
  streak_cooldown_losses:[2,     10]   as const,
  dry_powder_reserve_pct:[0.10,  0.40] as const,
};

type ConfigShape = {
  base_stake_pct?: number;
  max_stake_pct?: number;
  daily_loss_limit_pct?: number;
  weekly_loss_limit_pct?: number;
  drawdown_halt_pct?: number;
  streak_cooldown_losses?: number;
  dry_powder_reserve_pct?: number;
  starting_cash?: number | null;
  archetype_skip_list?: string[];
  archetype_stake_multipliers?: Record<string, number>;
  [k: string]: unknown;
};

interface Props {
  config: ConfigShape | null;
  onSaved: () => void;
}

export default function Risk({ config, onSaved }: Props) {
  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Risk controls</h1>
          </div>
        </div>
      </div>

      <RiskPanel config={config} onSaved={onSaved} />
      <ResolutionWindowPanel config={config} onSaved={onSaved} />
      <ArchetypePanel onSaved={onSaved} />
    </div>
  );
}

// ── Price band constants (used only by per-archetype band controls) ──────
//
// V1 has no global price-band filter. Each archetype card has its own
// band row; these constants + helpers are shared between those cards
// and any future UI that reuses 10pp band semantics on raw market
// price (0-100).

const PRICE_BAND_COUNT = 10;
const PRICE_BAND_STEP  = 0.10;

// Build the canonical 10 buckets [0.00,0.10), [0.10,0.20), ..., [0.90,1.00].
const PRICE_BANDS: ReadonlyArray<readonly [number, number]> = Array.from(
  { length: PRICE_BAND_COUNT },
  (_, i) => [i * PRICE_BAND_STEP, (i + 1) * PRICE_BAND_STEP] as const,
);

function bandLabel(lo: number, hi: number): string {
  return `${Math.round(lo * 100)}-${Math.round(hi * 100)}`;
}

function bandKey(lo: number, hi: number): string {
  // Avoid floating-point equality games. Lo*100 rounded is unique per band.
  return `${Math.round(lo * 100)}_${Math.round(hi * 100)}`;
}

// ── Time-to-resolution window ────────────────────────────────────────────

const RESOLUTION_BOUNDS = [0, 30] as const;  // days; 0 = no constraint; 30 = 30 days

function ResolutionWindowPanel({
  config,
  onSaved,
}: {
  config: ConfigShape | null;
  onSaved: () => void;
}) {
  const [minDays, setMinDays] = useState("");
  const [maxDays, setMaxDays] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // Same one-shot sync as RiskPanel: read current persisted values
  // ONCE on first non-null arrival of `config`. Backend stores NULL
  // for "no constraint"; surface that as 0 in the input so the user
  // sees one consistent sentinel.
  const syncedRef = useRef(false);
  useEffect(() => {
    if (!config) return;
    if (syncedRef.current) return;
    syncedRef.current = true;
    const minVal = (config as { min_days_to_resolution?: number | null }).min_days_to_resolution;
    const maxVal = (config as { max_days_to_resolution?: number | null }).max_days_to_resolution;
    setMinDays(minVal != null ? String(minVal) : "0");
    setMaxDays(maxVal != null ? String(maxVal) : "0");
  }, [config]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const parse = (label: string, raw: string): number => {
        const n = Number(raw.trim() === "" ? "0" : raw);
        if (!Number.isInteger(n)) {
          throw new Error(`${label} must be a whole number of days.`);
        }
        const [lo, hi] = RESOLUTION_BOUNDS;
        if (n < lo || n > hi) {
          throw new Error(`${label} must be between ${lo} (off) and ${hi} days.`);
        }
        return n;
      };
      const minN = parse("Minimum", minDays);
      const maxN = parse("Maximum", maxDays);
      // Cross-field rule, mirrored on the backend:
      // 0 = "no constraint" so it never conflicts.
      if (minN > 0 && maxN > 0 && maxN < minN) {
        throw new Error(
          `Maximum (${maxN}d) must be at least the minimum (${minN}d). ` +
          `Set either to 0 to remove that side of the limit.`,
        );
      }
      await api.updateConfig({
        min_days_to_resolution: minN,
        max_days_to_resolution: maxN,
      });
      setMsg({ kind: "ok", text: "Saved." });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Time to resolution</h2>
        <span className="panel-meta">days, 0 = no limit</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Filter markets by how soon they resolve. Set a minimum to avoid
        markets settling within the next day or two; set a maximum to
        avoid long-dated markets where capital sits tied up. Either side
        at 0 means no constraint on that side.
      </p>
      <form onSubmit={save}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, maxWidth: 480 }}>
          <NumField
            label="Minimum days to resolution"
            step="1"
            range={RESOLUTION_BOUNDS}
            value={minDays}
            onChange={setMinDays}
          />
          <NumField
            label="Maximum days to resolution"
            step="1"
            range={RESOLUTION_BOUNDS}
            value={maxDays}
            onChange={setMaxDays}
          />
        </div>
        <div className="form-actions" style={{ marginTop: 18 }}>
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save resolution window"}
          </button>
          {msg && (
            <span className={msg.kind === "ok" ? "form-success" : "form-error"}>
              {msg.text}
            </span>
          )}
        </div>
      </form>
    </div>
  );
}

// ── Risk + sizing ────────────────────────────────────────────────────────

function RiskPanel({
  config,
  onSaved,
}: {
  config: ConfigShape | null;
  onSaved: () => void;
}) {
  const [risk, setRisk] = useState({
    base_stake_pct: "",
    max_stake_pct: "",
    daily_loss_limit_pct: "",
    weekly_loss_limit_pct: "",
    drawdown_halt_pct: "",
    streak_cooldown_losses: "",
    dry_powder_reserve_pct: "",
  });
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // Sync the form from `config` ONCE, on first non-null arrival. App's
  // 5-second poll re-renders this component with a fresh `config` object
  // every tick; without this guard the user's typed-but-not-yet-saved
  // values get clobbered every 5s, and clicking Save then writes back
  // the (just-clobbered) old values - the exact bug the user reported as
  // "showed saved but nothing changed".
  const syncedRef = useRef(false);
  useEffect(() => {
    if (!config) return;
    if (syncedRef.current) return;
    syncedRef.current = true;
    setRisk({
      base_stake_pct:         config.base_stake_pct         != null ? String(config.base_stake_pct)         : "",
      max_stake_pct:          config.max_stake_pct          != null ? String(config.max_stake_pct)          : "",
      daily_loss_limit_pct:   config.daily_loss_limit_pct   != null ? String(config.daily_loss_limit_pct)   : "",
      weekly_loss_limit_pct:  config.weekly_loss_limit_pct  != null ? String(config.weekly_loss_limit_pct)  : "",
      drawdown_halt_pct:      config.drawdown_halt_pct      != null ? String(config.drawdown_halt_pct)      : "",
      streak_cooldown_losses: config.streak_cooldown_losses != null ? String(config.streak_cooldown_losses) : "",
      dry_powder_reserve_pct: config.dry_powder_reserve_pct != null ? String(config.dry_powder_reserve_pct) : "",
    });
  }, [config]);

  const saveRisk = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const changes: Record<string, unknown> = {};
      const numericKeys = [
        "base_stake_pct", "max_stake_pct", "daily_loss_limit_pct",
        "weekly_loss_limit_pct", "drawdown_halt_pct", "dry_powder_reserve_pct",
      ] as const;
      for (const k of numericKeys) {
        const raw = risk[k].trim();
        if (raw === "") continue;
        const n = Number(raw);
        if (!Number.isFinite(n)) throw new Error(`${k} must be a number.`);
        const [lo, hi] = BOUNDS[k];
        if (n < lo || n > hi) throw new Error(`${k} must be between ${lo} and ${hi}.`);
        changes[k] = n;
      }
      const streakRaw = risk.streak_cooldown_losses.trim();
      if (streakRaw !== "") {
        const n = Number(streakRaw);
        if (!Number.isInteger(n)) throw new Error("Streak cooldown must be an integer.");
        const [lo, hi] = BOUNDS.streak_cooldown_losses;
        if (n < lo || n > hi) throw new Error(`Streak cooldown must be between ${lo} and ${hi}.`);
        changes.streak_cooldown_losses = n;
      }
      if (Object.keys(changes).length === 0) {
        setMsg({ kind: "err", text: "Nothing to save." });
        return;
      }
      await api.updateConfig(changes);
      setMsg({ kind: "ok", text: `Saved ${Object.keys(changes).length} field(s).` });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Sizing and limits</h2>
        <span className="panel-meta">% of capital</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Stake = capital × base stake × archetype multiplier, capped at
        max stake. Loss limits halt new trades when the threshold is
        crossed.
      </p>
      <form onSubmit={saveRisk}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, maxWidth: 720 }}>
          <PercentField
            label="Base stake" step="0.1"
            fractionRange={BOUNDS.base_stake_pct}
            fractionValue={risk.base_stake_pct}
            onChangeFraction={(v) => setRisk({ ...risk, base_stake_pct: v })}
          />
          <PercentField
            label="Max stake" step="0.1"
            fractionRange={BOUNDS.max_stake_pct}
            fractionValue={risk.max_stake_pct}
            onChangeFraction={(v) => setRisk({ ...risk, max_stake_pct: v })}
          />
          <PercentField
            label="Daily loss limit" step="1"
            fractionRange={BOUNDS.daily_loss_limit_pct}
            fractionValue={risk.daily_loss_limit_pct}
            onChangeFraction={(v) => setRisk({ ...risk, daily_loss_limit_pct: v })}
          />
          <PercentField
            label="Weekly loss limit" step="1"
            fractionRange={BOUNDS.weekly_loss_limit_pct}
            fractionValue={risk.weekly_loss_limit_pct}
            onChangeFraction={(v) => setRisk({ ...risk, weekly_loss_limit_pct: v })}
          />
          <PercentField
            label="Drawdown halt" step="1"
            fractionRange={BOUNDS.drawdown_halt_pct}
            fractionValue={risk.drawdown_halt_pct}
            onChangeFraction={(v) => setRisk({ ...risk, drawdown_halt_pct: v })}
          />
          <NumField
            label="Streak cooldown (losses)" step="1"
            range={BOUNDS.streak_cooldown_losses}
            value={risk.streak_cooldown_losses}
            onChange={(v) => setRisk({ ...risk, streak_cooldown_losses: v })}
          />
          <PercentField
            label="Dry powder reserve" step="1"
            fractionRange={BOUNDS.dry_powder_reserve_pct}
            fractionValue={risk.dry_powder_reserve_pct}
            onChangeFraction={(v) => setRisk({ ...risk, dry_powder_reserve_pct: v })}
          />
        </div>
        <div className="form-actions" style={{ marginTop: 18 }}>
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save risk and sizing"}
          </button>
          {msg && (
            <span className={msg.kind === "ok" ? "form-success" : "form-error"}>
              {msg.text}
            </span>
          )}
        </div>
      </form>
    </div>
  );
}

function NumField({
  label, step, range, value, onChange,
}: {
  label: string;
  step: string;
  range: readonly [number, number];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="form-field">
      <label>{label}</label>
      <input
        type="number"
        step={step}
        min={range[0]}
        max={range[1]}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className="form-hint">Range: {range[0]} - {range[1]}</span>
    </div>
  );
}

/**
 * Percent display wrapper around NumField.
 *
 * The DB stores fractions (0.10 = 10%) because the engine's risk
 * manager expects fractions. The UI shows percentages because that's
 * how humans read risk parameters.
 *
 * `fractionValue` is the form state string (still a fraction, e.g.
 * "0.10"). We multiply by 100 for display, format to one decimal
 * to keep the number from displaying as 9.999999999, and convert the
 * percent back to a fraction string before passing to onChangeFraction.
 *
 * `step` is in PERCENT units (e.g. step="1" steps by 1 percentage
 * point, step="0.1" steps by 0.1pp). `fractionRange` is in fraction
 * units (so [0.005, 0.05] is 0.5%-5%); we multiply by 100 for display.
 */
function PercentField({
  label, step, fractionRange, fractionValue, onChangeFraction,
}: {
  label: string;
  step: string;
  fractionRange: readonly [number, number];
  fractionValue: string;
  onChangeFraction: (fractionStr: string) => void;
}) {
  const percentValue = fractionValue === ""
    ? ""
    : (() => {
        const n = Number(fractionValue);
        if (!Number.isFinite(n)) return fractionValue;
        // Round to 4 decimal places to avoid 0.1*100 = 10.000000000000002.
        return String(Math.round(n * 10000) / 100);
      })();
  const minPct = fractionRange[0] * 100;
  const maxPct = fractionRange[1] * 100;
  return (
    <div className="form-field">
      <label>{label}</label>
      <div style={{ position: "relative" }}>
        <input
          type="number"
          step={step}
          min={minPct}
          max={maxPct}
          value={percentValue}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              onChangeFraction("");
              return;
            }
            const n = Number(raw);
            if (!Number.isFinite(n)) return;
            // Round to 6 decimal places to avoid float drift on save.
            const fraction = Math.round(n / 100 * 1_000_000) / 1_000_000;
            onChangeFraction(String(fraction));
          }}
          style={{ paddingRight: 28 }}
        />
        <span style={{
          position: "absolute", right: 10, top: "50%",
          transform: "translateY(-50%)", color: "var(--text-muted, #888)",
          pointerEvents: "none", fontSize: "0.9em",
        }}>%</span>
      </div>
      <span className="form-hint">
        Range: {minPct % 1 === 0 ? minPct : minPct.toFixed(1)}% -
        {' '}{maxPct % 1 === 0 ? maxPct : maxPct.toFixed(1)}%
      </span>
    </div>
  );
}

// ── Archetype grid ───────────────────────────────────────────────────────

function ArchetypePanel({ onSaved }: { onSaved: () => void }) {
  const [data, setData] = useState<ArchetypeCatalogue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await api.archetypes();
      setData(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => { load(); }, []);

  const groups = useMemo(() => {
    if (!data) return [] as Array<{ title: string; items: ArchetypeEntry[] }>;
    // Group buckets aligned with the categories in
    // `engine/archetype_classifier.py`. Order matters - this is also
    // the render order, top-to-bottom.
    const groupOrder: Array<{ title: string; ids: string[] }> = [
      { title: "Sports", ids: [
        "tennis", "basketball", "baseball", "football", "hockey",
        "cricket", "esports", "soccer", "sports_other",
      ]},
      { title: "Finance and markets", ids: [
        "crypto", "stocks", "macro", "fx_commodities",
      ]},
      { title: "Politics and society", ids: [
        "election", "policy_event", "geopolitical_event",
      ]},
      { title: "Tech and culture", ids: [
        "tech_release", "awards", "entertainment",
      ]},
      { title: "Other markets", ids: [
        "weather_event", "price_threshold", "activity_count", "binary_event",
      ]},
    ];
    const byId = new Map(data.archetypes.map((a) => [a.id, a]));
    const seen = new Set<string>();
    const out: Array<{ title: string; items: ArchetypeEntry[] }> = [];
    for (const g of groupOrder) {
      const items = g.ids
        .map((id) => byId.get(id))
        .filter((a): a is ArchetypeEntry => a != null);
      items.forEach((a) => seen.add(a.id));
      if (items.length) out.push({ title: g.title, items });
    }
    // Belt-and-suspenders: any future archetype not in groupOrder
    // still renders, just lumped at the end so it's never invisible.
    const stragglers = data.archetypes.filter((a) => !seen.has(a.id));
    if (stragglers.length) out.push({ title: "Uncategorized", items: stragglers });
    return out;
  }, [data]);

  const update = async (
    a: ArchetypeEntry,
    patch: Partial<Pick<ArchetypeEntry, "skip" | "multiplier" | "bands">>,
  ) => {
    if (!data || busyId) return;
    setBusyId(a.id);
    setError(null);

    const nextSkip = new Set(
      data.archetypes
        .filter((x) => (x.id === a.id ? (patch.skip ?? x.skip) : x.skip))
        .map((x) => x.id),
    );
    const nextMults: Record<string, number> = {};
    for (const x of data.archetypes) {
      const m = x.id === a.id ? (patch.multiplier ?? x.multiplier) : x.multiplier;
      if (Math.abs(m - x.default_mult) > 1e-6) {
        nextMults[x.id] = m;
      }
    }
    // Build the per-archetype band map for ALL archetypes (so we
    // round-trip the user's existing per-card overrides, not just the
    // one being edited). Empty band lists are dropped so the stored
    // representation is canonical.
    const nextBands: Record<string, number[][]> = {};
    for (const x of data.archetypes) {
      const bands = x.id === a.id ? (patch.bands ?? x.bands ?? []) : (x.bands ?? []);
      if (bands.length > 0) {
        nextBands[x.id] = bands;
      }
    }

    try {
      await api.updateConfig({
        archetype_skip_list: Array.from(nextSkip),
        archetype_stake_multipliers: nextMults,
        archetype_skip_market_price_bands: nextBands,
      });
      await load();
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  const reset = (a: ArchetypeEntry) => {
    void update(a, {
      skip: a.default_skip,
      multiplier: a.default_mult,
      // Reset also clears any per-archetype band overrides.
      bands: [],
    });
  };

  if (!data) {
    return (
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Archetypes</h2>
        </div>
        <div className="empty-state">Loading archetypes...</div>
        {error && !isConnectionError(error) && (
        <div className="error">{error}</div>
      )}
      </div>
    );
  }

  const { multiplier_min, multiplier_max } = data.bounds;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Archetypes</h2>
        <span className="panel-meta">{data.archetypes.length} categories</span>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Each market Delfi looks at is classified into one archetype. Skip an
        archetype to ignore those markets entirely. Use the multiplier to
        size up or down without skipping. Default for unknown archetypes is 1.0×.
      </p>

      {error && !isConnectionError(error) && (
        <div className="error">{error}</div>
      )}

      {groups.map((g) => (
        <div key={g.title} style={{ marginTop: 16 }}>
          <h3 className="t-caption" style={{ margin: "0 0 8px" }}>{g.title}</h3>
          <div className="archetype-grid">
            {g.items.map((a) => (
              <ArchetypeCard
                key={a.id}
                a={a}
                busy={busyId === a.id}
                multMin={multiplier_min}
                multMax={multiplier_max}
                onToggleSkip={() => update(a, { skip: !a.skip })}
                onMultChange={(m) => update(a, { multiplier: m })}
                onBandsChange={(bands) => update(a, { bands })}
                onReset={() => reset(a)}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ArchetypeCard({
  a, busy, multMin, multMax, onToggleSkip, onMultChange, onBandsChange, onReset,
}: {
  a: ArchetypeEntry;
  busy: boolean;
  multMin: number;
  multMax: number;
  onToggleSkip: () => void;
  onMultChange: (m: number) => void;
  onBandsChange: (bands: number[][]) => void;
  onReset: () => void;
}) {
  const [pending, setPending] = useState<number | null>(null);
  const [bandsOpen, setBandsOpen] = useState(false);
  const shown = pending ?? a.multiplier;
  // Set of disabled bucket keys derived from a.bands. Each band is a
  // [lo, hi] pair already snapped to a 10pp boundary by the API or
  // by a previous toggle.
  const disabledBands = useMemo(() => {
    const set = new Set<string>();
    for (const pair of a.bands ?? []) {
      if (!Array.isArray(pair) || pair.length !== 2) continue;
      const loPct = Math.round(Number(pair[0]) * 100);
      const hiPct = Math.round(Number(pair[1]) * 100);
      if (hiPct - loPct !== 10) continue;
      if (loPct % 10 !== 0) continue;
      if (loPct < 0 || hiPct > 100) continue;
      set.add(`${loPct}_${hiPct}`);
    }
    return set;
  }, [a.bands]);
  const isDefault =
    a.skip === a.default_skip
    && Math.abs(a.multiplier - a.default_mult) < 1e-6
    && disabledBands.size === 0;

  const toggleBand = (lo: number, hi: number) => {
    if (busy) return;
    const k = bandKey(lo, hi);
    const next = new Set(disabledBands);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    // Re-emit as a sorted list of [lo, hi] pairs in canonical 10pp form.
    const bands = PRICE_BANDS
      .filter(([l, h]) => next.has(bandKey(l, h)))
      .map(([l, h]) => [
        Math.round(l * 100) / 100,
        Math.round(h * 100) / 100,
      ]);
    onBandsChange(bands);
  };

  return (
    <div className={`archetype-card ${a.skip ? "skipped" : ""}`}>
      <div>
        <div className="archetype-name">{a.label}</div>
        <div className="archetype-desc">{a.description}</div>
      </div>

      <div className="archetype-controls">
        <span style={{
          fontSize: 11, color: "var(--vellum-60)",
          letterSpacing: "0.1em", textTransform: "uppercase",
        }}>
          {a.skip ? "Skip" : "Trade"}
        </span>
        <label className="toggle-switch">
          <input
            type="checkbox"
            checked={!a.skip}
            disabled={busy}
            onChange={onToggleSkip}
          />
          <span className="toggle-slider" />
        </label>
      </div>

      <div className="archetype-mult">
        <span className="archetype-mult-label">Stake mult</span>
        <input
          type="range"
          min={multMin}
          max={multMax}
          step="0.05"
          value={shown}
          disabled={busy || a.skip}
          onChange={(e) => setPending(Number(e.target.value))}
          onMouseUp={(e) => {
            const v = Number((e.target as HTMLInputElement).value);
            setPending(null);
            if (Math.abs(v - a.multiplier) > 1e-6) onMultChange(v);
          }}
          onTouchEnd={(e) => {
            const v = Number((e.target as HTMLInputElement).value);
            setPending(null);
            if (Math.abs(v - a.multiplier) > 1e-6) onMultChange(v);
          }}
        />
        <span className="archetype-mult-value">{shown.toFixed(2)}×</span>
        {!isDefault && (
          <button
            type="button"
            className="archetype-mult-default"
            onClick={onReset}
            disabled={busy}
            title={`Default: ${a.default_skip ? "skip" : "trade"} at ${a.default_mult}×`}
          >
            Reset
          </button>
        )}
      </div>

      <div className="archetype-bands-row">
        <button
          type="button"
          className="archetype-bands-toggle"
          onClick={() => setBandsOpen(o => !o)}
          aria-expanded={bandsOpen}
        >
          {bandsOpen ? "Hide" : "Bands"}
          {disabledBands.size > 0 && (
            <span className="archetype-bands-count">
              {disabledBands.size} skipped
            </span>
          )}
        </button>
      </div>

      {bandsOpen && (
        <div className="archetype-bands-panel">
          <p className="archetype-bands-help">
            Toggle off any 10pp band Delfi should skip on {a.label}{" "}
            markets. 0-50 means market favours NO; 50-100 means YES. A
            band disabled here only applies to {a.label}; other
            archetypes are unaffected.
          </p>
          <div className="price-band-row">
            {PRICE_BANDS.slice(0, 5).map(([lo, hi]) => {
              const k = bandKey(lo, hi);
              const off = disabledBands.has(k);
              return (
                <button
                  type="button"
                  key={k}
                  onClick={() => toggleBand(lo, hi)}
                  disabled={busy}
                  className={off ? "price-band off" : "price-band on"}
                  aria-pressed={!off}
                >
                  {bandLabel(lo, hi)}
                </button>
              );
            })}
            <span className="price-band-divider" aria-hidden="true" />
            {PRICE_BANDS.slice(5).map(([lo, hi]) => {
              const k = bandKey(lo, hi);
              const off = disabledBands.has(k);
              return (
                <button
                  type="button"
                  key={k}
                  onClick={() => toggleBand(lo, hi)}
                  disabled={busy}
                  className={off ? "price-band off" : "price-band on"}
                  aria-pressed={!off}
                >
                  {bandLabel(lo, hi)}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
