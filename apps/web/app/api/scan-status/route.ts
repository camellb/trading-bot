import { NextResponse } from "next/server";

import { getScanStatus } from "@/lib/local-bot-data";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(await getScanStatus());
  } catch (error) {
    const message = error instanceof Error ? error.message : "scan-status failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
