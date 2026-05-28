// apps/web/app/api/license/claim-device/route.ts
//
// Claim the single per-license activation slot for THIS device.
//
// Body:
//   {
//     "license_id":   "<uuid>",      // from the signed payload
//     "device_id":    "<32-hex>",    // SHA-256(machine identifier)
//     "device_label": "MacBook Pro", // optional, max 80 chars
//     "force":        false           // optional, kick existing device
//   }
//
// Returns:
//   200 { claimed: true, device_id }
//     - slot was free, now owned by this device
//     - OR slot was owned by THIS device already (idempotent re-claim)
//     - OR force=true and we overwrote whoever was there
//
//   409 { claimed: false, current_device_id, current_device_label,
//         activated_at, last_seen_at }
//     - slot owned by a DIFFERENT device, force was not set
//     - the GUI shows the "license active elsewhere" prompt and
//       re-calls with force=true if the user confirms
//
//   400 / 404 / 410 on bad input, missing licence, revoked licence.
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
// Device ID is opaque; we just check it looks like the desktop's hex digest.
const DEVICE_ID_RE = /^[0-9a-f]{16,64}$/i;

const DEVICE_LABEL_MAX = 80;

type Body = {
  license_id?: unknown;
  device_id?: unknown;
  device_label?: unknown;
  force?: unknown;
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
  const force      = body.force === true;

  if (!UUID_RE.test(license_id)) return bad("license_id must be a UUID");
  if (!DEVICE_ID_RE.test(device_id)) return bad("device_id must be a 16-64 char hex string");

  let device_label: string | null = null;
  if (typeof body.device_label === "string" && body.device_label.trim()) {
    device_label = body.device_label.trim().slice(0, DEVICE_LABEL_MAX);
  }

  const pool = db();

  // 1. Verify the licence exists and is not revoked. Without this a
  //    revoked licence could still claim a device slot (the desktop's
  //    daily revocation poll would catch it within 24h but we'd rather
  //    refuse here for clarity).
  let lic;
  try {
    const r = await pool.query(
      `SELECT id, revoked_at FROM licenses WHERE id = $1 LIMIT 1`,
      [license_id],
    );
    lic = r.rows[0];
  } catch (exc) {
    console.error("[claim-device] db lookup failed", exc);
    return bad("license lookup failed", 500);
  }
  if (!lic) return bad("license not found", 404);
  if (lic.revoked_at) return bad("license has been revoked", 410);

  // 2. Read the current activation slot (if any).
  let current;
  try {
    const r = await pool.query(
      `SELECT device_id, device_label, activated_at, last_seen_at
       FROM license_activations
       WHERE license_id = $1
       LIMIT 1`,
      [license_id],
    );
    current = r.rows[0];
  } catch (exc) {
    console.error("[claim-device] slot read failed", exc);
    return bad("license activation lookup failed", 500);
  }

  // 3a. Slot is free -> insert and return 200.
  if (!current) {
    try {
      await pool.query(
        `INSERT INTO license_activations
           (license_id, device_id, device_label, activated_at, last_seen_at)
         VALUES ($1, $2, $3, now(), now())`,
        [license_id, device_id, device_label],
      );
    } catch (exc) {
      console.error("[claim-device] insert failed", exc);
      return bad("license activation write failed", 500);
    }
    return NextResponse.json({ claimed: true, device_id });
  }

  // 3b. Slot held by THIS device -> heartbeat and 200 (idempotent).
  if (current.device_id === device_id) {
    try {
      await pool.query(
        `UPDATE license_activations
           SET last_seen_at = now(),
               device_label = COALESCE($2, device_label)
         WHERE license_id = $1`,
        [license_id, device_label],
      );
    } catch (exc) {
      // Best-effort heartbeat; never fail the claim because of this.
      console.error("[claim-device] heartbeat failed", exc);
    }
    return NextResponse.json({ claimed: true, device_id });
  }

  // 3c. Slot held by a DIFFERENT device.
  //   - force=true: overwrite. The old device's next periodic check
  //     will see device_match=false and lock itself.
  //   - force=false: 409 with current-device info so the GUI can
  //     prompt the user.
  if (!force) {
    return NextResponse.json(
      {
        claimed: false,
        reason: "another_device_active",
        current_device_id:    current.device_id,
        current_device_label: current.device_label,
        activated_at:         current.activated_at,
        last_seen_at:         current.last_seen_at,
      },
      { status: 409 },
    );
  }

  try {
    await pool.query(
      `UPDATE license_activations
         SET device_id    = $2,
             device_label = $3,
             activated_at = now(),
             last_seen_at = now()
       WHERE license_id  = $1`,
      [license_id, device_id, device_label],
    );
  } catch (exc) {
    console.error("[claim-device] force overwrite failed", exc);
    return bad("license activation overwrite failed", 500);
  }
  return NextResponse.json({
    claimed: true,
    device_id,
    forced_kick_of_previous_device: true,
  });
}
