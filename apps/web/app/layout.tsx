import type { Metadata } from "next";
import { headers } from "next/headers";
import { ConsentGate } from "./components/ConsentGate";
import { CookieBanner } from "./components/CookieBanner";
import { consentRequiredForCountry } from "@/lib/regions";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://delfibot.com"),
  title: {
    default: "Delfi - Autonomous Polymarket Trading Bot",
    template: "%s - Delfi",
  },
  description:
    "Autonomous trading bot for Polymarket. Trades 24/7 on your computer. Non-custodial: keys never leave your machine. $249 one-time payment, lifetime updates. macOS and Windows.",
  keywords: [
    "polymarket bot",
    "polymarket trading bot",
    "polymarket automation",
    "polymarket automated trading",
    "prediction market bot",
    "prediction market trading",
    "non-custodial trading bot",
    "polymarket strategy",
    "polymarket arbitrage",
    "automated prediction markets",
  ],
  authors: [{ name: "Delfi" }],
  creator: "Delfi",
  publisher: "Delfi",
  category: "finance",
  alternates: {
    canonical: "/",
  },
  openGraph: {
    type: "website",
    url: "https://delfibot.com/",
    siteName: "Delfi",
    title: "Delfi - Autonomous Polymarket Trading Bot",
    description:
      "Trades Polymarket 24/7 on your computer. Non-custodial. $249 one-time payment with lifetime updates. macOS and Windows.",
    images: [
      {
        url: "/brand/oracle-hero.jpg",
        width: 1200,
        height: 630,
        alt: "Delfi - Autonomous Polymarket Trading Bot",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Delfi - Autonomous Polymarket Trading Bot",
    description:
      "Trades Polymarket 24/7. Non-custodial. $249 one-time payment with lifetime updates.",
    images: ["/brand/oracle-hero.jpg"],
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      "max-snippet": -1,
      "max-image-preview": "large",
      "max-video-preview": -1,
    },
  },
  icons: {
    icon: [
      { url: "/brand/mark.svg", type: "image/svg+xml" },
    ],
    shortcut: "/brand/mark.svg",
    apple: "/brand/mark.svg",
  },
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Vercel sets `x-vercel-ip-country` (ISO 3166-1 alpha-2) at the
  // edge. When absent (local dev, custom proxies) we treat the
  // visitor as in scope for consent so the banner still appears.
  const h = await headers();
  const country = h.get("x-vercel-ip-country");
  const consentRequired = consentRequiredForCountry(country);

  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        <link
          href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,200..800;1,6..72,200..800&family=Geist:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        {children}
        <CookieBanner consentRequired={consentRequired} />
        <ConsentGate consentRequired={consentRequired} />
      </body>
    </html>
  );
}
