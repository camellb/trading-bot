import { proxyGet } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const scope = url.searchParams.get("scope") ?? "all";
  const search = `?scope=${encodeURIComponent(scope)}`;
  return proxyGet("/api/diagnostics", search);
}
