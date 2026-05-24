import { NextRequest } from "next/server";
import { proxyPost } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxyPost("/api/switch-mode", body);
}
