import { proxyGet, proxyPost, proxyDelete } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET() {
  return proxyGet("/api/controls/blocked-markets");
}

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  return proxyPost("/api/controls/blocked-markets", body);
}

export async function DELETE(request: Request) {
  const { searchParams } = new URL(request.url);
  const search = searchParams.toString() ? `?${searchParams.toString()}` : "";
  return proxyDelete("/api/controls/blocked-markets", search);
}
