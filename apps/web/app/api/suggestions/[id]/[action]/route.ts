import { NextRequest, NextResponse } from "next/server";

import { proxyPost } from "@/lib/bot-proxy";

export const dynamic = "force-dynamic";

const ALLOWED = new Set(["apply", "skip", "snooze"]);

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string; action: string }> },
) {
  const { id, action } = await params;
  if (!ALLOWED.has(action)) {
    return NextResponse.json({ error: "invalid action" }, { status: 400 });
  }
  const body = await req.json().catch(() => ({}));
  return proxyPost(`/api/suggestions/${encodeURIComponent(id)}/${action}`, body);
}
