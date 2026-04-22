import { NextResponse } from "next/server";

import { getConfigData } from "@/lib/local-bot-data";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(await getConfigData());
  } catch (error) {
    const message = error instanceof Error ? error.message : "config failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
