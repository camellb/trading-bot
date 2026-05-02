/**
 * Centralised date / number formatters.
 *
 * Why this file exists: every page used to roll its own
 * `fmtDate` / `fmtDateTime` with hardcoded locale strings and no
 * timezone awareness. The user complained that timestamps were
 * either UTC (off by N hours) or unconfigurable. Now all formatters
 * live here and respect a single user preference for display
 * timezone.
 *
 * The timezone preference is purely a UI concern (which clock the
 * user wants to read in), so we keep it in localStorage rather than
 * the bot's database. Survives across GUI launches, no backend
 * round-trip, no DB migration.
 *
 * Default behaviour: when no preference is set, dates render in the
 * user's system timezone via Intl.DateTimeFormat with no `timeZone`
 * option (which lets the runtime pick). This matches what the user
 * expects "by default" - the OS clock.
 */

const STORAGE_KEY = "delfi.display_tz";

/**
 * Module-level cache of the user's chosen tz. null / empty means
 * "use system default". Updated by `setDisplayTz` and on first load
 * via `loadFromStorage`.
 */
let currentTz: string | null = null;
let loaded = false;

function loadFromStorage(): string | null {
  if (loaded) return currentTz;
  loaded = true;
  try {
    const v = typeof window !== "undefined" && window.localStorage
      ? window.localStorage.getItem(STORAGE_KEY)
      : null;
    currentTz = v && v.trim() ? v.trim() : null;
  } catch {
    currentTz = null;
  }
  return currentTz;
}

/** Return the current display timezone, or null for system default. */
export function getDisplayTz(): string | null {
  return loadFromStorage();
}

/**
 * Update the display timezone preference.
 *
 * Pass null or "" to fall back to the system default. The new value
 * is persisted to localStorage and applied to all future formatter
 * calls. Live components don't auto-refresh - the page's next React
 * render will pick it up. In practice we wire this through a React
 * state setter (see useDisplayTz) so subscribed components do
 * re-render.
 */
export function setDisplayTz(tz: string | null): void {
  loaded = true;
  const clean = tz && tz.trim() ? tz.trim() : null;
  currentTz = clean;
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      if (clean) window.localStorage.setItem(STORAGE_KEY, clean);
      else window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // Quota / private mode etc. Module-level cache still applies
    // for the lifetime of the page.
  }
}

function withTz(
  opts: Intl.DateTimeFormatOptions,
): Intl.DateTimeFormatOptions {
  const tz = loadFromStorage();
  return tz ? { ...opts, timeZone: tz } : opts;
}

/** "May 3" / "May 3, 2026" depending on whether the year matches now. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const now = new Date();
  const opts: Intl.DateTimeFormatOptions = d.getFullYear() === now.getFullYear()
    ? { month: "short", day: "numeric" }
    : { year: "numeric", month: "short", day: "numeric" };
  try {
    return new Intl.DateTimeFormat(undefined, withTz(opts)).format(d);
  } catch {
    return d.toDateString();
  }
}

/** "May 3, 14:32" / "May 3, 2026, 14:32". */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const now = new Date();
  const opts: Intl.DateTimeFormatOptions = d.getFullYear() === now.getFullYear()
    ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
    : {
        year: "numeric", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      };
  try {
    return new Intl.DateTimeFormat(undefined, withTz(opts)).format(d);
  } catch {
    return d.toString();
  }
}

/** "1d 6h" / "12h 4m" / "-3d 2h" - approximate distance from now. */
export function daysFromNow(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const ms = d.getTime() - Date.now();
  const sign = ms < 0 ? "-" : "";
  const abs = Math.abs(ms);
  const days = Math.floor(abs / 86_400_000);
  const hours = Math.floor((abs % 86_400_000) / 3_600_000);
  if (days > 0) return `${sign}${days}d ${hours}h`;
  const minutes = Math.floor((abs % 3_600_000) / 60_000);
  return `${sign}${hours}h ${minutes}m`;
}

/**
 * Get the user's resolved timezone, even when the preference is
 * "system default". Useful for the Settings dropdown so we can
 * display "System default (Europe/Warsaw)" instead of just
 * "System default".
 */
export function resolvedTz(): string {
  const pref = loadFromStorage();
  if (pref) return pref;
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/**
 * A small curated list of common timezone IDs for the Settings
 * dropdown. We don't try to expose the full IANA tz database (~400+
 * zones) because that's a bad UX for a single dropdown. Users with
 * exotic tzs can leave it on system default; the OS clock is
 * authoritative.
 */
export const COMMON_TIMEZONES: { value: string; label: string }[] = [
  { value: "UTC",                 label: "UTC" },
  { value: "Europe/London",       label: "London" },
  { value: "Europe/Warsaw",       label: "Warsaw" },
  { value: "Europe/Berlin",       label: "Berlin" },
  { value: "Europe/Paris",        label: "Paris" },
  { value: "Europe/Madrid",       label: "Madrid" },
  { value: "Europe/Athens",       label: "Athens" },
  { value: "Europe/Moscow",       label: "Moscow" },
  { value: "America/New_York",    label: "New York" },
  { value: "America/Chicago",     label: "Chicago" },
  { value: "America/Denver",      label: "Denver" },
  { value: "America/Los_Angeles", label: "Los Angeles" },
  { value: "America/Toronto",     label: "Toronto" },
  { value: "America/Sao_Paulo",   label: "São Paulo" },
  { value: "Asia/Tokyo",          label: "Tokyo" },
  { value: "Asia/Shanghai",       label: "Shanghai" },
  { value: "Asia/Singapore",      label: "Singapore" },
  { value: "Asia/Hong_Kong",      label: "Hong Kong" },
  { value: "Asia/Kolkata",        label: "Mumbai / Delhi" },
  { value: "Asia/Dubai",          label: "Dubai" },
  { value: "Australia/Sydney",    label: "Sydney" },
];
