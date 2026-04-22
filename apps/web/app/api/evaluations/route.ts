import { NextRequest, NextResponse } from "next/server";

import { getEvaluationsData } from "@/lib/local-bot-data";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const limit = Number(req.nextUrl.searchParams.get("limit") ?? 50);
  try {
    return NextResponse.json(await getEvaluationsData(Number.isFinite(limit) ? limit : 50));
  } catch (error) {
    const message = error instanceof Error ? error.message : "evaluations failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
