import type { NextRequest } from "next/server";
import { proxyGet, proxyPost } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET() {
  return proxyGet("/api/admin/scanner");
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  return proxyPost("/api/admin/scanner", body);
}
