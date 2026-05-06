import type { Metadata } from "next";
import { headers } from "next/headers";
import { ConsentGate } from "./components/ConsentGate";
import { CookieBanner } from "./components/CookieBanner";
import { consentRequiredForCountry } from "@/lib/regions";
import "./globals.css";

export const metadata: Metadata = {
  title: "Delfi - The future is no longer a guess",
  description: "The first autonomous, self-improving forecasting AI agent for Polymarket.",
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
