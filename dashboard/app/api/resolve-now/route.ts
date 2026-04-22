import { proxyPost } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function POST() {
  return proxyPost("/api/resolve-now", {});
}
