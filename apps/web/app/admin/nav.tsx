"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import "../styles/content.css";

const ADMIN_NAV = [
  { id: "overview", label: "Overview", href: "/admin", match: /^\/admin\/?$/ },
  { id: "users", label: "Users", href: "/admin/users", match: /^\/admin\/users/ },
  { id: "trades", label: "Trades", href: "/admin/trades", match: /^\/admin\/trades/ },
  { id: "forecaster", label: "Forecaster", href: "/admin/forecaster", match: /^\/admin\/forecaster/ },
  { id: "scanner", label: "Scanner", href: "/admin/scanner", match: /^\/admin\/scanner/ },
  { id: "learning", label: "Learning", href: "/admin/learning", match: /^\/admin\/learning/ },
];

export default function AdminNav({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "/admin";
  return (
    <div className="content-page">
      <header className="content-nav">
        <Link href="/admin" className="wordmark">
          <img src="/brand/mark.svg" alt="" />
          <span>DELFI · ADMIN</span>
        </Link>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <span className="pill pill-open">Operator</span>
          <Link href="/dashboard" className="back-link" style={{ marginLeft: 12 }}>← Exit admin</Link>
        </div>
      </header>

      <div className="tab-bar" style={{ padding: "0 32px", margin: 0 }}>
        {ADMIN_NAV.map((n) => {
          const active = n.match.test(pathname);
          return (
            <Link key={n.id} href={n.href} className={`tab ${active ? "on" : ""}`}>
              {n.label}
            </Link>
          );
        })}
      </div>

      {children}
    </div>
  );
}
