import { proxyGet, proxyPut } from "@/lib/bot-proxy";

export async function GET() {
  return proxyGet("/api/user-config");
}

export async function PUT(req: Request) {
  const body = await req.json().catch(() => ({}));
  return proxyPut("/api/user-config", body);
}
