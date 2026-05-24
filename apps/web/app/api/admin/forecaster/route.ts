import type { NextRequest } from "next/server";
import { proxyGet } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const days = req.nextUrl.searchParams.get("days") ?? "7";
  return proxyGet(`/api/admin/forecaster?days=${encodeURIComponent(days)}`);
}
