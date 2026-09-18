/**
 * Daily keep-alive for the license database (Vercel cron, see
 * vercel.json). A hosted Postgres on a free tier is paused after a week
 * without queries; on 2026-09-18 that took down license delivery and
 * activation. One cheap read a day keeps it awake and doubles as a
 * health signal: a non-200 here means a buyer would not get a key.
 */
import { NextResponse } from "next/server";
import { Pool } from "pg";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

let pgPool: Pool | null = null;
function db(): Pool {
  if (pgPool) return pgPool;
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error("DATABASE_URL is not set");
  }
  pgPool = new Pool({
    connectionString: url,
    max: 1,
    idleTimeoutMillis: 5_000,
  });
  return pgPool;
}

export async function GET(): Promise<NextResponse> {
  try {
    const r = await db().query(`SELECT count(*)::int AS n FROM licenses`);
    return NextResponse.json({ ok: true, licenses: r.rows[0]?.n ?? 0 });
  } catch (exc) {
    console.error("[keepalive] license database unreachable", exc);
    return NextResponse.json(
      { ok: false, error: "license database unreachable" },
      { status: 503 },
    );
  }
}
