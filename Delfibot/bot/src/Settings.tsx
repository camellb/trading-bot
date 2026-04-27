import { useEffect, useState } from "react";
import { api, Credentials } from "./api";

/**
 * Settings panel.
 *
 * Three forms, each independently submittable:
 *   1. Credentials. Polymarket private key + wallet, Anthropic API key.
 *      Secrets go to the OS keychain via PUT /api/credentials.
 *   2. Bankroll. starting_cash. PUT /api/config.
 *   3. Risk and sizing. base_stake_pct, max_stake_pct, daily/weekly/drawdown
 *      loss limits, streak cooldown, dry powder reserve, per-archetype
 *      skip list and stake multipliers. All clamped server-side by
 *      USER_CONFIG_BOUNDS in engine/user_config.py; we mirror the bounds
 *      here so users get inline feedback before submitting.
 *
 * The component is purely controlled, hydrating from `creds` and
 * `config` props the parent fetches via /api/credentials and /api/config.
 * On a successful save the parent calls `onSaved()` to refresh.
 */

// Bounds mirror engine/user_config.py:USER_CONFIG_BOUNDS. Keep in sync.
const BOUNDS = {
  base_stake_pct:        [0.005, 0.05],
  max_stake_pct:         [0.01,  0.10],
  daily_loss_limit_pct:  [0.01,  1.00],
  weekly_loss_limit_pct: [0.01,  1.00],
  drawdown_halt_pct:     [0.01,  1.00],
  streak_cooldown_losses:[2,     10],
  dry_powder_reserve_pct:[0.10,  0.40],
  starting_cash:         [10,    100_000],
} as const;

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
};

interface Props {
  creds: Credentials | null;
  config: ConfigShape | null;
  onSaved: () => void;
}

