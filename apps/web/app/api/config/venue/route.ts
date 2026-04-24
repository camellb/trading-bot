import { NextRequest } from "next/server";
import { proxyGet, proxyPut } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

// Thin proxy to the bot's /api/config/venue. Returns venue + credential
// status for BOTH Polymarket and Polymarket US, and accepts atomic updates
// of venue + per-venue credential bundles. Secrets are never echoed back.
export async function GET(req: NextRequest) {
  const search = req.nextUrl.search ?? "";
  return proxyGet("/api/config/venue", search);
}

export async function PUT(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  return proxyPut("/api/config/venue", body);
}
