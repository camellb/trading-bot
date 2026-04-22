import "server-only";

import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

import { Pool, type QueryResultRow } from "pg";

const execFileAsync = promisify(execFile);

const ROOT_DIR = path.resolve(process.cwd(), "..");
const CONFIG_PATH = path.join(ROOT_DIR, "config.py");
const RUNTIME_CONFIG_PATH = path.join(ROOT_DIR, "runtime_config.json");
const BOT_MATCH = `${ROOT_DIR}/.venv/bin/python3 main.py`;

const RELIABILITY_BINS: Array<[number, number]> = [
  [0.0, 0.2],
  [0.2, 0.4],
  [0.4, 0.6],
  [0.6, 0.8],
  [0.8, 1.0],
];

const ALLOWED_CONFIG_KEYS = [
  "PM_SIMULATION_MIN_CONFIDENCE",
  "PM_LIVE_MIN_CONFIDENCE",
  "PM_MAX_POSITION_PCT",
  "PM_MIN_TRADE_USD",
  "PM_MAX_TRADE_USD",
  "PM_MAX_CONCURRENT_POSITIONS",
  "PM_SCAN_LIMIT",
  "PM_MIN_VOLUME_24H_USD",
  "PM_MAX_DAYS_TO_END",
  "PM_SKIP_EXISTING_DAYS",
] as const;

type ConfigValue = boolean | number | string | null;
type ConfigSnapshot = Record<string, ConfigValue>;

declare global {
  var __dashboardPgPool: Pool | undefined;
}

const pool =
  globalThis.__dashboardPgPool ??
  new Pool({
    connectionString: process.env.DATABASE_URL,
  });

globalThis.__dashboardPgPool = pool;

function parseNumeric(raw: string): number | null {
  const normalized = raw.replaceAll("_", "");
  if (!/^-?\d+(\.\d+)?$/.test(normalized)) return null;
  return Number(normalized);
}

