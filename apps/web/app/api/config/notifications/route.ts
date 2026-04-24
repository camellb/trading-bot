import { NextRequest } from "next/server";
import { proxyGet, proxyPut } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const search = req.nextUrl.search ?? "";
  return proxyGet("/api/config/notifications", search);
}

export async function PUT(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  return proxyPut("/api/config/notifications", body);
}
