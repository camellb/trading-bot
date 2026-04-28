/**
 * Tiny client for the Python sidecar's local HTTP API.
 *
 * The Tauri shell starts the sidecar and assigns it a loopback port at
 * launch. We fetch that port via the `get_api_port` IPC command, then
 * make all subsequent requests against `http://127.0.0.1:<port>`.
 *
 * If the page is opened outside Tauri (e.g. `vite dev` in a regular
 * browser, useful for UI hacking without the Rust shell), we fall back
 * to `VITE_DELFI_API_PORT` from `.env.local`.
 */

import { invoke } from "@tauri-apps/api/core";

let cachedPort: number | null = null;
let portPromise: Promise<number> | null = null;

async function fetchPort(): Promise<number> {
  if (cachedPort) return cachedPort;

  // Outside Tauri: rely on the env override so devs can poke the API
  // from a regular browser tab.
  if (typeof window !== "undefined" && !window.__TAURI_INTERNALS__) {
    const env = (import.meta as ImportMeta).env;
    const fallback = Number(env?.VITE_DELFI_API_PORT ?? 0);
    if (Number.isFinite(fallback) && fallback > 0) {
      cachedPort = fallback;
      return fallback;
    }
    throw new Error(
      "Not running inside Tauri and VITE_DELFI_API_PORT is unset. " +
        "Either run via `npm run tauri dev` or set the env var.",
    );
  }

  // Inside Tauri: poll the IPC command until it reports ready. Mirror
  // the 120s deadline on the Rust side - PyInstaller cold-start on a
  // first launch can take tens of seconds while the bundled Python
  // interpreter decompresses to a tempdir.
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const res = await invoke<{ port: number; ready: boolean }>(
      "get_api_port",
    );
    if (res.ready && res.port > 0) {
      cachedPort = res.port;
      return res.port;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("sidecar did not become ready within 120s");
}

async function port(): Promise<number> {
  if (cachedPort) return cachedPort;
  if (!portPromise) portPromise = fetchPort();
  return portPromise;
}

async function request<T>(
  path: string,
  init?: RequestInit & { body?: BodyInit },
): Promise<T> {
  const p = await port();
  const res = await fetch(`http://127.0.0.1:${p}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const text = await res.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`non-JSON response from ${path}: ${text.slice(0, 200)}`);
  }
  if (!res.ok) {
    const err = (data as { error?: string } | null)?.error ?? `HTTP ${res.status}`;
    throw new Error(`${path}: ${err}`);
  }
  return data as T;
}

// ── Typed endpoints ────────────────────────────────────────────────────

export interface HealthSnapshot {
  uptime_s: number;
  started_at: string | null;
  error_count: number;
  jobs: Record<string, { last_ok: string | null; last_error: string | null }>;
}

export interface BotState {
  mode: string | null;
  starting_cash: number | null;
  wallet_address: string | null;
  is_onboarded: boolean;
  can_trade_live: boolean;
  uptime_s: number;
  started_at: string | null;
  error_count: number;
}

export interface Credentials {
  wallet_address: string | null;
  has_polymarket_key: boolean;
  has_anthropic_key: boolean;
}

export interface PMPosition {
  id: number;
  user_id: string;
  market_id: string;
  question: string;
  side: "YES" | "NO";
  shares: number;
  entry_price: number;
  cost_usd: number;
  status: string;
  mode: string;
  created_at: string;
  settled_at: string | null;
  realized_pnl_usd: number | null;
  claude_probability?: number | null;
  market_archetype?: string | null;
  reasoning?: string | null;
  category?: string | null;
  settlement_outcome?: string | null;
  settlement_price?: number | null;
  expected_resolution_at?: string | null;
  ev_bps?: number | null;
  confidence?: number | null;
  [k: string]: unknown;
}

export interface EventLogRow {
  id: number;
  timestamp: string;
  event_type: string;
  severity: number | null;
  description: string;
  source: string;
  user_id: string;
}

export interface PerformanceSummary {
  mode: string | null;
  bankroll: number | null;
  equity: number | null;
  starting_cash: number | null;
  open_positions: number | null;
  open_cost: number | null;
  settled_total: number | null;
  settled_wins: number | null;
  win_rate: number | null;
  realized_pnl: number | null;
  roi: number | null;
  brier: number | null;
  resolved_predictions: number | null;
  total_predictions: number | null;
}

export interface BrierTrendPoint {
  date: string | null;
  brier: number;
  n: number;
}

export interface CalibrationBin {
  lo: number;
  hi: number;
  n: number;
  mean_pred: number | null;
  mean_actual: number | null;
}

/**
 * Shared shape for breakdown buckets returned by the calibration report.
 * Sidecar-side `get_report` populates pnl_usd / cost_usd / wins from
 * 2026-04-28 onward. Older sidecars omit them, so they're optional and
 * the Performance page guards with `?? 0`.
 */
export interface CalibrationBucket {
  n: number;
  brier: number | null;
  mean_pred: number | null;
  mean_actual: number | null;
  pnl_usd?: number;
  cost_usd?: number;
  wins?: number;
}

export interface CalibrationReport {
  source: string | null;
  since_days: number | null;
  total: number;
  resolved: number;
  unresolved: number;
  brier: number | null;
  mean_prob: number | null;
  mean_outcome: number | null;
  realized_pnl_usd: number | null;
  bins: CalibrationBin[];
  by_category: Array<CalibrationBucket & { category: string | null; win_rate?: number | null }>;
  by_archetype: Array<CalibrationBucket & { archetype: string | null }>;
  by_horizon: Array<CalibrationBucket & { bucket: string }>;
}

export interface PendingSuggestion {
  id: number;
  created_at: string | null;
  param_name: string;
  current_value: number | null;
  proposed_value: number | null;
  evidence: string | null;
  backtest_delta: number | null;
  backtest_trades: number | null;
  status: string;
  settled_count: number | null;
  metadata: Record<string, unknown> | null;
}

export interface LearningReport {
  id: number;
  created_at: string | null;
  settled_count_at_creation: number | null;
  thesis: string | null;
  body: Record<string, unknown> | string | null;
  user_id: string;
}

export interface ArchetypeEntry {
  id: string;
  label: string;
  description: string;
  skip: boolean;
  multiplier: number;
  default_skip: boolean;
  default_mult: number;
}

export interface ArchetypeCatalogue {
  archetypes: ArchetypeEntry[];
  bounds: { multiplier_min: number; multiplier_max: number };
}

export interface MarketEvaluation {
  id: number;
  evaluated_at: string;
  market_id: string;
  question: string;
  category: string | null;
  market_price_yes: number;
  claude_probability: number;
  confidence: number | null;
  ev_bps: number | null;
  recommendation: string | null;
  reasoning_short: string | null;
  reasoning: string | null;
  market_archetype: string | null;
  [k: string]: unknown;
}

export interface TelegramConfig {
  has_telegram_token: boolean;
  telegram_chat_id: string | null;
  is_configured: boolean;
}

export interface NotificationsConfig {
  categories: string[];
  notification_prefs: Record<string, boolean>;
}

export const api = {
  // Process / config
  health:        () => request<HealthSnapshot>("/api/health"),
  state:         () => request<BotState>("/api/state"),
  config:        () => request<Record<string, unknown>>("/api/config"),
  credentials:   () => request<Credentials>("/api/credentials"),

  // Live data
  positions:     (limit = 100) =>
    request<{ positions: PMPosition[] }>(`/api/positions?limit=${limit}`),
  events:        (limit = 200) =>
    request<{ events: EventLogRow[] }>(`/api/events?limit=${limit}`),
  evaluations:   (limit = 100) =>
    request<{ evaluations: MarketEvaluation[] }>(`/api/evaluations?limit=${limit}`),

  // Mutations
  updateConfig:  (changes: Record<string, unknown>) =>
    request<Record<string, unknown>>("/api/config", {
      method: "PUT",
      body: JSON.stringify(changes),
    }),
  saveCredentials: (creds: {
    polymarket_private_key?: string;
    wallet_address?: string;
    anthropic_api_key?: string;
  }) =>
    request<Credentials & { wrote: string[] }>("/api/credentials", {
      method: "PUT",
      body: JSON.stringify(creds),
    }),

  // Bot lifecycle
  start: () => request<{ mode: string }>("/api/bot/start", { method: "POST" }),
  stop:  () => request<{ mode: string }>("/api/bot/stop",  { method: "POST" }),
  scan:  () => request<{ queued: boolean }>("/api/scan",   { method: "POST" }),
  resetSimulation: () =>
    request<{ ok: boolean; detail: string }>("/api/reset-simulation", {
      method: "POST",
    }),

  // Performance + learning
  summary:     () => request<PerformanceSummary>("/api/summary"),
  brierTrend:  () => request<{ points: BrierTrendPoint[] }>("/api/brier-trend"),
  calibration: (opts?: { source?: string; since_days?: number }) => {
    const q = new URLSearchParams();
    if (opts?.source) q.set("source", opts.source);
    if (opts?.since_days) q.set("since_days", String(opts.since_days));
    const qs = q.toString();
    return request<CalibrationReport>(`/api/calibration${qs ? `?${qs}` : ""}`);
  },
  suggestions: () =>
    request<{ suggestions: PendingSuggestion[] }>("/api/suggestions"),
  applySuggestion: (id: number) =>
    request<Record<string, unknown>>(`/api/suggestions/${id}/apply`, {
      method: "POST",
    }),
  skipSuggestion: (id: number) =>
    request<Record<string, unknown>>(`/api/suggestions/${id}/skip`, {
      method: "POST",
    }),
  snoozeSuggestion: (id: number, wait_trades?: number) =>
    request<Record<string, unknown>>(`/api/suggestions/${id}/snooze`, {
      method: "POST",
      body: JSON.stringify(wait_trades ? { wait_trades } : {}),
    }),
  learningReports: (limit = 10) =>
    request<{ reports: LearningReport[] }>(`/api/learning-reports?limit=${limit}`),

  // Archetypes
  archetypes: () => request<ArchetypeCatalogue>("/api/archetypes"),

  // Telegram + notifications
  telegram:        () => request<TelegramConfig>("/api/config/telegram"),
  saveTelegram:    (changes: { telegram_bot_token?: string | null; telegram_chat_id?: string | null }) =>
    request<TelegramConfig & { wrote: string[] }>("/api/config/telegram", {
      method: "PUT",
      body: JSON.stringify(changes),
    }),
  testTelegram:    () =>
    request<{ ok: boolean; detail: string }>("/api/config/telegram/test", {
      method: "POST",
    }),
  notifications:   () => request<NotificationsConfig>("/api/config/notifications"),
  saveNotifications: (prefs: Record<string, boolean>) =>
    request<NotificationsConfig>("/api/config/notifications", {
      method: "PUT",
      body: JSON.stringify({ notification_prefs: prefs }),
    }),
};
