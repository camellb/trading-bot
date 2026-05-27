// apps/web/app/api/download/[platform]/route.ts
//
// Public download endpoint for the desktop app installers. Resolves
// `mac` -> the latest macOS .dmg, `win` -> the latest Windows
// setup.exe, and STREAMS the binary back through Vercel so the
// end-user URL bar never reveals GitHub. The post-purchase email
// and the checkout success page both link to /api/download/{mac,win}
// instead of github.com/...
//
// User rule (2026-05-27): "NO ONE SHOULD EVER SEE THE GITHUB. NO
// USER. EVER." The proxy is the only correct fix - a 302 redirect
// would still surface the github.com host in the browser's network
// tab / address bar mid-redirect.
//
// Trade-off: every download flows ~120 MB through a Vercel function.
// Vercel Pro tier has plenty of bandwidth headroom for early
// volume. If this becomes the dominant egress line item, swap to
// a CDN (R2 / Bunny / S3 + custom subdomain) and have this route
// either proxy from that or 302 to it (still non-github).

import { NextResponse } from "next/server";

// Override Vercel's default 10s edge / serverless timeout for this
// route. 60s is the Pro-tier maximum for Node serverless functions.
// A 120 MB download at ~3 MB/s = 40s, fits comfortably; users on
// slow connections may still time out but get to retry.
export const maxDuration = 60;

// Force Node runtime (not Edge) so the streaming response body
// passthrough works with Vercel's full bandwidth allowance. Edge
// has a tighter response-size cap that can choke on multi-100MB
// binaries.
export const runtime = "nodejs";

type Platform = "mac" | "win";

interface GitHubAsset {
  name: string;
  browser_download_url: string;
  size: number;
  content_type: string | null;
}

interface GitHubRelease {
  tag_name: string;
  assets: GitHubAsset[];
}

const GITHUB_REPO = "camellb/trading-bot";
const REVALIDATE_SECONDS = 300; // re-query GH at most once per 5 min

// Map platform -> (asset matcher, download filename the browser
// sees). The matcher returns the asset whose `name` satisfies the
// platform's signature (.dmg for mac, -setup.exe for windows).
const PLATFORM_RULES: Record<
  Platform,
  { match: (name: string) => boolean; downloadAs: string }
> = {
  mac: {
    match: (name) => name.endsWith(".dmg"),
    downloadAs: "Delfi.dmg",
  },
  win: {
    match: (name) =>
      name.endsWith("-setup.exe") || name.endsWith(".msi"),
    downloadAs: "Delfi-Setup.exe",
  },
};

async function resolveAssetUrl(platform: Platform): Promise<string | null> {
  // Next.js fetch with `next.revalidate` caches the response in the
  // platform CDN, so 1000 concurrent downloads still only hit
  // GitHub's API once per REVALIDATE_SECONDS window. Avoids
  // tripping the unauthenticated 60-req/hr/IP rate limit during
  // launch spikes.
  const ghToken = process.env.GITHUB_TOKEN;
  const res = await fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/releases/latest`,
    {
      headers: {
        Accept: "application/vnd.github+json",
        "User-Agent": "delfibot-download-proxy",
        ...(ghToken ? { Authorization: `Bearer ${ghToken}` } : {}),
      },
      next: { revalidate: REVALIDATE_SECONDS },
    },
  );
  if (!res.ok) {
    console.error(
      `[download] GitHub API failed: ${res.status} ${res.statusText}`,
    );
    return null;
  }
  const release = (await res.json()) as GitHubRelease;
  const rule = PLATFORM_RULES[platform];
  const asset = (release.assets || []).find((a) => rule.match(a.name));
  if (!asset) {
    console.error(
      `[download] no ${platform} asset in release ${release.tag_name}`,
    );
    return null;
  }
  return asset.browser_download_url;
}

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ platform: string }> },
): Promise<Response> {
  const { platform: rawPlatform } = await ctx.params;
  const platform = rawPlatform.toLowerCase() as Platform;
  if (!(platform in PLATFORM_RULES)) {
    return NextResponse.json(
      { error: `unknown platform '${rawPlatform}'; valid: mac, win` },
      { status: 400 },
    );
  }

  const upstreamUrl = await resolveAssetUrl(platform);
  if (!upstreamUrl) {
    return NextResponse.json(
      { error: "no installer published yet for this platform" },
      { status: 503 },
    );
  }

  // Stream the binary back without buffering it in memory. The
  // upstream.body is a ReadableStream; Next.js Response forwards it
  // chunk-by-chunk to the client.
  const upstream = await fetch(upstreamUrl);
  if (!upstream.ok || !upstream.body) {
    console.error(
      `[download] upstream fetch failed: ${upstream.status} ${upstream.statusText}`,
    );
    return NextResponse.json(
      { error: "installer download failed - please retry" },
      { status: 502 },
    );
  }

  const rule = PLATFORM_RULES[platform];
  const headers = new Headers();
  headers.set(
    "Content-Type",
    upstream.headers.get("content-type") || "application/octet-stream",
  );
  headers.set(
    "Content-Disposition",
    `attachment; filename="${rule.downloadAs}"`,
  );
  const len = upstream.headers.get("content-length");
  if (len) headers.set("Content-Length", len);
  // No-store: each install version is unique; we don't want
  // browsers caching a stale .dmg after a release.
  headers.set("Cache-Control", "no-store");

  return new Response(upstream.body, { status: 200, headers });
}
