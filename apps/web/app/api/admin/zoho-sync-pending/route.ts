// apps/web/app/api/admin/zoho-sync-pending/route.ts
//
// Manual backstop for the Zoho Books retry sweep. The webhook
// already triggers a sweep via Next.js after() on every fire, so
// in steady state this endpoint shouldn't have to do much. It's
// for the cases where:
//
//   - Traffic has stopped entirely and pending rows aren't being
//     swept because no new webhooks are arriving.
//   - You're debugging why a specific row isn't syncing.
//   - You just want to confirm the sweep machinery is alive.
//
// Usage:
//
//   curl -X POST https://delfibot.com/api/admin/zoho-sync-pending \
//     -H "Authorization: Bearer $ADMIN_TOKEN"
//
// Returns:
//
//   {
//     "purchases": { "pending": N, "succeeded": M, "stillFailing": K },
//     "refunds":   { "pending": N, "succeeded": M, "stillFailing": K }
//   }
//
// `pending` is "rows we tried this run". `succeeded` is rows that
// synced ok. `stillFailing` is rows whose attempt count was bumped
// (and zoho_sync_last_error updated). Inspect the licenses table
// directly for `zoho_sync_last_error` text if you need the cause.

import { NextResponse } from "next/server";
import {
  syncPendingPurchases,
  syncPendingRefunds,
} from "@/lib/zoho-sync";

export const runtime = "nodejs";

function unauthorized() {
  return NextResponse.json(
    { error: "unauthorized" },
    { status: 401, headers: { "WWW-Authenticate": "Bearer" } },
  );
}

export async function POST(req: Request): Promise<NextResponse> {
  const adminToken = process.env.ADMIN_TOKEN;
  if (!adminToken) {
    return NextResponse.json(
      { error: "ADMIN_TOKEN not set" },
      { status: 503 },
    );
  }
  const auth = req.headers.get("authorization") ?? "";
  const m = auth.match(/^Bearer\s+(.+)$/i);
  if (!m || m[1] !== adminToken) return unauthorized();

  // Higher cap than the per-webhook sweep (5) because this is an
  // operator-initiated action; we expect them to want a real
  // chunk of work done.
  try {
    const purchases = await syncPendingPurchases(20);
    const refunds   = await syncPendingRefunds(20);
    return NextResponse.json({ purchases, refunds });
  } catch (e) {
    return NextResponse.json(
      {
        error: e instanceof Error ? e.message : String(e),
        hint:
          "If this is the first time the sweep ran, check that the " +
          "Zoho env vars are set (ZOHO_DC, ZOHO_CLIENT_ID, " +
          "ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID). " +
          "Use /api/admin/test-zoho first to verify auth and the " +
          "Books endpoints work end-to-end.",
      },
      { status: 502 },
    );
  }
}
