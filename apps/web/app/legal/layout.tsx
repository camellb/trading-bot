import Link from "next/link";
import "../styles/content.css";

export default function LegalLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="content-page">
      <header className="content-nav">
        <Link href="/" className="wordmark">
          <img src="/brand/mark.svg" alt="" />
          <span>DELFI</span>
        </Link>
        <Link href="/" className="back-link">← Back to home</Link>
      </header>

      {children}

      <footer className="content-footer">
        <div>© 2026 Delfi. All rights reserved.</div>
        <div className="content-footer-links">
          <Link href="/legal/terms">Terms</Link>
          <Link href="/legal/privacy">Privacy</Link>
          <Link href="/legal/risk">Risk</Link>
        </div>
      </footer>
    </div>
  );
}
