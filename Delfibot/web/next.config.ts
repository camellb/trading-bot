import type { NextConfig } from "next";

// Marketing site config. Static-leaning; the only dynamic surface is the
// license-verify route at /api/license/verify. We keep this lean so the
// Vercel build is fast and the site has no server-side dependencies
// beyond a single route handler.
const nextConfig: NextConfig = {
  reactStrictMode: true,

  // Send a few hardening headers on every response. The desktop app does
  // not embed the marketing site, so framing is never legitimate.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
