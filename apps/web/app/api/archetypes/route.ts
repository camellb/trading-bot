import { proxyGet } from "@/lib/bot-proxy";

export async function GET() {
  return proxyGet("/api/archetypes");
}
