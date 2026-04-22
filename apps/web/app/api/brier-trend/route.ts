import { NextRequest } from "next/server";

import { proxyGet } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  return proxyGet("/api/brier-trend", req.nextUrl.search ?? "");
}