export default function Settings({ creds, config, onSaved }: Props) {
  // ── Credentials form state ────────────────────────────────────────────────
  const [pmKey, setPmKey] = useState("");
  const [wallet, setWallet] = useState("");
  const [anthropic, setAnthropic] = useState("");
  const [credsMsg, setCredsMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [credsBusy, setCredsBusy] = useState(false);

  useEffect(() => {
    if (creds) setWallet(creds.wallet_address ?? "");
  }, [creds]);

  const saveCreds = async (e: React.FormEvent) => {
    e.preventDefault();
    setCredsBusy(true);
    setCredsMsg(null);
    try {
      const payload: Record<string, string> = {};
      if (pmKey.trim())     payload.polymarket_private_key = pmKey.trim();
      if (wallet.trim())    payload.wallet_address = wallet.trim();
      if (anthropic.trim()) payload.anthropic_api_key = anthropic.trim();
      if (Object.keys(payload).length === 0) {
        setCredsMsg({ kind: "err", text: "Nothing to save." });
        return;
      }
      const res = await api.saveCredentials(payload);
      setPmKey("");
      setAnthropic("");
      setCredsMsg({
        kind: "ok",
        text: `Saved: ${res.wrote.join(", ") || "nothing"}.`,
      });
      onSaved();
    } catch (err) {
      setCredsMsg({
        kind: "err",
        text: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setCredsBusy(false);
    }
  };

  // ── Bankroll form state ───────────────────────────────────────────────────
  const [startingCash, setStartingCash] = useState<string>("");
  const [bankrollMsg, setBankrollMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [bankrollBusy, setBankrollBusy] = useState(false);

  useEffect(() => {
    if (config?.starting_cash != null) {
      setStartingCash(String(config.starting_cash));
    }
  }, [config?.starting_cash]);

  const saveBankroll = async (e: React.FormEvent) => {
    e.preventDefault();
    setBankrollBusy(true);
    setBankrollMsg(null);
    try {
      const n = Number(startingCash);
      if (!Number.isFinite(n)) throw new Error("Starting cash must be a number.");
      const [lo, hi] = BOUNDS.starting_cash;
      if (n < lo || n > hi) throw new Error(`Starting cash must be between ${lo} and ${hi}.`);
      await api.updateConfig({ starting_cash: n });
      setBankrollMsg({ kind: "ok", text: `Bankroll set to $${n.toFixed(2)}.` });
      onSaved();
    } catch (err) {
      setBankrollMsg({
        kind: "err",
        text: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBankrollBusy(false);
    }
  };

  // ── Risk + sizing form state ──────────────────────────────────────────────
  const [risk, setRisk] = useState({
    base_stake_pct: "",
    max_stake_pct: "",
    daily_loss_limit_pct: "",
    weekly_loss_limit_pct: "",
    drawdown_halt_pct: "",
    streak_cooldown_losses: "",
    dry_powder_reserve_pct: "",
    archetype_skip_list: "",
    archetype_stake_multipliers: "",
  });
  const [riskMsg, setRiskMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [riskBusy, setRiskBusy] = useState(false);

  useEffect(() => {
    if (!config) return;
    setRisk({
      base_stake_pct:         config.base_stake_pct         != null ? String(config.base_stake_pct)         : "",
      max_stake_pct:          config.max_stake_pct          != null ? String(config.max_stake_pct)          : "",
      daily_loss_limit_pct:   config.daily_loss_limit_pct   != null ? String(config.daily_loss_limit_pct)   : "",
      weekly_loss_limit_pct:  config.weekly_loss_limit_pct  != null ? String(config.weekly_loss_limit_pct)  : "",
      drawdown_halt_pct:      config.drawdown_halt_pct      != null ? String(config.drawdown_halt_pct)      : "",
      streak_cooldown_losses: config.streak_cooldown_losses != null ? String(config.streak_cooldown_losses) : "",
      dry_powder_reserve_pct: config.dry_powder_reserve_pct != null ? String(config.dry_powder_reserve_pct) : "",
      archetype_skip_list:
        Array.isArray(config.archetype_skip_list)
          ? config.archetype_skip_list.join(", ")
          : "",
      archetype_stake_multipliers:
        config.archetype_stake_multipliers && typeof config.archetype_stake_multipliers === "object"
          ? JSON.stringify(config.archetype_stake_multipliers, null, 2)
          : "",
    });
  }, [config]);

  const saveRisk = async (e: React.FormEvent) => {
    e.preventDefault();
    setRiskBusy(true);
    setRiskMsg(null);
    try {
      const changes: Record<string, unknown> = {};

      const numericKeys = [
        "base_stake_pct",
        "max_stake_pct",
        "daily_loss_limit_pct",
        "weekly_loss_limit_pct",
        "drawdown_halt_pct",
        "dry_powder_reserve_pct",
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
        if (!Number.isInteger(n)) throw new Error("streak_cooldown_losses must be an integer.");
        const [lo, hi] = BOUNDS.streak_cooldown_losses;
        if (n < lo || n > hi) throw new Error(`streak_cooldown_losses must be between ${lo} and ${hi}.`);
        changes.streak_cooldown_losses = n;
      }

      const skipRaw = risk.archetype_skip_list.trim();
      if (skipRaw !== "") {
        const items = skipRaw.split(",").map((s) => s.trim()).filter(Boolean);
        changes.archetype_skip_list = items;
      } else if (risk.archetype_skip_list === "") {
        // Empty input clears the list explicitly.
        changes.archetype_skip_list = [];
      }

      const multRaw = risk.archetype_stake_multipliers.trim();
      if (multRaw !== "") {
        let parsed: unknown;
        try {
          parsed = JSON.parse(multRaw);
        } catch {
          throw new Error("archetype_stake_multipliers must be valid JSON.");
        }
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("archetype_stake_multipliers must be a JSON object.");
        }
        const clean: Record<string, number> = {};
        for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
          const n = Number(v);
          if (!Number.isFinite(n)) throw new Error(`Multiplier for "${k}" must be numeric.`);
          if (n < 0.1 || n > 10) throw new Error(`Multiplier for "${k}" must be between 0.1 and 10.`);
          clean[k] = n;
        }
        changes.archetype_stake_multipliers = clean;
      } else {
        changes.archetype_stake_multipliers = {};
      }

      if (Object.keys(changes).length === 0) {
        setRiskMsg({ kind: "err", text: "Nothing to save." });
        return;
      }
      await api.updateConfig(changes);
      setRiskMsg({
        kind: "ok",
        text: `Saved ${Object.keys(changes).length} field(s).`,
      });
      onSaved();
    } catch (err) {
      setRiskMsg({
        kind: "err",
        text: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setRiskBusy(false);
    }
  };

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="settings">
      <form className="settings-card" onSubmit={saveCreds}>
        <h2>Credentials</h2>
        <p className="hint">
          Private keys go to the OS keychain. The wallet address is the
          public 0x address that pairs with your Polymarket private key.
          Leaving a key field blank keeps the existing value.
        </p>
        <label>
          Polymarket private key
          <input
            type="password"
            autoComplete="off"
            placeholder={creds?.has_polymarket_key ? "(stored)" : "0x..."}
            value={pmKey}
            onChange={(e) => setPmKey(e.target.value)}
          />
        </label>
        <label>
          Wallet address
          <input
            type="text"
            autoComplete="off"
            placeholder="0x..."
            value={wallet}
            onChange={(e) => setWallet(e.target.value)}
          />
        </label>
        <label>
          Anthropic API key
          <input
            type="password"
            autoComplete="off"
            placeholder={creds?.has_anthropic_key ? "(stored)" : "sk-ant-..."}
            value={anthropic}
            onChange={(e) => setAnthropic(e.target.value)}
          />
        </label>
        <div className="form-actions">
          <button type="submit" disabled={credsBusy}>
            {credsBusy ? "Saving..." : "Save credentials"}
          </button>
          {credsMsg && (
            <span className={credsMsg.kind === "ok" ? "ok" : "err"}>
              {credsMsg.text}
            </span>
          )}
        </div>
      </form>

      <form className="settings-card" onSubmit={saveBankroll}>
        <h2>Bankroll</h2>
        <p className="hint">
          The starting cash Delfi treats as 100% of bankroll. Stake size
          and circuit breakers are computed against this number. In
          simulation mode it is the synthetic balance; in live mode it is
          your seeded capital.
        </p>
        <label>
          Starting cash (USD)
          <input
            type="number"
            min={BOUNDS.starting_cash[0]}
            max={BOUNDS.starting_cash[1]}
            step="1"
            value={startingCash}
            onChange={(e) => setStartingCash(e.target.value)}
          />
        </label>
        <div className="form-actions">
          <button type="submit" disabled={bankrollBusy}>
            {bankrollBusy ? "Saving..." : "Save bankroll"}
          </button>
          {bankrollMsg && (
            <span className={bankrollMsg.kind === "ok" ? "ok" : "err"}>
              {bankrollMsg.text}
            </span>
          )}
        </div>
      </form>

      <form className="settings-card" onSubmit={saveRisk}>
        <h2>Risk and sizing</h2>
        <p className="hint">
          Sizing is flat (V1 doctrine): stake = bankroll * base_stake_pct *
          archetype_multiplier, capped at max_stake_pct. Loss limits halt
          new trades when realized loss crosses the threshold. All values
          are fractions of bankroll (0.05 = 5%).
        </p>
        <div className="grid-2">
          <label>
            Base stake (fraction)
            <input
              type="number"
              step="0.001"
              min={BOUNDS.base_stake_pct[0]}
              max={BOUNDS.base_stake_pct[1]}
              value={risk.base_stake_pct}
              onChange={(e) => setRisk({ ...risk, base_stake_pct: e.target.value })}
            />
          </label>
          <label>
            Max stake (fraction)
            <input
              type="number"
              step="0.001"
              min={BOUNDS.max_stake_pct[0]}
              max={BOUNDS.max_stake_pct[1]}
              value={risk.max_stake_pct}
              onChange={(e) => setRisk({ ...risk, max_stake_pct: e.target.value })}
            />
          </label>
          <label>
            Daily loss limit (fraction)
            <input
              type="number"
              step="0.01"
              min={BOUNDS.daily_loss_limit_pct[0]}
              max={BOUNDS.daily_loss_limit_pct[1]}
              value={risk.daily_loss_limit_pct}
              onChange={(e) => setRisk({ ...risk, daily_loss_limit_pct: e.target.value })}
            />
          </label>
          <label>
            Weekly loss limit (fraction)
            <input
              type="number"
              step="0.01"
              min={BOUNDS.weekly_loss_limit_pct[0]}
              max={BOUNDS.weekly_loss_limit_pct[1]}
              value={risk.weekly_loss_limit_pct}
              onChange={(e) => setRisk({ ...risk, weekly_loss_limit_pct: e.target.value })}
            />
          </label>
          <label>
            Drawdown halt (fraction)
            <input
              type="number"
              step="0.01"
              min={BOUNDS.drawdown_halt_pct[0]}
              max={BOUNDS.drawdown_halt_pct[1]}
              value={risk.drawdown_halt_pct}
              onChange={(e) => setRisk({ ...risk, drawdown_halt_pct: e.target.value })}
            />
          </label>
          <label>
            Streak cooldown (losses)
            <input
              type="number"
              step="1"
              min={BOUNDS.streak_cooldown_losses[0]}
              max={BOUNDS.streak_cooldown_losses[1]}
              value={risk.streak_cooldown_losses}
              onChange={(e) => setRisk({ ...risk, streak_cooldown_losses: e.target.value })}
            />
          </label>
          <label>
            Dry powder reserve (fraction)
            <input
              type="number"
              step="0.01"
              min={BOUNDS.dry_powder_reserve_pct[0]}
              max={BOUNDS.dry_powder_reserve_pct[1]}
              value={risk.dry_powder_reserve_pct}
              onChange={(e) => setRisk({ ...risk, dry_powder_reserve_pct: e.target.value })}
            />
          </label>
        </div>
        <label>
          Archetype skip list (comma separated)
          <input
            type="text"
            placeholder="sports_other, hockey, cricket"
            value={risk.archetype_skip_list}
            onChange={(e) => setRisk({ ...risk, archetype_skip_list: e.target.value })}
          />
        </label>
        <label>
          Archetype stake multipliers (JSON)
          <textarea
            rows={5}
            placeholder='{"basketball": 1.5, "tennis": 0.5}'
            value={risk.archetype_stake_multipliers}
            onChange={(e) => setRisk({ ...risk, archetype_stake_multipliers: e.target.value })}
          />
        </label>
        <div className="form-actions">
          <button type="submit" disabled={riskBusy}>
            {riskBusy ? "Saving..." : "Save risk and sizing"}
          </button>
          {riskMsg && (
            <span className={riskMsg.kind === "ok" ? "ok" : "err"}>
              {riskMsg.text}
            </span>
          )}
        </div>
      </form>
    </div>
  );
}
