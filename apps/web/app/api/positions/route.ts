import { NextResponse } from "next/server";

import { getPositionsData } from "@/lib/local-bot-data";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(await getPositionsData());
  } catch (error) {
    const message = error instanceof Error ? error.message : "positions failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
