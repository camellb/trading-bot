import { NextResponse } from "next/server";

const DEFAULT_URL = "http://127.0.0.1:8765";

async function _call(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  search: string,
  body: unknown,
  timeoutMs: number,
): Promise<NextResponse> {
  const secret = process.env.BOT_API_SECRET;
  if (!secret) {
    return NextResponse.json(
      { error: "BOT_API_SECRET not configured in dashboard/.env.local" },
      { status: 500 },
    );
  }

  const hasBody = method === "POST" || method === "PUT";
  const url = `${process.env.BOT_API_URL ?? DEFAULT_URL}${path}${search}`;
  try {
    const init: RequestInit = {
      method,
      headers: {
        "X-Bot-Secret": secret,
        ...(hasBody ? { "Content-Type": "application/json" } : {}),
      },
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    };
    if (hasBody) {
      init.body = JSON.stringify(body ?? {});
    }
    const res  = await fetch(url, init);
    const data = await res.json().catch(() => ({ error: "invalid JSON from bot" }));
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: `Bot unreachable: ${msg}` },
      { status: 502 },
    );
  }
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
