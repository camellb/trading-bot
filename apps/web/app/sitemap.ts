// Next.js App Router sitemap convention. The framework detects this
// file, calls the default export at build time, and writes the result
// to /sitemap.xml. Linked from /robots.txt so crawlers find it.
//
// Keep this list short and curated. Listing every dynamic URL would
// bloat the sitemap; LLM and search crawlers fan out from the homepage
// + named anchors below anyway.

import type { MetadataRoute } from "next";

export default function sitemap(): MetadataRoute.Sitemap {
  const base = "https://delfibot.com";
  // lastModified bumps every deploy. That's enough signal for crawlers
  // to re-check; granular per-section dates are not needed.
  const now = new Date();
  return [
    { url: `${base}/`,              lastModified: now, changeFrequency: "weekly", priority: 1.0 },
    { url: `${base}/legal/terms`,   lastModified: now, changeFrequency: "yearly", priority: 0.3 },
    { url: `${base}/legal/privacy`, lastModified: now, changeFrequency: "yearly", priority: 0.3 },
    { url: `${base}/legal/risk`,    lastModified: now, changeFrequency: "yearly", priority: 0.3 },
    { url: `${base}/legal/cookies`, lastModified: now, changeFrequency: "yearly", priority: 0.3 },
  ];
}
