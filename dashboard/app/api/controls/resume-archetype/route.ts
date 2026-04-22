import { proxyPost } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  return proxyPost("/api/controls/resume-archetype", body);
}
