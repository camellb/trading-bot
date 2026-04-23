import type { NextRequest } from "next/server";
import { proxyPost } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ user_id: string }> },
) {
  const { user_id } = await params;
  const body = await req.json().catch(() => ({}));
  return proxyPost(
    `/api/admin/users/${encodeURIComponent(user_id)}/action`,
    body,
  );
}
