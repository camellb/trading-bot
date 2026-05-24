import { proxyGet } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const url = new URL(request.url);
  return proxyGet("/api/admin/trades", url.search);
}
