import { NextResponse } from "next/server";

import { getHealthData } from "@/lib/local-bot-data";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(await getHealthData());
  } catch (error) {
    const message = error instanceof Error ? error.message : "health failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
