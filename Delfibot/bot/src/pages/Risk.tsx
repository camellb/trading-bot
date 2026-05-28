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
  // Upper bounds widened 2026-05-18 so users at small live bankrolls
  // can configure a stake-pct large enough to clear Polymarket's
  // platform floors. Bot also gained a max_stake_pct_enabled toggle
  // (default OFF) so the cap is opt-in rather than always-on.
  base_stake_pct:        [0.005, 1.00] as const,
  max_stake_pct:         [0.01,  1.00] as const,
  daily_loss_limit_pct:  [0.01,  1.00] as const,
  weekly_loss_limit_pct: [0.01,  1.00] as const,
  drawdown_halt_pct:     [0.01,  1.00] as const,
  streak_cooldown_losses:[2,     10]   as const,
  dry_powder_reserve_pct:[0.10,  0.40] as const,
  // Exit policy — mirrors USER_CONFIG_BOUNDS in engine/user_config.py.
  take_profit_threshold_pct:           [0.05, 5.00] as const,   // 5% - 500%
  stop_loss_threshold_pct:             [0.05, 0.95] as const,   // 5% - 95% loss
  stop_loss_min_time_remaining_pct:    [0.00, 0.95] as const,   // 0% - 95%
  time_decay_max_hours:                [1,    720]  as const,   // 1h - 30d
  time_decay_flat_band_pct:            [0.00, 1.00] as const,   // 0% - 100%
  exit_min_time_to_resolution_minutes: [0,    1440] as const,   // 0 - 24h
};

type ConfigShape = {
  base_stake_pct?: number;
  max_stake_pct?: number;
  max_stake_pct_enabled?: boolean;
  daily_loss_limit_pct?: number;
  weekly_loss_limit_pct?: number;
  drawdown_halt_pct?: number;
  streak_cooldown_losses?: number;
  dry_powder_reserve_pct?: number;
  starting_cash?: number | null;
  archetype_skip_list?: string[];
  archetype_stake_multipliers?: Record<string, number>;
  // Exit policy
  exit_policy_enabled?: boolean;
  take_profit_enabled?: boolean;
  take_profit_threshold_pct?: number;
  stop_loss_enabled?: boolean;
  stop_loss_threshold_pct?: number;
  stop_loss_min_time_remaining_pct?: number;
  time_decay_enabled?: boolean;
  time_decay_max_hours?: number;
  time_decay_flat_band_pct?: number;
  exit_min_time_to_resolution_minutes?: number;
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
      <ExitPolicyPanel config={config} onSaved={onSaved} />
      <ResolutionWindowPanel config={config} onSaved={onSaved} />
      <VolumeTierPanel config={config} onSaved={onSaved} />
      <ArchetypePanel onSaved={onSaved} />
    </div>
  );
}

// ── Volume-tier stake multipliers ────────────────────────────────────────
//
// Three buckets keyed by the market's 24h CLOB volume in USD:
//   low  < $1,000          default 0.8x
//   mid  $1,000 - $10,000  default 1.0x
//   high >= $10,000        default 1.1x
//
// Multiplied into the stake alongside the archetype multiplier:
//   stake = bankroll * base_stake_pct * archetype_mult * volume_mult
//
// Polymarket's accuracy page (2026-05-28 research) shows higher-volume
// markets are more accurate (Brier-vs-Volume curve trends down). The
// defaults are a mild tilt; users can amplify or flatten the gradient
// here.

const VOLUME_TIER_DEFAULTS: Record<string, number> = {
  low:  0.8,
  mid:  1.0,
  high: 1.1,
};
const VOLUME_TIER_BOUNDS = { min: 0.1, max: 10.0 };

