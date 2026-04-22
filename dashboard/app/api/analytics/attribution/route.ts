import { proxyGet } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const search = searchParams.toString() ? `?${searchParams.toString()}` : "";
  return proxyGet("/api/analytics/attribution", search);
}
