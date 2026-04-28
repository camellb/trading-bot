import type { Metadata } from "next";
import "./globals.css";

// Root layout. Wraps every page in the marketing site. The site is
// dark-themed by default (matches the desktop app's palette).

export const metadata: Metadata = {
  title: "Delfi. Autonomous Polymarket trader",
  description:
    "Delfi watches Polymarket, backs the side the market itself favours on every tradeable contract, and steps aside whenever its own forecast disagrees. Runs on your machine.",
  metadataBase: new URL("https://delfibot.com"),
  openGraph: {
    title: "Delfi. Autonomous Polymarket trader",
    description:
      "Follow the market. Use the forecast as a filter. A local desktop app that trades Polymarket while you sleep.",
    type: "website",
  },
  robots: { index: true, follow: true },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-[#0a0f1f] text-slate-100 antialiased">
        {children}
      </body>
    </html>
  );
}
