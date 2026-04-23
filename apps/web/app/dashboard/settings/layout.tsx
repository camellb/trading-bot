"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import "../../styles/content.css";

const TABS = [
  { id: "account", label: "Account", href: "/dashboard/settings/account" },
  { id: "risk", label: "Risk controls", href: "/dashboard/settings/risk" },
  { id: "notifications", label: "Notifications", href: "/dashboard/settings/notifications" },
];

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "";
  return (
    <div className="page-wrap">
      <div className="page-head">
        <div className="page-head-row">
          <div>
            <h1 className="page-h1">Settings</h1>
            <p className="page-sub">Manage your account, risk controls, and notification preferences.</p>
          </div>
        </div>
      </div>

      <div className="tab-bar">
        {TABS.map((t) => {
          const active = pathname.startsWith(t.href);
          return (
            <Link key={t.id} href={t.href} className={`tab ${active ? "on" : ""}`}>
              {t.label}
            </Link>
          );
        })}
      </div>

      {children}
    </div>
  );
}
