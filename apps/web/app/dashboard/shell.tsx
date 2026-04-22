"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCredentials } from "../../lib/credentials";
import { signOut } from "../auth/actions";

export type IconKey =
  | "grid"
  | "layers"
  | "trend"
  | "list"
  | "shield"
  | "gear"
  | "life"
  | "pause"
  | "bolt"
  | "arrow"
  | "plus"
  | "book";

type NavItem = {
  id: string;
  label: string;
  icon: IconKey;
  href: string;
  match?: RegExp;
  sub?: { id: string; label: string; href: string }[];
};

const NAV: NavItem[] = [
  { id: "dashboard", label: "Dashboard", icon: "grid", href: "/dashboard", match: /^\/dashboard\/?$/ },
  { id: "positions", label: "Positions", icon: "layers", href: "/dashboard/positions", match: /^\/dashboard\/positions/ },
  { id: "performance", label: "Performance", icon: "trend", href: "/dashboard/performance", match: /^\/dashboard\/performance/ },
  { id: "activity", label: "Activity log", icon: "list", href: "/dashboard/activity", match: /^\/dashboard\/activity/ },
  { id: "risk", label: "Risk controls", icon: "shield", href: "/dashboard/risk", match: /^\/dashboard\/risk/ },
  {
    id: "settings",
    label: "Settings",
    icon: "gear",
    href: "/dashboard/settings/account",
    match: /^\/dashboard\/settings/,
    sub: [
      { id: "account", label: "Account", href: "/dashboard/settings/account" },
      { id: "notifications", label: "Notifications", href: "/dashboard/settings/notifications" },
    ],
  },
  { id: "support", label: "Support", icon: "life", href: "/dashboard/support", match: /^\/dashboard\/support/ },
];

export const ICON: Record<IconKey, React.ReactNode> = {
  grid: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
    </svg>
  ),
  layers: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M12 3 3 8l9 5 9-5-9-5z" />
      <path d="M3 13l9 5 9-5" />
      <path d="M3 18l9 5 9-5" />
    </svg>
  ),
  trend: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 17l6-6 4 4 8-10" />
      <path d="M14 5h7v7" />
    </svg>
  ),
  list: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M8 6h13M8 12h13M8 18h13" />
      <circle cx="4" cy="6" r="1.2" />
      <circle cx="4" cy="12" r="1.2" />
      <circle cx="4" cy="18" r="1.2" />
    </svg>
  ),
  shield: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M12 3 4 6v6c0 4.5 3.2 8.3 8 9 4.8-.7 8-4.5 8-9V6l-8-3z" />
    </svg>
  ),
  gear: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.36.15.68.4.9.74" />
    </svg>
  ),
  life: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="3.5" />
      <path d="M5 5l4.3 4.3M14.7 14.7 19 19M5 19l4.3-4.3M14.7 9.3 19 5" />
    </svg>
  ),
  pause: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="6" y="5" width="4" height="14" />
      <rect x="14" y="5" width="4" height="14" />
    </svg>
  ),
  bolt: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />
    </svg>
  ),
  arrow: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  ),
  plus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  book: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M4 5v14a2 2 0 0 0 2 2h14V5a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2z" />
      <path d="M8 7h8M8 11h8M8 15h5" />
    </svg>
  ),
};

export type Mode = "simulation" | "live";

export type DashboardUser = { name: string; email: string; initials: string };

export function DashboardShell({
  children,
  user,
}: {
  children: React.ReactNode;
  user: DashboardUser;
}) {
  const [mode, setMode] = useState<Mode>("simulation");
  const pathname = usePathname() || "/dashboard";
  const { missing, canGoLive, hydrated } = useCredentials();

  useEffect(() => {
    document.body.classList.add("app");
    return () => {
      document.body.classList.remove("app");
    };
  }, []);

  const activeId = NAV.find((n) => n.match?.test(pathname))?.id ?? "dashboard";
  const showCredsBanner = hydrated && !canGoLive && mode === "simulation";

  const trySwitch = (next: Mode) => {
    if (next === "live" && !canGoLive) return;
    setMode(next);
  };

  return (
    <div className="app-shell density-roomy" data-screen-label="Dashboard">
      <Sidebar activeId={activeId} user={user} mode={mode} pathname={pathname} />
      <main className="app-main">
        <ModeBanner mode={mode} onSwitch={trySwitch} canGoLive={canGoLive} missing={missing} />
        {showCredsBanner && <CredentialsBanner missing={missing} />}
        {children}
      </main>
    </div>
  );
}

