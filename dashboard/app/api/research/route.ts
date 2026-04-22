import { NextRequest } from "next/server";
import { proxyPost } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  return proxyPost("/api/research", body, 60_000);
}
