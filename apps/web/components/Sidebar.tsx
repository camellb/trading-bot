"use client";

import { useState } from "react";

export type NavView = "overview" | "positions" | "scanner" | "analytics" | "risk" | "intelligence" | "settings";

const NAV_ITEMS: { id: NavView; label: string; icon: React.ReactNode }[] = [
  {
    id: "overview",
    label: "Overview",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <rect x="3" y="3" width="7" height="7" rx="1"/>
        <rect x="14" y="3" width="7" height="7" rx="1"/>
        <rect x="3" y="14" width="7" height="7" rx="1"/>
        <rect x="14" y="14" width="7" height="7" rx="1"/>
      </svg>
    ),
  },
  {
    id: "positions",
    label: "Live Positions",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M12 20V10M18 20V4M6 20v-4"/>
      </svg>
    ),
  },
  {
    id: "scanner",
    label: "Market Scanner",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
      </svg>
    ),
  },
  {
    id: "analytics",
    label: "Analytics",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M21 21H4.6c-.56 0-.84 0-1.05-.11a1 1 0 0 1-.44-.44C3 20.24 3 19.96 3 19.4V3"/>
        <path d="m7 14 4-4 4 4 6-6"/>
      </svg>
    ),
  },
  {
    id: "risk",
    label: "Risk & Controls",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
      </svg>
    ),
  },
  {
    id: "intelligence",
    label: "Intelligence",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M12 2a7 7 0 0 0-7 7c0 2.38 1.19 4.47 3 5.74V17a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-2.26c1.81-1.27 3-3.36 3-5.74a7 7 0 0 0-7-7z"/>
        <path d="M10 21h4"/>
      </svg>
    ),
  },
  {
    id: "settings",
    label: "Bot Config",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>
        <circle cx="12" cy="12" r="3"/>
      </svg>
    ),
  },
];

export function Sidebar({
  currentView,
  onNavigate,
  mode,
  open,
  onClose,
}: {
  currentView: NavView;
  onNavigate: (view: NavView) => void;
  mode: string;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <aside
      className={`
        fixed inset-y-0 left-0 z-40 w-[240px] flex flex-col
        bg-[#050505] border-r border-[#1a1a1a]
        transition-transform duration-200
        lg:relative lg:translate-x-0
        ${open ? "translate-x-0" : "-translate-x-full"}
      `}
    >
      {/* Logo */}
      <div className="px-5 py-5 border-b border-[#1a1a1a]">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-[#00ffff]/8 flex items-center justify-center">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-accent">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
            </svg>
          </div>
          <div>
            <div className="text-sm font-semibold text-white leading-none font-headline uppercase tracking-wide">Delfi</div>
            <div className="flex items-center gap-1.5 mt-1">
              <span className={`w-1.5 h-1.5 rounded-full ${mode === "live" ? "bg-red-400" : "bg-accent"}`} />
              <span className="text-[10px] uppercase tracking-widest text-[#666]">
                {mode === "live" ? "live" : "simulation"}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {NAV_ITEMS.map((item) => {
          const active = currentView === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onNavigate(item.id)}
              className={`
                w-full flex items-center gap-3 px-3 py-2.5 text-sm font-body
                transition-colors duration-150
                ${active
                  ? "bg-[#00ffff]/8 text-accent font-medium"
                  : "text-[#a0a0a0] hover:text-white/90 hover:bg-[#0a0a0a]"
                }
              `}
            >
              <span className={active ? "text-accent" : "text-[#666]"}>{item.icon}</span>
              {item.label}
            </button>
          );
        })}
      </nav>

      {/* Emergency Stop */}
      <div className="px-3 pb-4">
        <EmergencyStopButton />
      </div>
    </aside>
  );
}

function EmergencyStopButton() {
  const [confirming, setConfirming] = useState(false);
  const [firing, setFiring] = useState(false);

  const handleClick = async () => {
    if (!confirming) {
      setConfirming(true);
      setTimeout(() => setConfirming(false), 5000);
      return;
    }
    setFiring(true);
    try {
      // This would need a backend endpoint; for now just visual
      setConfirming(false);
    } finally {
      setFiring(false);
    }
  };

  return (
    <button
      onClick={handleClick}
      disabled={firing}
      className={`
        w-full flex items-center justify-center gap-2 px-3 py-2.5 text-sm font-medium font-body
        transition-colors duration-150 disabled:opacity-50
        ${confirming
          ? "bg-red-500 text-white animate-pulse"
          : "bg-danger-dim text-red-400 border border-red-500/20 hover:bg-red-500/20"
        }
      `}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M18 6 6 18M6 6l12 12"/>
      </svg>
      {confirming ? "Confirm Stop?" : "Emergency Stop"}
    </button>
  );
}

