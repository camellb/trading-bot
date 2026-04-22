"use client";

import { useState } from "react";
import "../../styles/content.css";

type Risk = {
  dailyCap: number;
  weeklyCap: number;
  drawdownHalt: number;
  streakCooldown: number;
  dryPowder: number;
  maxStake: number;
  baselineStake: number;
  minPwin: number;
  skipList: string[];
};

// Mirror of engine/archetype_classifier.py :: ARCHETYPES. If the Python
// list changes, update here (or wire contracts once the monorepo lands).
const ALL_ARCHETYPES: { id: string; label: string; group: string }[] = [
  { id: "tennis_qualifier",    label: "Tennis — qualifier",     group: "Sports" },
  { id: "tennis_main_draw",    label: "Tennis — main draw",     group: "Sports" },
  { id: "tennis_lower_tier",   label: "Tennis — lower tier",    group: "Sports" },
  { id: "basketball_game",     label: "Basketball — game",      group: "Sports" },
  { id: "basketball_prop",     label: "Basketball — prop",      group: "Sports" },
  { id: "baseball_game",       label: "Baseball — game",        group: "Sports" },
  { id: "football_game",       label: "Football — game",        group: "Sports" },
  { id: "hockey_game",         label: "Hockey — game",          group: "Sports" },
  { id: "cricket_match",       label: "Cricket — match",        group: "Sports" },
  { id: "esports_match",       label: "Esports — match",        group: "Sports" },
  { id: "soccer_match",        label: "Soccer — match",         group: "Sports" },
  { id: "sports_other",        label: "Sports — other",         group: "Sports" },
  { id: "price_threshold",     label: "Price threshold",        group: "Markets" },
  { id: "activity_count",      label: "Activity count",         group: "Markets" },
  { id: "geopolitical_event",  label: "Geopolitical event",     group: "Markets" },
  { id: "binary_event",        label: "Binary event",           group: "Markets" },
];

const DEFAULTS: Risk = {
  dailyCap: 10,
  weeklyCap: 20,
  drawdownHalt: 40,
  streakCooldown: 3,
  dryPowder: 20,
  maxStake: 5,
  baselineStake: 2,
  minPwin: 65,
  // Defaults mirror engine/user_config.py UserConfig.archetype_skip_list.
  skipList: ["tennis_qualifier", "tennis_lower_tier"],
};

export default function RiskPage() {
  const [r, setR] = useState<Risk>(DEFAULTS);

  const upd = <K extends keyof Risk>(k: K, v: Risk[K]) => setR((prev) => ({ ...prev, [k]: v }));
  const reset = () => setR(DEFAULTS);
  const save = () => {};

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
            <button className="btn-sm" onClick={reset}>Reset to defaults</button>
            <button className="btn-sm gold" onClick={save}>Save changes</button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Loss caps</h2>
          <span className="panel-meta">Stop trading when losses hit a ceiling</span>
        </div>

        <Slider
          label="Daily loss cap"
          desc="Delfi stops opening new positions after losing this much of bankroll in a single day."
          min={5} max={25} step={1} unit="%"
          value={r.dailyCap}
          onChange={(v) => upd("dailyCap", v)}
        />
        <Slider
          label="Weekly loss cap"
          desc="Halt trading for the rest of the week if cumulative losses exceed this share of bankroll."
          min={10} max={40} step={1} unit="%"
          value={r.weeklyCap}
          onChange={(v) => upd("weeklyCap", v)}
        />
        <Slider
          label="Drawdown halt"
          desc="Total drawdown from peak that triggers a manual-review halt."
          min={20} max={60} step={1} unit="%"
          value={r.drawdownHalt}
          onChange={(v) => upd("drawdownHalt", v)}
        />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Position sizing</h2>
          <span className="panel-meta">How big each trade can be</span>
        </div>

        <Slider
          label="Baseline stake"
          desc="Default position size when confidence is around 0.5."
          min={0.5} max={5} step={0.1} unit="%"
          value={r.baselineStake}
          onChange={(v) => upd("baselineStake", v)}
        />
        <Slider
          label="Maximum stake per trade"
          desc="Hard ceiling on any single position, regardless of confidence."
          min={1} max={10} step={0.5} unit="%"
          value={r.maxStake}
          onChange={(v) => upd("maxStake", v)}
        />
        <Slider
          label="Dry powder reserve"
          desc="Share of bankroll Delfi will never deploy. Held in reserve for exceptional opportunities."
          min={10} max={40} step={1} unit="%"
          value={r.dryPowder}
          onChange={(v) => upd("dryPowder", v)}
        />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Trade selection</h2>
          <span className="panel-meta">What Delfi considers worth betting</span>
        </div>

        <Slider
          label="Minimum p_win"
          desc="Delfi's probability for the chosen side must clear this floor. Below it, the trade is skipped."
          min={50} max={90} step={1} unit="%"
          value={r.minPwin}
          onChange={(v) => upd("minPwin", v)}
        />
        <Slider
          label="Streak cooldown"
          desc="Halve stake for 5 trades after this many consecutive losses."
          min={2} max={10} step={1} unit=""
          value={r.streakCooldown}
          onChange={(v) => upd("streakCooldown", v)}
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
          skipList={r.skipList}
          onToggle={(id) =>
            setR((prev) => ({
              ...prev,
              skipList: prev.skipList.includes(id)
                ? prev.skipList.filter((x) => x !== id)
                : [...prev.skipList, id],
            }))
          }
        />
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Current usage</h2>
          <span className="panel-meta">Today</span>
        </div>

        <div className="slider-row">
          <div>
            <div className="slider-label">Daily loss used</div>
            <div className="slider-desc">$142 of $500 daily cap · 28%</div>
          </div>
          <div className="slider-val" style={{ width: 220 }}>
            <div style={{ height: 6, background: "rgba(232, 228, 216, 0.1)", borderRadius: 3, overflow: "hidden" }}>
              <div style={{ width: "28%", height: "100%", background: "var(--gold)" }}></div>
            </div>
          </div>
          <div className="slider-val">28%</div>
        </div>

        <div className="slider-row">
          <div>
            <div className="slider-label">Drawdown</div>
            <div className="slider-desc">3.2% from peak · halt at 40%</div>
          </div>
          <div className="slider-val" style={{ width: 220 }}>
            <div style={{ height: 6, background: "rgba(232, 228, 216, 0.1)", borderRadius: 3, overflow: "hidden" }}>
              <div style={{ width: "8%", height: "100%", background: "var(--teal)" }}></div>
            </div>
          </div>
          <div className="slider-val">3.2%</div>
        </div>

        <div className="slider-row">
          <div>
            <div className="slider-label">Gross exposure</div>
            <div className="slider-desc">$1,080 across 5 positions · 7.3% of bankroll</div>
          </div>
          <div className="slider-val" style={{ width: 220 }}>
            <div style={{ height: 6, background: "rgba(232, 228, 216, 0.1)", borderRadius: 3, overflow: "hidden" }}>
              <div style={{ width: "36%", height: "100%", background: "var(--teal)" }}></div>
            </div>
          </div>
          <div className="slider-val">$1,080</div>
        </div>
      </div>
    </div>
  );
}

function Slider({
  label, desc, min, max, step, value, unit, onChange,
}: {
  label: string; desc: string; min: number; max: number; step: number;
  value: number; unit: string; onChange: (v: number) => void;
}) {
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
      <div className="slider-val">{value}{unit}</div>
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
                    title={disallowed ? "Disallowed — click to allow" : "Allowed — click to disallow"}
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
