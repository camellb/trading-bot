import { NextResponse } from "next/server";

import { getSummaryData } from "@/lib/local-bot-data";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json(await getSummaryData());
  } catch (error) {
    const message = error instanceof Error ? error.message : "summary failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