function VolumeTierPanel({
  config, onSaved,
}: {
  config: ConfigShape | null;
  onSaved: () => void;
}) {
  // The ConfigShape interface has an `[k: string]: unknown` index
  // signature for forward-compat with sidecars that emit new fields
  // we haven't typed yet. TS prefers the index signature over the
  // declared `volume_tier_multipliers` shape, so we cast here.
  const current = (config?.volume_tier_multipliers ?? {}) as Record<string, number>;
  const value = (k: string): number =>
    typeof current[k] === "number" ? current[k] : VOLUME_TIER_DEFAULTS[k];

  // Local pending edits: only persisted on Save. Lets the user type
  // 0.85 without each keystroke racing a server round-trip.
  const [draft, setDraft] = useState<Record<string, string>>({
    low:  String(value("low")),
    mid:  String(value("mid")),
    high: String(value("high")),
  });
  const [busy,  setBusy]  = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg,   setMsg]   = useState<string | null>(null);

  // Re-sync draft whenever the config prop changes (e.g. after a
  // successful save or an external update). Otherwise the inputs
  // would freeze at the values entered before the last save.
  useEffect(() => {
    setDraft({
      low:  String(value("low")),
      mid:  String(value("mid")),
      high: String(value("high")),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    (config?.volume_tier_multipliers as Record<string, number> | undefined)?.low,
    (config?.volume_tier_multipliers as Record<string, number> | undefined)?.mid,
    (config?.volume_tier_multipliers as Record<string, number> | undefined)?.high,
  ]);

  const save = async () => {
    if (busy) return;
    setError(null);
    setMsg(null);
    const parsed: Record<string, number> = {};
    for (const k of ["low", "mid", "high"] as const) {
      const raw = draft[k];
      const n = Number(raw);
      if (!Number.isFinite(n)) {
        setError(`${k} must be a number`);
        return;
      }
      if (n < VOLUME_TIER_BOUNDS.min || n > VOLUME_TIER_BOUNDS.max) {
        setError(
          `${k} must be between ${VOLUME_TIER_BOUNDS.min} and ${VOLUME_TIER_BOUNDS.max}`,
        );
        return;
      }
      parsed[k] = n;
    }
    setBusy(true);
    try {
      await api.updateConfig({
        volume_tier_multipliers: parsed,
      });
      setMsg("Saved.");
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setDraft({
      low:  String(VOLUME_TIER_DEFAULTS.low),
      mid:  String(VOLUME_TIER_DEFAULTS.mid),
      high: String(VOLUME_TIER_DEFAULTS.high),
    });
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Volume tier</h2>
      </div>
      <p className="page-sub" style={{ marginBottom: 16 }}>
        Per-market-volume stake multiplier. Higher-volume markets are
        historically more accurate, so the default tilts slightly
        toward them.
      </p>
      <div className="form-row">
        {(["low", "mid", "high"] as const).map((k) => {
          const labels: Record<string, string> = {
            low:  "Low (< $1k)",
            mid:  "Mid ($1k - $10k)",
            high: "High (>= $10k)",
          };
          return (
            <div className="form-field" key={k}>
              <label>{labels[k]}</label>
              <input
                type="number"
                step="0.05"
                min={VOLUME_TIER_BOUNDS.min}
                max={VOLUME_TIER_BOUNDS.max}
                value={draft[k]}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, [k]: e.target.value }))
                }
              />
              <p className="form-hint">
                Default {VOLUME_TIER_DEFAULTS[k]}x.
              </p>
            </div>
          );
        })}
      </div>
      <div className="form-actions" style={{ marginTop: 12 }}>
        <button type="button" className="btn small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
        <button type="button" className="btn ghost small" onClick={reset} disabled={busy}>
          Reset to defaults
        </button>
        {msg && <span className="form-success">{msg}</span>}
        {error && <span className="form-error">{error}</span>}
      </div>
    </div>
  );
}

