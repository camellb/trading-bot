import { NextRequest, NextResponse } from "next/server";

import { getBrierTrendData } from "@/lib/local-bot-data";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const source = req.nextUrl.searchParams.get("source");
  try {
    return NextResponse.json(await getBrierTrendData(source));
  } catch (error) {
    const message = error instanceof Error ? error.message : "brier trend failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