function CredentialsBanner({ missing }: { missing: string[] }) {
  return (
    <div className="creds-banner" role="status">
      <div className="creds-banner-body">
        <span className="creds-banner-dot" aria-hidden="true"></span>
        <div>
          <div className="creds-banner-title">Live trading is locked</div>
          <div className="creds-banner-text">
            Add the missing credentials before switching modes — {missing.join(", ")}. Until then,
            Delfi will keep running in Simulation with paper capital.
          </div>
        </div>
      </div>
      <Link href="/dashboard/settings/account" className="creds-banner-cta">
        Add credentials →
      </Link>
    </div>
  );
}

function Sidebar({
  activeId,
  user,
  mode,
  pathname,
}: {
  activeId: string;
  user: DashboardUser;
  mode: Mode;
  pathname: string;
}) {
  return (
    <aside className="side">
      <Link href="/" className="side-brand">
        <img src="/brand/mark.svg" alt="" className="side-mark" />
        <span className="side-word">DELFI</span>
      </Link>

      <nav className="side-nav">
        {NAV.map((item) => {
          const isActive = item.id === activeId;
          return (
            <div className="side-group" key={item.id}>
              <Link
                href={item.href}
                className={`side-link ${isActive ? "active" : ""}`}
                aria-current={isActive ? "page" : undefined}
              >
                <span className="side-icon">{ICON[item.icon]}</span>
                <span className="side-label">{item.label}</span>
              </Link>
              {isActive && item.sub && (
                <div className="side-sub">
                  {item.sub.map((s) => (
                    <Link
                      className={`side-sublink ${pathname === s.href ? "active" : ""}`}
                      href={s.href}
                      key={s.id}
                    >
                      {s.label}
                    </Link>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </nav>

      <div className="side-foot">
        <div className={`side-agent ${mode}`}>
          <span className="side-agent-dot"></span>
          <div className="side-agent-body">
            <div className="side-agent-label">Delfi is</div>
            <div className="side-agent-state">{mode === "live" ? "trading live" : "in simulation"}</div>
          </div>
        </div>
        <Link className="side-user" href="/dashboard/settings/account">
          <span className="side-avatar" aria-hidden="true">
            {user.initials}
          </span>
          <span className="side-user-body">
            <span className="side-user-name">{user.name}</span>
            <span className="side-user-mail">{user.email}</span>
          </span>
        </Link>
        <form action={signOut} className="side-signout-form">
          <button type="submit" className="side-signout">Sign out</button>
        </form>
      </div>
    </aside>
  );
}

function ModeBanner({
  mode,
  onSwitch,
  canGoLive,
  missing,
}: {
  mode: Mode;
  onSwitch: (m: Mode) => void;
  canGoLive: boolean;
  missing: string[];
}) {
  const isLive = mode === "live";
  const lockedLabel =
    missing.length === 0
      ? "Go Live →"
      : `Go Live locked — add ${missing[0]}${missing.length > 1 ? ` +${missing.length - 1} more` : ""}`;
  return (
    <div className={`mode-banner ${isLive ? "live" : "sim"}`}>
      <div className="mode-banner-left">
        <span className="mode-banner-pill">
          <span className="mode-banner-dot"></span>
          {isLive ? "LIVE MODE" : "SIMULATION MODE"}
        </span>
        <span className="mode-banner-text">
          {isLive
            ? "You're trading with real capital from your connected wallet. Every position is real."
            : "You're in Simulation. Same signals, same decisions, paper capital. Nothing here risks real money."}
        </span>
      </div>
      <div className="mode-banner-right">
        {isLive ? (
          <button className="mode-banner-btn" onClick={() => onSwitch("simulation")}>
            Switch to Simulation
          </button>
        ) : canGoLive ? (
          <button className="mode-banner-btn gold" onClick={() => onSwitch("live")}>
            Go Live →
          </button>
        ) : (
          <Link
            href="/dashboard/settings/account"
            className="mode-banner-btn locked"
            aria-disabled="true"
            title={`Missing: ${missing.join(", ")}`}
          >
            {lockedLabel}
          </Link>
        )}
      </div>
    </div>
  );
}
