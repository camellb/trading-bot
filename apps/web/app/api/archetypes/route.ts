import { NextRequest } from "next/server";

import { proxyGet } from "@/lib/bot-proxy";

export async function GET(req: NextRequest) {
  return proxyGet("/api/archetypes", req.nextUrl.search ?? "");
}
