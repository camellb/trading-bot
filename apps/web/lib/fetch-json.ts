import { readViewModeForFetch } from "./view-mode";

// Appends the current view mode to every dashboard API call as a query
// string parameter. The Next.js /api/* route forwards it to the bot so
// queries can be scoped by mode (simulation vs live). Admin routes
// ignore the param on the server side - see the admin handlers in
// bot_api.py, which force mode='live' regardless of X-View-Mode.
function withViewMode(path: string): string {
  const mode = readViewModeForFetch();
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}view_mode=${encodeURIComponent(mode)}`;
}

export async function getJSON<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(withViewMode(path), { cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}
