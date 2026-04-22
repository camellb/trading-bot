import { NextRequest } from "next/server";

import { proxyGet } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const search = req.nextUrl.search;
  return proxyGet("/api/suggestions", search);
}