function parseConfigLine(line: string): [string, ConfigValue] | null {
  const match = line.match(
    /^([A-Z0-9_]+)\s*=\s*(?:(['"])(.*?)\2|([^#\n]+))/,
  );
  if (!match) return null;

  const key = match[1];
  const quoted = match[3];
  const raw = (quoted ?? match[4] ?? "").trim();

  if (quoted != null) return [key, quoted];
  if (raw === "True") return [key, true];
  if (raw === "False") return [key, false];
  if (raw === "None") return [key, null];

  const numeric = parseNumeric(raw);
  if (numeric != null) return [key, numeric];

  return [key, raw];
}

async function readConfig(): Promise<ConfigSnapshot> {
  const text = await fs.readFile(CONFIG_PATH, "utf8");
  const snapshot: ConfigSnapshot = {};

  for (const line of text.split("\n")) {
    const parsed = parseConfigLine(line);
    if (!parsed) continue;
    snapshot[parsed[0]] = parsed[1];
  }

  // Overlay runtime overrides written by the dashboard / self-improvement.
  try {
    const raw = await fs.readFile(RUNTIME_CONFIG_PATH, "utf8");
    const overrides = JSON.parse(raw);
    if (overrides && typeof overrides === "object" && !Array.isArray(overrides)) {
      for (const [key, value] of Object.entries(overrides)) {
        if (/^[A-Z][A-Z0-9_]*$/.test(key)) {
          snapshot[key] = value as ConfigValue;
        }
      }
    }
  } catch (err: unknown) {
    const code = (err as NodeJS.ErrnoException)?.code;
    if (code !== "ENOENT") {
      console.error("[local-bot-data] runtime_config.json read failed", err);
    }
  }

  return snapshot;
}

function currentModeFromConfig(config: ConfigSnapshot): string {
  const raw = config.PM_MODE;
  return typeof raw === "string" && raw.trim()
    ? raw.trim().toLowerCase()
    : "simulation";
}

function startingCashFromConfig(config: ConfigSnapshot, mode: string): number {
  const simulation = typeof config.PM_SIMULATION_STARTING_CASH === "number"
    ? config.PM_SIMULATION_STARTING_CASH
    : 1000;
  const live = typeof config.PM_LIVE_STARTING_CASH === "number"
    ? config.PM_LIVE_STARTING_CASH
    : 200;
  return mode === "live" ? live : simulation;
}

function toNumber(value: unknown): number | null {
  if (value == null) return null;
  if (typeof value === "number") return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toIso(value: unknown): string | null {
  if (value == null) return null;
  if (value instanceof Date) return value.toISOString();
  const parsed = new Date(String(value));
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function parseJsonField<T>(value: unknown): T | null {
  if (value == null) return null;
  if (typeof value !== "string") return value as T;
  try {
    return JSON.parse(value) as T;
  } catch {
    return null;
  }
}

function parseElapsedSeconds(raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;

  const [dayPart, timePart] = text.includes("-")
    ? text.split("-", 2)
    : [null, text];

  const parts = timePart.split(":").map((part) => Number(part));
  if (parts.some((part) => !Number.isFinite(part))) return null;

  let seconds = 0;
  if (parts.length === 2) {
    seconds = (parts[0] * 60) + parts[1];
  } else if (parts.length === 3) {
    seconds = (parts[0] * 3600) + (parts[1] * 60) + parts[2];
  } else {
    return null;
  }

  if (dayPart != null) {
    const days = Number(dayPart);
    if (!Number.isFinite(days)) return null;
    seconds += days * 86_400;
  }

  return seconds;
}

async function detectBotStartedAt(): Promise<string | null> {
  try {
    const { stdout: pidOut } = await execFileAsync("pgrep", ["-f", BOT_MATCH]);
    const pid = pidOut.trim().split(/\s+/)[0];
    if (!pid) return null;

    const { stdout: etimeOut } = await execFileAsync("ps", ["-p", pid, "-o", "etime="]);
    const seconds = parseElapsedSeconds(etimeOut);
    if (seconds == null) return null;

    return new Date(Date.now() - (seconds * 1000)).toISOString();
  } catch {
    return null;
  }
}

async function query<T extends QueryResultRow>(
  sql: string,
  params: unknown[] = [],
): Promise<T[]> {
  const result = await pool.query<T>(sql, params);
  return result.rows;
}

function buildPredictionFilters(source?: string | null, sinceDays?: number | null) {
  const filters: string[] = [];
  const params: unknown[] = [];

  if (source && source !== "all") {
    params.push(source);
    filters.push(`source = $${params.length}`);
  }

  if (sinceDays && sinceDays > 0) {
    params.push(sinceDays);
    filters.push(`resolved_at >= NOW() - ($${params.length} * INTERVAL '1 day')`);
  }

  return {
    where: filters.length > 0 ? `WHERE ${filters.join(" AND ")}` : "",
    filters,
    params,
  };
}

const SCAN_STATUS_PATH = path.join(ROOT_DIR, "logs", "scan_status.json");

export async function getScanStatus() {
  try {
    const raw = await fs.readFile(SCAN_STATUS_PATH, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed;
    }
    return { phase: "idle" };
  } catch (err: unknown) {
    const code = (err as NodeJS.ErrnoException)?.code;
    if (code === "ENOENT") return { phase: "idle" };
    return { phase: "idle" };
  }
}

export async function getHealthData() {
  const config = await readConfig();
  const startedAt = await detectBotStartedAt();

  return {
    status: startedAt ? "ok" : "degraded",
    mode: currentModeFromConfig(config),
    started_at: startedAt,
  };
}

export async function getCalibrationReport(
  source: string | null = null,
  sinceDays: number | null = null,
) {
  const built = buildPredictionFilters(source, sinceDays);
  const totalsRows = await query<{
    total: string;
    resolved: string;
  }>(
    `
      SELECT
        COUNT(*)::int AS total,
        COUNT(resolved_at)::int AS resolved
      FROM predictions
      ${built.where}
    `,
    built.params,
  );

  const total = Number(totalsRows[0]?.total ?? 0);
  const resolved = Number(totalsRows[0]?.resolved ?? 0);

  const resolvedFilters = [...built.filters, "resolved_at IS NOT NULL"];
  const resolvedWhere = `WHERE ${resolvedFilters.join(" AND ")}`;

  const aggregateRows = await query<{
    mean_prob: string | null;
    mean_outcome: string | null;
    brier: string | null;
    pnl: string | null;
  }>(
    `
      SELECT
        AVG(probability) AS mean_prob,
        AVG(resolved_outcome::float) AS mean_outcome,
        AVG(POWER(probability - resolved_outcome, 2)) AS brier,
        SUM(resolved_pnl_usd) AS pnl
      FROM predictions
      ${resolvedWhere}
    `,
    built.params,
  );

  const agg = aggregateRows[0];
  const bins = [];
  for (const [lo, hi] of RELIABILITY_BINS) {
    const cmp = hi === 1 ? "<=" : "<";
    const rows = await query<{
      n: string;
      mean_pred: string | null;
      mean_actual: string | null;
    }>(
      `
        SELECT
          COUNT(*)::int AS n,
          AVG(probability) AS mean_pred,
          AVG(resolved_outcome::float) AS mean_actual
        FROM predictions
        ${resolvedWhere}
          AND probability >= $${built.params.length + 1}
          AND probability ${cmp} $${built.params.length + 2}
      `,
      [...built.params, lo, hi],
    );
    const row = rows[0];
    bins.push({
      lo,
      hi,
      n: Number(row?.n ?? 0),
      mean_pred: toNumber(row?.mean_pred),
      mean_actual: toNumber(row?.mean_actual),
    });
  }

  const byCategoryRows = await query<{
    category: string;
    n: string;
    brier: string | null;
    mean_pred: string | null;
    mean_actual: string | null;
  }>(
    `
      SELECT
        category,
        COUNT(*)::int AS n,
        AVG(POWER(probability - resolved_outcome, 2)) AS brier,
        AVG(probability) AS mean_pred,
        AVG(resolved_outcome::float) AS mean_actual
      FROM predictions
      ${resolvedWhere}
        AND category IS NOT NULL
      GROUP BY category
      ORDER BY n DESC
    `,
    built.params,
  );

  return {
    source: source ?? "all",
    since_days: sinceDays,
    total,
    resolved,
    unresolved: total - resolved,
    brier: toNumber(agg?.brier),
    mean_prob: toNumber(agg?.mean_prob),
    mean_outcome: toNumber(agg?.mean_outcome),
    realized_pnl_usd: toNumber(agg?.pnl),
    bins,
    by_category: byCategoryRows.map((row) => ({
      category: row.category,
      n: Number(row.n ?? 0),
      brier: toNumber(row.brier),
      mean_pred: toNumber(row.mean_pred),
      mean_actual: toNumber(row.mean_actual),
    })),
  };
}

export async function getSummaryData() {
  const config = await readConfig();
  const mode = currentModeFromConfig(config);
  const startingCash = startingCashFromConfig(config, mode);
  const calibration = await getCalibrationReport("polymarket");

  const positionRows = await query<{
    open_n: string;
    settled_n: string;
    open_cost: string | null;
    realized: string | null;
    wins: string;
  }>(
    `
      SELECT
        COUNT(*) FILTER (WHERE status = 'open')::int AS open_n,
        COUNT(*) FILTER (WHERE status IN ('settled', 'invalid'))::int AS settled_n,
        COALESCE(SUM(cost_usd) FILTER (WHERE status = 'open'), 0) AS open_cost,
        COALESCE(SUM(realized_pnl_usd) FILTER (WHERE status IN ('settled', 'invalid')), 0) AS realized,
        COUNT(*) FILTER (WHERE status IN ('settled', 'invalid') AND realized_pnl_usd > 0)::int AS wins
      FROM pm_positions
      WHERE mode = $1
    `,
    [mode],
  );

  const row = positionRows[0];
  const openPositions = Number(row?.open_n ?? 0);
  const settledTotal = Number(row?.settled_n ?? 0);
  const openCost = toNumber(row?.open_cost) ?? 0;
  const realized = toNumber(row?.realized) ?? 0;
  const settledWins = Number(row?.wins ?? 0);

  const earliestPredRows = await query<{ earliest: Date | string | null }>(
    `SELECT MIN(created_at) AS earliest FROM predictions WHERE source = 'polymarket'`,
    [],
  );
  const earliestRaw = earliestPredRows[0]?.earliest ?? null;
  let testEnd: string | null = null;
  if (earliestRaw) {
    const start = earliestRaw instanceof Date ? earliestRaw : new Date(earliestRaw);
    if (!Number.isNaN(start.getTime())) {
      testEnd = new Date(start.getTime() + 7 * 24 * 60 * 60 * 1000).toISOString();
    }
  }

  return {
    mode,
    bankroll: startingCash + realized - openCost,
    equity: startingCash + realized,
    starting_cash: startingCash,
    open_positions: openPositions,
    open_cost: openCost,
    settled_total: settledTotal,
    settled_wins: settledWins,
    win_rate: settledTotal > 0 ? settledWins / settledTotal : null,
    realized_pnl: realized,
    brier: calibration.brier,
    resolved_predictions: calibration.resolved,
    total_predictions: calibration.total,
    test_end: testEnd,
  };
}

export async function getPositionsData() {
  const config = await readConfig();
  const mode = currentModeFromConfig(config);

  const openRows = await query<{
    id: number;
    market_id: string;
    question: string;
    category: string | null;
    side: "YES" | "NO";
    shares: string;
    entry_price: string;
    cost_usd: string;
    claude_probability: string | null;
    ev_bps: string | null;
    confidence: string | null;
    expected_resolution_at: Date | null;
    created_at: Date | null;
    prediction_id: number | null;
    reasoning: string | null;
    slug: string | null;
  }>(
    `
      SELECT
        id, market_id, question, category, side, shares,
        entry_price, cost_usd, claude_probability,
        ev_bps, confidence, expected_resolution_at,
        created_at, prediction_id, reasoning, slug
      FROM pm_positions
      WHERE mode = $1 AND status = 'open'
      ORDER BY created_at DESC
    `,
    [mode],
  );

  const settledRows = await query<{
    id: number;
    market_id: string;
    question: string;
    category: string | null;
    side: "YES" | "NO";
    shares: string;
    entry_price: string;
    cost_usd: string;
    claude_probability: string | null;
    ev_bps: string | null;
    confidence: string | null;
    settlement_outcome: string | null;
    settlement_price: string | null;
    realized_pnl_usd: string | null;
    created_at: Date | null;
    settled_at: Date | null;
    slug: string | null;
  }>(
    `
      SELECT
        id, market_id, question, category, side, shares,
        entry_price, cost_usd, claude_probability, ev_bps,
        confidence, settlement_outcome, settlement_price,
        realized_pnl_usd, created_at, settled_at, slug
      FROM pm_positions
      WHERE mode = $1 AND status IN ('settled', 'invalid')
      ORDER BY settled_at DESC NULLS LAST
      LIMIT 50
    `,
    [mode],
  );

  return {
    open: openRows.map((row) => ({
      id: row.id,
      market_id: row.market_id,
      question: row.question,
      category: row.category,
      side: row.side,
      shares: toNumber(row.shares) ?? 0,
      entry_price: toNumber(row.entry_price) ?? 0,
      cost_usd: toNumber(row.cost_usd) ?? 0,
      claude_probability: toNumber(row.claude_probability),
      ev_bps: toNumber(row.ev_bps),
      confidence: toNumber(row.confidence),
      expected_resolution_at: toIso(row.expected_resolution_at),
      created_at: toIso(row.created_at),
      prediction_id: row.prediction_id,
      reasoning: row.reasoning,
      slug: row.slug,
    })),
    settled: settledRows.map((row) => ({
      id: row.id,
      market_id: row.market_id,
      question: row.question,
      category: row.category,
      side: row.side,
      shares: toNumber(row.shares) ?? 0,
      entry_price: toNumber(row.entry_price) ?? 0,
      cost_usd: toNumber(row.cost_usd) ?? 0,
      claude_probability: toNumber(row.claude_probability),
      ev_bps: toNumber(row.ev_bps),
      confidence: toNumber(row.confidence),
      settlement_outcome: row.settlement_outcome,
      settlement_price: toNumber(row.settlement_price),
      realized_pnl_usd: toNumber(row.realized_pnl_usd),
      created_at: toIso(row.created_at),
      settled_at: toIso(row.settled_at),
      slug: row.slug,
    })),
  };
}

export async function getEvaluationsData(limit = 50) {
  const rows = await query<{
    id: number;
    evaluated_at: Date | null;
    market_id: string;
    question: string;
    category: string | null;
    market_price_yes: string | null;
    claude_probability: string | null;
    confidence: string | null;
    ev_bps: string | null;
    recommendation: string | null;
    reasoning: string | null;
    pm_position_id: number | null;
    slug: string | null;
    research_sources: string | null;
  }>(
    `
      SELECT
        id, evaluated_at, market_id, question, category,
        market_price_yes, claude_probability, confidence,
        ev_bps, recommendation, reasoning, pm_position_id,
        slug, research_sources
      FROM market_evaluations
      ORDER BY evaluated_at DESC
      LIMIT $1
    `,
    [limit],
  );

  return {
    evaluations: rows.map((row) => ({
      id: row.id,
      evaluated_at: toIso(row.evaluated_at),
      market_id: row.market_id,
      question: row.question,
      category: row.category,
      market_price_yes: toNumber(row.market_price_yes),
      claude_probability: toNumber(row.claude_probability),
      confidence: toNumber(row.confidence),
      ev_bps: toNumber(row.ev_bps),
      recommendation: row.recommendation ?? "SKIP",
      reasoning: row.reasoning,
      pm_position_id: row.pm_position_id,
      slug: row.slug,
      research_sources: parseJsonField<string[]>(row.research_sources),
    })),
  };
}

export async function getBrierTrendData(source: string | null = "polymarket") {
  const params: unknown[] = [];
  let snapshotWhere = "";
  if (source && source !== "all") {
    params.push(source);
    snapshotWhere = `WHERE source = $${params.length}`;
  }

  const snapshotRows = await query<{
    captured_at: Date | null;
    resolved: string | null;
    brier: string | null;
  }>(
    `
      SELECT captured_at, resolved, brier
      FROM calibration_snapshots
      ${snapshotWhere}
      ORDER BY captured_at ASC
    `,
    params,
  );

  if (snapshotRows.length >= 2) {
    return {
      points: snapshotRows
        .filter((row) => row.brier != null)
        .map((row) => ({
          date: toIso(row.captured_at),
          brier: toNumber(row.brier) ?? 0,
          n: Number(row.resolved ?? 0),
        })),
    };
  }

  const predictionParams: unknown[] = [];
  let predictionWhere = "WHERE resolved_at IS NOT NULL";
  if (source && source !== "all") {
    predictionParams.push(source);
    predictionWhere += ` AND source = $${predictionParams.length}`;
  }

  const predictionRows = await query<{
    resolved_at: Date | null;
    probability: string;
    resolved_outcome: string;
  }>(
    `
      SELECT resolved_at, probability, resolved_outcome
      FROM predictions
      ${predictionWhere}
      ORDER BY resolved_at ASC
    `,
    predictionParams,
  );

  let running = 0;
  const points = predictionRows.map((row, index) => {
    const probability = toNumber(row.probability) ?? 0;
    const outcome = toNumber(row.resolved_outcome) ?? 0;
    running += (probability - outcome) ** 2;
    return {
      date: toIso(row.resolved_at),
      brier: Number((running / (index + 1)).toFixed(4)),
      n: index + 1,
    };
  });

  return { points };
}

export async function getConfigData() {
  const config = await readConfig();
  const mode = currentModeFromConfig(config);

  return {
    config,
    active_mode: mode,
    configured_mode: mode,
    restart_pending: false,
    allowed_keys: [...ALLOWED_CONFIG_KEYS],
    pending: null,
  };
}
