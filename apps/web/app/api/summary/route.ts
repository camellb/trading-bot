import { proxyGet } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET() {
  return proxyGet("/api/summary");
}
