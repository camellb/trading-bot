import { NextResponse, type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";
import { invalidateCache } from "@/lib/geoblock/check";

export const dynamic = "force-dynamic";

type RuleInput = {
  country_code?: unknown;
  subdivision_code?: unknown;
  reason?: unknown;
};

function clean(value: unknown, max: number): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  return trimmed.slice(0, max);
}

async function requireAdmin() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return {
      supabase,
      user: null,
      ok: false as const,
      status: 401,
      error: "not_authenticated",
    };
  }
  const { data: cfg } = await supabase
    .from("user_config")
    .select("is_admin")
    .eq("user_id", user.id)
    .maybeSingle();
  if (!cfg?.is_admin) {
    return {
      supabase,
      user,
      ok: false as const,
      status: 403,
      error: "forbidden",
    };
  }
  return { supabase, user, ok: true as const };
}

// GET /api/admin/geoblock  -> list every rule.
export async function GET() {
  const gate = await requireAdmin();
  if (!gate.ok) {
    return NextResponse.json({ error: gate.error }, { status: gate.status });
  }
  const { data, error } = await gate.supabase
    .from("geoblock_rules")
    .select("id, country_code, subdivision_code, reason, created_at, created_by")
    .order("country_code", { ascending: true })
    .order("subdivision_code", { ascending: true, nullsFirst: true });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  return NextResponse.json({ rules: data ?? [] });
}

// POST /api/admin/geoblock  -> add a rule.
// Body: { country_code: "US", subdivision_code?: "CA-ON" | "ON", reason?: "..." }
export async function POST(req: NextRequest) {
  const gate = await requireAdmin();
  if (!gate.ok) {
    return NextResponse.json({ error: gate.error }, { status: gate.status });
  }
  let body: RuleInput;
  try {
    body = (await req.json()) as RuleInput;
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }

  const rawCountry = clean(body.country_code, 2);
  if (!rawCountry || !/^[A-Za-z]{2}$/.test(rawCountry)) {
    return NextResponse.json(
      { error: "country_code must be ISO 3166-1 alpha-2 (two letters)" },
      { status: 400 },
    );
  }
  const country_code = rawCountry.toUpperCase();

  const rawSub = clean(body.subdivision_code, 10);
  let subdivision_code: string | null = null;
  if (rawSub) {
    // Accept "CA-ON" or bare "ON". Strip the country prefix.
    const upper = rawSub.toUpperCase();
    const candidate = upper.startsWith(`${country_code}-`)
      ? upper.slice(country_code.length + 1)
      : upper;
    if (!/^[A-Z0-9]{1,10}$/.test(candidate)) {
      return NextResponse.json(
        { error: "subdivision_code must be an ISO 3166-2 suffix" },
        { status: 400 },
      );
    }
    subdivision_code = candidate;
  }

  const reason = clean(body.reason, 500);

  const { data, error } = await gate.supabase
    .from("geoblock_rules")
    .insert({
      country_code,
      subdivision_code,
      reason,
      created_by: gate.user.id,
    })
    .select("id, country_code, subdivision_code, reason, created_at, created_by")
    .single();

  if (error) {
    // 23505 = unique_violation. Surface as a friendly message.
    if (error.code === "23505") {
      return NextResponse.json({ error: "already_blocked" }, { status: 409 });
    }
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  invalidateCache();
  return NextResponse.json({ rule: data });
}

// DELETE /api/admin/geoblock?id=123  -> remove a rule by primary key.
export async function DELETE(req: NextRequest) {
  const gate = await requireAdmin();
  if (!gate.ok) {
    return NextResponse.json({ error: gate.error }, { status: gate.status });
  }
  const idParam = new URL(req.url).searchParams.get("id");
  const id = idParam ? Number.parseInt(idParam, 10) : NaN;
  if (!Number.isFinite(id) || id <= 0) {
    return NextResponse.json({ error: "invalid_id" }, { status: 400 });
  }
  const { error } = await gate.supabase
    .from("geoblock_rules")
    .delete()
    .eq("id", id);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  invalidateCache();
  return NextResponse.json({ ok: true });
}
