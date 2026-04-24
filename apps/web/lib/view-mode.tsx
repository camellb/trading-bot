"use client";

// Per-user view mode. Clients flip between "simulation" and "live" to
// see their stats scoped to either set of positions. The mode is
// forwarded to the bot via the X-View-Mode header so pm_positions and
// market_evaluations queries can be scoped. It does NOT change what
// the bot trades as - that's still driven by user_config.mode.
//
// Persisted in localStorage so a reload keeps the selection. Default
// is "simulation" - new users start there and the bot can't open live
// positions until credentials are wired anyway.

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type ViewMode = "simulation" | "live";

const STORAGE_KEY = "delfi.view_mode";
const DEFAULT_MODE: ViewMode = "simulation";

function readStorage(): ViewMode {
  if (typeof window === "undefined") return DEFAULT_MODE;
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v === "live" ? "live" : "simulation";
  } catch {
    return DEFAULT_MODE;
  }
}

type ViewModeContextValue = {
  mode: ViewMode;
  setMode: (next: ViewMode) => void;
  // Bumps every time the mode changes. Components pass this into
  // their useEffect deps to re-fetch when the user flips the switch.
  version: number;
};

const ViewModeContext = createContext<ViewModeContextValue | null>(null);

export function ViewModeProvider({ children }: { children: React.ReactNode }) {
  // Start with DEFAULT_MODE for SSR so the server and the first client
  // render agree. After hydration, pull the real value from localStorage.
  const [mode, setModeState] = useState<ViewMode>(DEFAULT_MODE);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const stored = readStorage();
    if (stored !== mode) {
      setModeState(stored);
      setVersion((v) => v + 1);
    }
    // Intentionally empty deps - run exactly once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setMode = useCallback((next: ViewMode) => {
    if (next !== "simulation" && next !== "live") return;
    try {
      if (typeof window !== "undefined") {
        window.localStorage.setItem(STORAGE_KEY, next);
      }
    } catch {
      // localStorage unavailable (private mode, quota, etc.) - the
      // tab-local state still updates so the user sees the switch take
      // effect. The only cost is that a reload forgets the choice.
    }
    setModeState(next);
    setVersion((v) => v + 1);
  }, []);

  const value = useMemo<ViewModeContextValue>(
    () => ({ mode, setMode, version }),
    [mode, setMode, version],
  );

  return (
    <ViewModeContext.Provider value={value}>{children}</ViewModeContext.Provider>
  );
}

export function useViewMode(): ViewModeContextValue {
  const ctx = useContext(ViewModeContext);
  if (!ctx) {
    // Fallback for components rendered outside the provider (shouldn't
    // happen in practice - the provider wraps the whole dashboard - but
    // we return a sane default rather than throw so an accidental
    // render doesn't blank the page).
    return {
      mode: DEFAULT_MODE,
      setMode: () => undefined,
      version: 0,
    };
  }
  return ctx;
}

// Synchronous read used by fetch helpers that run outside the React
// tree (e.g. getJSON called from a useEffect body). Safe on the server
// (returns DEFAULT_MODE) and on the client (reads localStorage).
export function readViewModeForFetch(): ViewMode {
  return readStorage();
}
