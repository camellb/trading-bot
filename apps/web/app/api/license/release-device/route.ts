// apps/web/app/api/license/release-device/route.ts
//
// Release THIS device's activation slot.
//
// Triggered by the Settings -> License -> Log out button. After this
// call the license can be activated on any other machine without
// hitting the 409 "active elsewhere" path.
//
// Body:
//   {
//     "license_id": "<uuid>",
//     "device_id":  "<32-hex>"
//   }
//
// Returns:
//   200 { released: true }
//     - the slot WAS owned by this device and is now empty
//     - OR the slot was already empty (idempotent)
//     - OR the slot was owned by a DIFFERENT device. We still return
//       200 because we do NOT want this endpoint to leak which
//       devices currently hold which licences; releasing a slot you
//       don't own is a no-op from the caller's perspective.
//
//   400 on bad input.
//
// Doctrine: Obsidian/Delfi/50_Feedback/license_one_device_at_a_time.md

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { Pool } from "pg";

export const runtime = "nodejs";

let pgPool: Pool | null = null;
function db(): Pool {
  if (pgPool) return pgPool;
  const url = process.env.DATABASE_URL;
  if (!url) throw new Error("DATABASE_URL is not set");
  pgPool = new Pool({
    connectionString: url,
    max: 2,
    idleTimeoutMillis: 5_000,
  });
  return pgPool;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DEVICE_ID_RE = /^[0-9a-f]{16,64}$/i;

type Body = {
  license_id?: unknown;
  device_id?: unknown;
};

function bad(msg: string, status: number = 400) {
  return NextResponse.json({ error: msg }, { status });
}

export async function POST(req: NextRequest) {
  let body: Body;
  try {
    body = (await req.json()) as Body;
  } catch {
    return bad("body must be valid JSON");
  }

  const license_id = typeof body.license_id === "string" ? body.license_id.trim() : "";
  const device_id  = typeof body.device_id  === "string" ? body.device_id.trim()  : "";

  if (!UUID_RE.test(license_id)) return bad("license_id must be a UUID");
  if (!DEVICE_ID_RE.test(device_id)) return bad("device_id must be a 16-64 char hex string");

  // We delete the row only if license_id AND device_id match. If the
  // slot is held by someone else, the WHERE clause matches nothing and
  // we return 200 anyway. That's the privacy property described in the
  // route header: callers can't probe for which devices hold which
  // licences by hitting this endpoint.
  try {
    await db().query(
      `DELETE FROM license_activations
       WHERE license_id = $1 AND device_id = $2`,
      [license_id, device_id],
    );
  } catch (exc) {
    console.error("[release-device] delete failed", exc);
    return bad("license activation delete failed", 500);
  }

  return NextResponse.json({ released: true });
}
