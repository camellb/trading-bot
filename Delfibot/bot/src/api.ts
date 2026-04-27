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

  // Inside Tauri: poll the IPC command until it reports ready.
  const deadline = Date.now() + 30_000;
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
  throw new Error("sidecar did not become ready within 30s");
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

// Typed endpoints

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

export const api = {
  health:        () => request<HealthSnapshot>("/api/health"),
  state:         () => request<BotState>("/api/state"),
  config:        () => request<Record<string, unknown>>("/api/config"),
  credentials:   () => request<Credentials>("/api/credentials"),
  positions:     (limit = 100) =>
    request<{ positions: PMPosition[] }>(`/api/positions?limit=${limit}`),
  events:        (limit = 200) =>
    request<{ events: EventLogRow[] }>(`/api/events?limit=${limit}`),

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

  start: () => request<{ mode: string }>("/api/bot/start", { method: "POST" }),
  stop:  () => request<{ mode: string }>("/api/bot/stop",  { method: "POST" }),
  scan:  () => request<{ queued: boolean }>("/api/scan",   { method: "POST" }),
};
