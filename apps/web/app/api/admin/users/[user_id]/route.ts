import type { NextRequest } from "next/server";
import { proxyGet } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ user_id: string }> },
) {
  const { user_id } = await params;
  return proxyGet(`/api/admin/users/${encodeURIComponent(user_id)}`);
}
