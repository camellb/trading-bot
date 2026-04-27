import { NextResponse } from "next/server";

// License verification endpoint.
//
// Contract:
//   POST /api/license/verify
//   body:  { license_key: string, machine_id: string }
//   reply: { valid: boolean, expires_at: string | null, reason?: string }
//
// The desktop app calls this once a day. If it fails to reach us for an
// extended period (default: 14 days; configured app-side) the app falls
// back to an offline grace mode rather than locking the user out.
//
// Phase 1 (this stub): always returns valid=true so the desktop app can
// be developed and shipped before the licensing backend exists. The
// envelope is final; only the verification logic will change.
//
// Phase 2 (later): real lookup against a license store (Vercel KV or
// Supabase) keyed by license_key, returning a signed JWT the desktop
// app can verify offline against an embedded public key.

export const runtime = "edge";

interface VerifyRequest {
  license_key?: unknown;
  machine_id?: unknown;
}

interface VerifyResponse {
  valid: boolean;
  expires_at: string | null;
  reason?: string;
}

function bad(reason: string, status = 400): NextResponse<VerifyResponse> {
  return NextResponse.json<VerifyResponse>(
    { valid: false, expires_at: null, reason },
    { status },
  );
}

export async function POST(request: Request): Promise<NextResponse<VerifyResponse>> {
  let body: VerifyRequest;
  try {
    body = (await request.json()) as VerifyRequest;
  } catch {
    return bad("invalid JSON body");
  }

  const license_key = typeof body.license_key === "string" ? body.license_key.trim() : "";
  const machine_id = typeof body.machine_id === "string" ? body.machine_id.trim() : "";

  if (!license_key) {
    return bad("missing license_key");
  }
  if (!machine_id) {
    return bad("missing machine_id");
  }

  // Phase 1 stub: accept any well-formed request.
  // The shape of the response is the contract the desktop app codes
  // against; only the validity check changes in phase 2.
  return NextResponse.json<VerifyResponse>({
    valid: true,
    expires_at: null,
  });
}

// Reject other methods cleanly so curl/browser pokes get a clear answer.
export async function GET(): Promise<NextResponse<VerifyResponse>> {
  return bad("use POST", 405);
}
