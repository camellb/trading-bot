import { NextResponse } from "next/server";

import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

export async function GET() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const { data: cfg } = await supabase
    .from("user_config")
    .select("display_name")
    .eq("user_id", user.id)
    .maybeSingle();

  return NextResponse.json({
    email: user.email ?? "",
    displayName: cfg?.display_name ?? "",
  });
}

export async function PUT(req: Request) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const body = await req.json().catch(() => ({}));
  const displayName = typeof body?.displayName === "string" ? body.displayName.trim() : "";
  if (displayName.length < 2) {
    return NextResponse.json({ error: "Name must be at least 2 characters." }, { status: 400 });
  }
  if (displayName.length > 80) {
    return NextResponse.json({ error: "Name is too long." }, { status: 400 });
  }

  const { error } = await supabase
    .from("user_config")
    .upsert(
      { user_id: user.id, display_name: displayName },
      { onConflict: "user_id" },
    );

  if (error) {
    console.error("[api/profile] upsert failed", {
      userId: user.id,
      code: error.code,
      message: error.message,
    });
    return NextResponse.json({ error: "Couldn't save - try again." }, { status: 500 });
  }

  return NextResponse.json({ ok: true, displayName });
}