// ── Exit policy: take-profit, stop-loss, time-decay ──────────────────────
//
// Per-position exit rules that fire BEFORE natural Polymarket
// settlement. Master switch defaults OFF; each sub-rule has its own
// toggle so the user can enable e.g. take-profit while leaving the
// others alone. Thresholds defaulted to sensible starting points:
// +50% TP, -30% SL, 72h time-decay flat band.
//
// All three rules share a universal safety floor
// (`exit_min_time_to_resolution_minutes`) that holds any open
// position when the market is within N minutes of natural settlement
// — the spread + Polymarket fees eat the marginal value of selling
// that close to resolution.

function ExitPolicyPanel({
  config,
  onSaved,
}: {
  config: ConfigShape | null;
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    exit_policy_enabled: false,
    take_profit_enabled: true,
    take_profit_threshold_pct: "",
    stop_loss_enabled: true,
    stop_loss_threshold_pct: "",
    stop_loss_min_time_remaining_pct: "",
    time_decay_enabled: false,
    time_decay_max_hours: "",
    time_decay_flat_band_pct: "",
    exit_min_time_to_resolution_minutes: "",
  });
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // Same single-shot sync pattern as RiskPanel — avoid clobbering
  // typed-but-unsaved values on every 5s App.tsx poll.
  const syncedRef = useRef(false);
  useEffect(() => {
    if (!config) return;
    if (syncedRef.current) return;
    syncedRef.current = true;
    setForm({
      exit_policy_enabled: !!config.exit_policy_enabled,
      take_profit_enabled: config.take_profit_enabled ?? true,
      take_profit_threshold_pct: config.take_profit_threshold_pct != null
        ? String(config.take_profit_threshold_pct) : "",
      stop_loss_enabled: config.stop_loss_enabled ?? true,
      stop_loss_threshold_pct: config.stop_loss_threshold_pct != null
        ? String(config.stop_loss_threshold_pct) : "",
      stop_loss_min_time_remaining_pct: config.stop_loss_min_time_remaining_pct != null
        ? String(config.stop_loss_min_time_remaining_pct) : "",
      time_decay_enabled: !!config.time_decay_enabled,
      time_decay_max_hours: config.time_decay_max_hours != null
        ? String(config.time_decay_max_hours) : "",
      time_decay_flat_band_pct: config.time_decay_flat_band_pct != null
        ? String(config.time_decay_flat_band_pct) : "",
      exit_min_time_to_resolution_minutes: config.exit_min_time_to_resolution_minutes != null
        ? String(config.exit_min_time_to_resolution_minutes) : "",
    });
  }, [config]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    // User-facing labels for validation messages so errors read
    // "Take-profit target must be between 5 and 500." instead of
    // "take_profit_threshold_pct must be between 0.05 and 5."
    const fieldLabels: Record<string, string> = {
      take_profit_threshold_pct:         "Take-profit target",
      stop_loss_threshold_pct:           "Stop-loss threshold",
      stop_loss_min_time_remaining_pct:  "Stop-loss grace period",
      time_decay_flat_band_pct:          "Flat-move threshold",
      time_decay_max_hours:              "Maximum hold time",
      exit_min_time_to_resolution_minutes: "Minimum time before settlement",
    };
    try {
      const changes: Record<string, unknown> = {
        exit_policy_enabled: form.exit_policy_enabled,
        take_profit_enabled: form.take_profit_enabled,
        stop_loss_enabled:   form.stop_loss_enabled,
        time_decay_enabled:  form.time_decay_enabled,
      };
      const numericFloat = [
        "take_profit_threshold_pct",
        "stop_loss_threshold_pct",
        "stop_loss_min_time_remaining_pct",
        "time_decay_flat_band_pct",
      ] as const;
      for (const k of numericFloat) {
        const raw = (form[k] as string).trim();
        if (raw === "") continue;
        const n = Number(raw);
        const label = fieldLabels[k] ?? k;
        if (!Number.isFinite(n)) throw new Error(`${label} must be a number.`);
        const [lo, hi] = BOUNDS[k];
        if (n < lo || n > hi) throw new Error(`${label} must be between ${lo} and ${hi}.`);
        changes[k] = n;
      }
      const numericInt = [
        "time_decay_max_hours",
        "exit_min_time_to_resolution_minutes",
      ] as const;
      for (const k of numericInt) {
        const raw = (form[k] as string).trim();
        if (raw === "") continue;
        const n = Number(raw);
        const label = fieldLabels[k] ?? k;
        if (!Number.isInteger(n)) throw new Error(`${label} must be a whole number.`);
        const [lo, hi] = BOUNDS[k];
        if (n < lo || n > hi) throw new Error(`${label} must be between ${lo} and ${hi}.`);
        changes[k] = n;
      }
      await api.updateConfig(changes);
      setMsg({ kind: "ok", text: "Exit policy saved." });
      onSaved();
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const disabled = !form.exit_policy_enabled;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Auto-close positions</h2>
      </div>
      <form onSubmit={save}>
        <div style={{ marginBottom: 18 }}>
          <ToggleRow
            label="Early exits"
            description="Master switch. When off, every position runs to natural settlement."
            checked={form.exit_policy_enabled}
            onChange={(v) => setForm({ ...form, exit_policy_enabled: v })}
          />
        </div>

        <div className="form-divider" style={{ opacity: disabled ? 0.45 : 1, transition: "opacity 0.2s" }}>
          <div style={{ marginBottom: 12 }}>
            <ToggleRow
              label="Take-profit"
              description="Sell positions that have gained enough vs. entry."
              checked={form.take_profit_enabled}
              onChange={(v) => setForm({ ...form, take_profit_enabled: v })}
              disabled={disabled}
            />
          </div>
          <div className="risk-grid risk-grid-2">
            <PercentField
              label="Take-profit target" step="1"
              fractionRange={BOUNDS.take_profit_threshold_pct}
              fractionValue={form.take_profit_threshold_pct}
              onChangeFraction={(v) => setForm({ ...form, take_profit_threshold_pct: v })}
              note="Close once unrealized gain reaches this % of cost."
            />
          </div>
        </div>

        <div className="form-divider" style={{ marginTop: 18, opacity: disabled ? 0.45 : 1, transition: "opacity 0.2s" }}>
          <div style={{ marginBottom: 12 }}>
            <ToggleRow
              label="Stop-loss"
              description="Cut positions that have lost enough vs. entry."
              checked={form.stop_loss_enabled}
              onChange={(v) => setForm({ ...form, stop_loss_enabled: v })}
              disabled={disabled}
            />
          </div>
          <div className="risk-grid risk-grid-2">
            <PercentField
              label="Stop-loss threshold" step="1"
              fractionRange={BOUNDS.stop_loss_threshold_pct}
              fractionValue={form.stop_loss_threshold_pct}
              onChangeFraction={(v) => setForm({ ...form, stop_loss_threshold_pct: v })}
              note="Close once unrealized loss reaches this % of cost."
            />
            <PercentField
              label="Stop-loss grace period" step="1"
              fractionRange={BOUNDS.stop_loss_min_time_remaining_pct}
              fractionValue={form.stop_loss_min_time_remaining_pct}
              onChangeFraction={(v) => setForm({ ...form, stop_loss_min_time_remaining_pct: v })}
              note="Hold off on stop-loss until this % of time-to-settlement has passed."
            />
          </div>
        </div>

        <div className="form-divider" style={{ marginTop: 18, opacity: disabled ? 0.45 : 1, transition: "opacity 0.2s" }}>
          <div style={{ marginBottom: 12 }}>
            <ToggleRow
              label="Time-based exit"
              description="Drop positions that haven't moved much after a long hold."
              checked={form.time_decay_enabled}
              onChange={(v) => setForm({ ...form, time_decay_enabled: v })}
              disabled={disabled}
            />
          </div>
          <div className="risk-grid risk-grid-2">
            <NumField
              label="Maximum hold time" step="1"
              range={BOUNDS.time_decay_max_hours}
              value={form.time_decay_max_hours}
              onChange={(v) => setForm({ ...form, time_decay_max_hours: v })}
              note="Time-based exit fires after this many hours."
            />
            <PercentField
              label="Flat-move threshold" step="1"
              fractionRange={BOUNDS.time_decay_flat_band_pct}
              fractionValue={form.time_decay_flat_band_pct}
              onChangeFraction={(v) => setForm({ ...form, time_decay_flat_band_pct: v })}
              note="Only exit if |P&L| is still within this % of cost (i.e. flat)."
            />
          </div>
        </div>

        <div className="form-divider" style={{ marginTop: 18, opacity: disabled ? 0.45 : 1, transition: "opacity 0.2s" }}>
          <h3 className="panel-subtitle" style={{ marginBottom: 8 }}>Settlement guard</h3>
          <div className="risk-grid risk-grid-2">
            <NumField
              label="Minimum time before settlement" step="1"
              range={BOUNDS.exit_min_time_to_resolution_minutes}
              value={form.exit_min_time_to_resolution_minutes}
              onChange={(v) => setForm({ ...form, exit_min_time_to_resolution_minutes: v })}
              note="Inside this window, Delfi holds every position to natural settlement. Selling this close costs more in spread + fees than it could gain."
            />
          </div>
        </div>

        <div className="form-actions" style={{ marginTop: 20 }}>
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save exit policy"}
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

function ToggleRow({
  label, description, checked, onChange, disabled,
}: {
  label: string;
  description?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: "12px 14px",
        border: "1px solid var(--border, #2a2a2a)",
        borderRadius: 8,
        background: "var(--surface-2, rgba(255,255,255,0.02))",
      }}
    >
      <label style={{
        display: "inline-flex",
        alignItems: "center",
        cursor: disabled ? "not-allowed" : "pointer",
        gap: 10,
      }}>
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span style={{ fontWeight: 600 }}>{label}</span>
      </label>
      {description ? (
        <span
          className="form-hint"
          style={{
            color: "var(--text-muted, #888)",
            marginLeft: 26, // align with label text after checkbox
          }}
        >
          {description}
        </span>
      ) : null}
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
        <h2 className="panel-title">Market timeframe</h2>
      </div>
      <form onSubmit={save}>
        <div className="risk-grid risk-grid-2">
          <NumField
            label="Earliest settlement (days)"
            step="1"
            range={RESOLUTION_BOUNDS}
            value={minDays}
            onChange={setMinDays}
          />
          <NumField
            label="Latest settlement (days)"
            step="1"
            range={RESOLUTION_BOUNDS}
            value={maxDays}
            onChange={setMaxDays}
          />
        </div>
        <div className="form-actions" style={{ marginTop: 18 }}>
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save changes"}
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
    max_stake_pct_enabled: false,
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
      max_stake_pct_enabled:  !!config.max_stake_pct_enabled,
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
    // User-facing labels for validation messages on circuit-breaker
    // fields. Matches the input labels rendered below.
    const fieldLabels: Record<string, string> = {
      base_stake_pct:         "Default bet size",
      max_stake_pct:          "Maximum bet size",
      daily_loss_limit_pct:   "Daily loss limit",
      weekly_loss_limit_pct:  "Weekly loss limit",
      drawdown_halt_pct:      "Maximum drawdown",
      dry_powder_reserve_pct: "Reserve cash",
    };
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
        const label = fieldLabels[k] ?? k;
        if (!Number.isFinite(n)) throw new Error(`${label} must be a number.`);
        const [lo, hi] = BOUNDS[k];
        if (n < lo || n > hi) throw new Error(`${label} must be between ${lo} and ${hi}.`);
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
      // Always send the cap-toggle so the user can flip it without
      // touching any numeric field.
      changes.max_stake_pct_enabled = risk.max_stake_pct_enabled;
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
        <h2 className="panel-title">Bet sizing and risk limits</h2>
      </div>
      <form onSubmit={saveRisk}>
        <div className="risk-grid risk-grid-3">
          <PercentField
            label="Default bet size" step="0.1"
            fractionRange={BOUNDS.base_stake_pct}
            fractionValue={risk.base_stake_pct}
            onChangeFraction={(v) => setRisk({ ...risk, base_stake_pct: v })}
          />
          <PercentField
            label="Maximum bet size" step="0.1"
            fractionRange={BOUNDS.max_stake_pct}
            fractionValue={risk.max_stake_pct}
            onChangeFraction={(v) => setRisk({ ...risk, max_stake_pct: v })}
          />
          <PercentField
            label="Reserve cash" step="1"
            fractionRange={BOUNDS.dry_powder_reserve_pct}
            fractionValue={risk.dry_powder_reserve_pct}
            onChangeFraction={(v) => setRisk({ ...risk, dry_powder_reserve_pct: v })}
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
            label="Maximum drawdown" step="1"
            fractionRange={BOUNDS.drawdown_halt_pct}
            fractionValue={risk.drawdown_halt_pct}
            onChangeFraction={(v) => setRisk({ ...risk, drawdown_halt_pct: v })}
          />
          <NumField
            label="Consecutive loss cooldown" step="1"
            range={BOUNDS.streak_cooldown_losses}
            value={risk.streak_cooldown_losses}
            onChange={(v) => setRisk({ ...risk, streak_cooldown_losses: v })}
          />
          <div className="risk-grid-full">
            <ToggleRow
              label="Strict maximum bet size"
              checked={risk.max_stake_pct_enabled}
              onChange={(v) => setRisk({ ...risk, max_stake_pct_enabled: v })}
            />
          </div>
        </div>
        <div className="form-actions" style={{ marginTop: 18 }}>
          <button type="submit" className="btn small" disabled={busy}>
            {busy ? "Saving..." : "Save changes"}
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
  label, step, range, value, onChange, note,
}: {
  label: string;
  step: string;
  range: readonly [number, number];
  value: string;
  onChange: (v: string) => void;
  note?: string;
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
      {note ? <span className="form-hint">{note}</span> : null}
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
  label, step, fractionRange, fractionValue, onChangeFraction, note,
}: {
  label: string;
  step: string;
  fractionRange: readonly [number, number];
  fractionValue: string;
  onChangeFraction: (fractionStr: string) => void;
  note?: string;
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
      <div style={{ position: "relative", maxWidth: 240 }}>
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
          // paddingRight leaves room for the absolute-positioned "%"
          // suffix inside the input. The native spinner buttons are
          // hidden globally in styles.css so 28px is enough clearance.
          style={{ paddingRight: 28 }}
        />
        <span style={{
          position: "absolute", right: 10, top: "50%",
          transform: "translateY(-50%)", color: "var(--text-muted, #888)",
          pointerEvents: "none", fontSize: "0.9em",
        }}>%</span>
      </div>
      {note ? (
        <span className="form-hint">{note}</span>
      ) : null}
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
        "crypto", "crypto_short", "stocks", "macro", "fx_commodities",
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
          onKeyUp={(e) => {
            // Keyboard users dragging via Arrow / Home / End /
            // PageUp / PageDown never trigger mouseUp or touchEnd;
            // without this commit handler the new value sat in
            // `pending` and never persisted.
            const KEYS = [
              "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
              "Home", "End", "PageUp", "PageDown",
            ];
            if (!KEYS.includes(e.key)) return;
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
            Toggle off any 10-point price range Delfi should avoid on{" "}
            {a.label} markets. 0-50 means the market favours NO;
            50-100 means YES. Ranges switched off here only affect{" "}
            {a.label} markets.
          </p>
          <div className="price-band-row">
            {PRICE_BANDS.map(([lo, hi]) => {
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
