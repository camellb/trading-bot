export type ToastKind = "info" | "error";
export type ToastFn = (msg: string, kind?: ToastKind) => void;

export type Side = "YES" | "NO";

export type Recommendation =
  | "BUY_YES"
  | "BUY_NO"
  | "HOLD"
  | "SKIP"
  | "WAIT";

const _fmtUsd = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function usd(
  n: number | null | undefined,
  opts: { sign?: boolean; clampZero?: boolean } = {},
): string {
  if (n == null) return "—";
  if (opts.clampZero && Math.abs(n) < 0.005) return "$0.00";
  const abs = _fmtUsd.format(Math.abs(n));
  if (n < 0) return `-$${abs}`;
  if (opts.sign && n > 0) return `+$${abs}`;
  return `$${abs}`;
}

export function prob(n: number | null | undefined): string {
  if (n == null) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

export function sharePrice(price: number | null | undefined): string {
  if (price == null) return "—";
  return `$${price.toFixed(3)}`;
}

export function pct(n: number | null | undefined, digits = 1): string {
  if (n == null) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

export function timeUntil(iso: string | null): string {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "awaiting result";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${Math.max(m, 1)}m`;
  const h = Math.floor(ms / 3_600_000);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  if (d < 60) return `${d}d`;
  const mo = Math.floor(d / 30);
  return `${mo}mo`;
}

export function formatUptime(startedAt: string | null): string {
  if (!startedAt) return "—";
  const ms = Date.now() - new Date(startedAt).getTime();
  if (ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h >= 24) {
    const d = Math.floor(h / 24);
    return `${d}d ${h % 24}h`;
  }
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export function polymarketUrl(slug: string | null | undefined, marketId?: string, eventSlug?: string | null): string {
  if (eventSlug) return `https://polymarket.com/event/${eventSlug}`;
  if (slug) return `https://polymarket.com/event/${slug}`;
  return `https://gamma-api.polymarket.com/markets/${marketId ?? ""}`;
}

export function pnlColorClass(n: number | null | undefined): string {
  if (n == null || n === 0) return "text-slate-400";
  return n > 0 ? "text-accent" : "text-red-400";
}

export function formatRecommendation(rec: Recommendation): string {
  return rec.replaceAll("_", " ");
}

export function humanizeIdentifier(raw: string): string {
  const tokenMap: Record<string, string> = {
    pnl: "P&L",
    usd: "USD",
    pct: "%",
    bps: "bps",
    api: "API",
    id: "ID",
    shadow: "Simulation",
  };

  // Strip leading "PM_" prefix — it adds no information in the UI.
  const stripped = raw.replace(/^PM_/, "");

  return stripped
    .split("_")
    .filter(Boolean)
    .map((part) => {
      const lower = part.toLowerCase();
      if (tokenMap[lower]) return tokenMap[lower];
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    })
    .join(" ");
}

export function sideColorClass(side: string | null | undefined): string {
  if (side === "YES") return "text-accent";
  if (side === "NO")  return "text-orange-400";
  return "text-slate-500";
}

export function recommendationColorClass(rec: Recommendation): string {
  if (rec === "BUY_YES") return "text-accent";
  if (rec === "BUY_NO")  return "text-orange-400";
  return "text-slate-500";
}
