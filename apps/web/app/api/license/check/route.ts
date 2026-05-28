// apps/web/app/api/license/check/route.ts
//
// Periodic revocation + device-binding check called by the desktop app.
//
// Why this exists: the desktop license verifier in
// `Delfibot/bot/engine/license.py` is offline-only - it checks the
// signed blob against the embedded public key and that's it. If we
// refund a customer (charge.refunded webhook -> licenses.revoked_at
// gets stamped), nothing on their machine knows. They keep trading
// forever.
//
// Same story for the device slot: a v1.5.16+ desktop pastes the
// licence on machine A, server stores the slot. The user pastes the
// same blob on machine B and force-claims the slot. Machine A keeps
// running because its offline crypto check still passes. The daily
// poll here is what notices the slot has moved.
//
// The sidecar polls this endpoint once a day (and on every boot)
// with its license id AND its device fingerprint. The response
// includes:
//   - `valid`         : false iff the licence is revoked (refund,
//                       chargeback, admin revoke)
//   - `revoked_at`    / `revoke_reason` : revocation context
//   - `device_match`  : true iff the activation slot holds the
//                       caller's device_id (or is unclaimed - the
//                       pre-v1.5.16 grandfather case). The desktop
//                       treats `device_match: false` as a soft revoke
//                       with reason "license is in use on another
//                       device".
//
// Privacy: the desktop app sends the UUID from the signed payload
// plus its hashed device_id. Both are opaque on the server.

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
const DEVICE_ID_RE = /^[0-9a-f]{16,64}$/i;

export async function GET(req: NextRequest) {
  const id = req.nextUrl.searchParams.get("id");
  if (!id || !UUID_RE.test(id)) {
    return NextResponse.json(
      { error: "id query param is required and must be a UUID" },
      { status: 400 },
    );
  }

  // v1.5.16+ desktops include their hashed device fingerprint. Older
  // clients don't pass it; we omit device_match from the response in
  // that case (older code wouldn't read the field anyway).
  const rawDeviceId = req.nextUrl.searchParams.get("device_id") ?? "";
  const device_id = DEVICE_ID_RE.test(rawDeviceId) ? rawDeviceId : "";

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

  // Read the device slot (if any) and decide device_match. Three cases:
  //   - no row at all -> grandfather period (pre-v1.5.16 install
  //     never called claim-device, or owner-bypass). Treat as match:
  //     true so the desktop doesn't lock itself before the user has
  //     even gotten a chance to upgrade.
  //   - row exists and device_id matches -> match: true (and we
  //     heartbeat last_seen_at).
  //   - row exists and device_id differs -> match: false. Desktop
  //     locks within 24h.
  // If the caller didn't send a device_id, we omit the field entirely
  // for backwards compatibility with v1.5.0-v1.5.15 clients.
  let device_match: boolean | undefined;
  let current_device_label: string | null = null;
  if (device_id) {
    try {
      const r = await db().query(
        `SELECT device_id, device_label
         FROM license_activations
         WHERE license_id = $1
         LIMIT 1`,
        [id],
      );
      const slot = r.rows[0];
      if (!slot) {
        // Grandfather path. The desktop will next call claim-device
        // to populate the slot.
        device_match = true;
      } else if (slot.device_id === device_id) {
        device_match = true;
        // Heartbeat. Best-effort; never fails the check.
        try {
          await db().query(
            `UPDATE license_activations
               SET last_seen_at = now()
             WHERE license_id = $1`,
            [id],
          );
        } catch (exc) {
          console.error("[license-check] heartbeat failed", exc);
        }
      } else {
        device_match = false;
        current_device_label = slot.device_label ?? null;
      }
    } catch (exc) {
      console.error("[license-check] activation lookup failed", exc);
      // Soft-fail: don't lock the user on a transient DB hiccup.
      // Keep device_match undefined so the desktop falls back to
      // the offline-valid state.
    }
  }

  return NextResponse.json({
    valid: revoked_at === null,
    revoked_at,
    revoke_reason: row.revoke_reason ?? null,
    ...(device_match !== undefined ? { device_match } : {}),
    ...(device_match === false ? { current_device_label } : {}),
  });
}
