import { NextRequest, NextResponse } from "next/server";

import { getCalibrationReport } from "@/lib/local-bot-data";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const source = req.nextUrl.searchParams.get("source");
  const rawSinceDays = Number(req.nextUrl.searchParams.get("since_days") ?? 0);
  const sinceDays = Number.isFinite(rawSinceDays) && rawSinceDays > 0
    ? rawSinceDays
    : null;

  try {
    return NextResponse.json(await getCalibrationReport(source, sinceDays));
  } catch (error) {
    const message = error instanceof Error ? error.message : "calibration failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
