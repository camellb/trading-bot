// apps/web/app/api/license/check/route.ts
//
// Periodic revocation check called by the desktop app.
//
// Why this exists: the desktop license verifier in
// `Delfibot/bot/engine/license.py` is offline-only - it checks the
// signed blob against the embedded public key and that's it. If we
// refund a customer (charge.refunded webhook -> licenses.revoked_at
// gets stamped), nothing on their machine knows. They keep trading
// forever.
//
// The sidecar polls this endpoint once a day with its license id.
// On `revoked: true` it clears the local keychain entry, the next
// /api/license/status call returns invalid, and the LicenseGate
// re-renders.
//
// Privacy: the only thing the desktop app sends is the UUID from the
// signed payload. No email, no IP analytics. The endpoint returns
// the minimum the client needs.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { Pool } from "pg";

export const runtime = "nodejs";

let pgPool: Pool | null = null;
function db(): Pool {
  if (pgPool) return pgPool;
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error("DATABASE_URL is not set");
  }
  pgPool = new Pool({
    connectionString: url,
    max: 2,
    idleTimeoutMillis: 5_000,
  });
  return pgPool;
}

// Loose UUID v4-ish shape check; the real validation is the DB lookup.
// We do this before hitting the DB to avoid SQL parse work on garbage.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function GET(req: NextRequest) {
  const id = req.nextUrl.searchParams.get("id");
  if (!id || !UUID_RE.test(id)) {
    return NextResponse.json(
      { error: "id query param is required and must be a UUID" },
      { status: 400 },
    );
  }

  let row;
  try {
    const r = await db().query(
      `SELECT id, revoked_at, revoke_reason
       FROM licenses
       WHERE id = $1
       LIMIT 1`,
      [id],
    );
    row = r.rows[0];
  } catch (exc) {
    console.error("[license-check] db query failed", exc);
    return NextResponse.json(
      { error: "license lookup failed" },
      { status: 500 },
    );
  }

  if (!row) {
    // No row at all. Could be: license issued by a sibling deployment
    // (test mode), or a forged blob whose signature happened to verify
    // against the embedded public key (impossible without the private
    // key), or a license whose row was manually deleted. Conservative:
    // treat "not found" as invalid so the client falls back to the
    // gate, but DON'T cache this state aggressively client-side (the
    // client should retry on the next interval).
    return NextResponse.json(
      {
        valid: false,
        revoked_at: null,
        revoke_reason: "license id not found",
      },
      { status: 200 },
    );
  }

  const revoked_at: string | null = row.revoked_at
    ? new Date(row.revoked_at).toISOString()
    : null;
  return NextResponse.json({
    valid: revoked_at === null,
    revoked_at,
    revoke_reason: row.revoke_reason ?? null,
  });
}
