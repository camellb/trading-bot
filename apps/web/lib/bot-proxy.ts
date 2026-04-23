import { NextResponse } from "next/server";

import { createClient } from "@/lib/supabase/server";

const DEFAULT_URL = "http://127.0.0.1:8765";

type Method = "GET" | "POST" | "PUT" | "DELETE";

export type BotFetchResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; error: string };

// Resolve the caller's Supabase user ID once per proxy call. The bot API
// reads this from X-User-Id and scopes all user-scoped queries by it -
// summary/positions/evaluations will 401 if the header is missing.
async function _currentUserId(): Promise<string | null> {
  try {
    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();
    return user?.id ?? null;
  } catch {
    return null;
  }
}

async function _rawFetch<T>(
  method: Method,
  path: string,
  search: string,
  body: unknown,
  timeoutMs: number,
): Promise<BotFetchResult<T>> {
  const secret = process.env.BOT_API_SECRET;
  if (!secret) {
    return {
      ok: false,
      status: 500,
      error: "BOT_API_SECRET not configured",
    };
  }

  const hasBody = method === "POST" || method === "PUT";
  const url = `${process.env.BOT_API_URL ?? DEFAULT_URL}${path}${search}`;
  const userId = await _currentUserId();
  try {
    const init: RequestInit = {
      method,
      headers: {
        "X-Bot-Secret": secret,
        ...(userId ? { "X-User-Id": userId } : {}),
        ...(hasBody ? { "Content-Type": "application/json" } : {}),
      },
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    };
    if (hasBody) {
      init.body = JSON.stringify(body ?? {});
    }
    const res = await fetch(url, init);
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const msg =
        (data && typeof data === "object" && "error" in data
          ? String((data as { error: unknown }).error)
          : null) ?? `Bot returned ${res.status}`;
      return { ok: false, status: res.status, error: msg };
    }
    return { ok: true, status: res.status, data: data as T };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, status: 502, error: `Bot unreachable: ${msg}` };
  }
}

/** Server-component / server-action data read. Use when you want the
 *  parsed body, not a NextResponse. */
export function botGet<T>(
  path: string,
  search = "",
  timeoutMs = 15_000,
): Promise<BotFetchResult<T>> {
  return _rawFetch<T>("GET", path, search, undefined, timeoutMs);
}

export function botPost<T>(
  path: string,
  body: unknown,
  timeoutMs = 30_000,
): Promise<BotFetchResult<T>> {
  return _rawFetch<T>("POST", path, "", body, timeoutMs);
}

async function _call(
  method: Method,
  path: string,
  search: string,
  body: unknown,
  timeoutMs: number,
): Promise<NextResponse> {
  const result = await _rawFetch<unknown>(method, path, search, body, timeoutMs);
  if (result.ok) {
    return NextResponse.json(result.data ?? {}, { status: result.status });
  }
  return NextResponse.json({ error: result.error }, { status: result.status });
}

export function proxyGet(
  path: string,
  search = "",
  timeoutMs = 15_000,
): Promise<NextResponse> {
  return _call("GET", path, search, undefined, timeoutMs);
}

export function proxyPost(
  path: string,
  body: unknown,
  timeoutMs = 30_000,
): Promise<NextResponse> {
  return _call("POST", path, "", body, timeoutMs);
}

export function proxyPut(
  path: string,
  body: unknown,
  timeoutMs = 15_000,
): Promise<NextResponse> {
  return _call("PUT", path, "", body, timeoutMs);
}

export function proxyDelete(
  path: string,
  search = "",
  timeoutMs = 15_000,
): Promise<NextResponse> {
  return _call("DELETE", path, search, undefined, timeoutMs);
}
