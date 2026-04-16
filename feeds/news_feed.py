"""
News Feed — multi-source RSS + Nitter + CryptoPanic aggregator with Gemini pre-filtering.

TWO-MODEL ARCHITECTURE:
  Gemini Flash (config.GEMINI_MODEL): fetches RSS + Nitter + CryptoPanic, filters and
  summarises raw content. Near-zero cost on free tier.

  Claude Sonnet (config.CLAUDE_MODEL): receives cleaned headlines and
  scores severity 1–10. Logic lives in engine/event_overlay.py unchanged.

Sources:
  RSS: 8 crypto + 6 macro/finance  (see config.RSS_FEEDS)
  Nitter: 12 crypto + 11 macro/finance (see config.NITTER_ACCOUNTS)
  CryptoPanic: crypto-specific aggregator via REST API (CRYPTOPANIC_API_KEY)

Nitter failures are expected and handled gracefully — many instances block
automated access. They log at DEBUG level only.

CryptoPanic items use a 2× longer deduplication window (60 min vs 30 min)
and are processed before RSS/Nitter items because they are higher quality.

Per the feed integrity policy: after NEWS_MAX_FAILED_POLLS consecutive polls
where ALL sources returned nothing, the feed reports degraded and the engine
continues at 50% size (NEWS_DEGRADED_SIZE_MULTIPLIER).
"""

import asyncio
import hashlib
import json
import os
import sys
import warnings
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

import aiohttp

import config
from feeds.feed_health_monitor import FeedHealthMonitor

# System prompt for Gemini relevance filtering
_GEMINI_SYSTEM = (
    "You are a crypto trading news filter. From the given news items, extract "
    "only those relevant to: BTC or ETH price movements, major exchange events, "
    "regulatory actions affecting crypto, protocol incidents, or macroeconomic "
    "events that move crypto markets (Fed decisions, CPI prints, inflation data, "
    "trade wars, geopolitical shocks). Ignore: sports, entertainment, unrelated "
    "business news, altcoin promotions, NFT hype. "
    "CryptoPanic posts are from a crypto-specific news aggregator and should be "
    "weighted more heavily than general RSS feeds. "
    "Return ONLY a valid JSON array "
    "of strings. Each string is one clear headline under 15 words. Maximum 10 "
    "items. No explanation, no markdown, just the JSON array."
)

_DEBUG = os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG"


def _md5(text: str) -> str:
    return hashlib.md5(text.lower().strip().encode()).hexdigest()


def _parse_published(entry) -> Optional[datetime]:
    """Extract published datetime from a feedparser entry. Returns None on failure."""
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            import calendar
            return datetime.fromtimestamp(
                calendar.timegm(entry.published_parsed), tz=timezone.utc
            )
        if hasattr(entry, "published") and entry.published:
            return parsedate_to_datetime(entry.published).astimezone(timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc)  # Default to now if unparseable


class NewsFeed:
    """
    Multi-source news aggregator.

    Fetches headlines from RSS feeds and Nitter accounts concurrently.
    Pre-filters and summarises using Gemini Flash.
    Passes cleaned headlines to event_overlay.py for Claude Sonnet severity scoring.
    """

    def __init__(self, health_monitor: FeedHealthMonitor) -> None:
        self._monitor = health_monitor
        self._monitor.register("news")
        self._monitor.register("cryptopanic")

        self._seen_hashes: dict[str, datetime] = {}     # RSS/Nitter md5 → first_seen (30 min window)
        self._seen_hashes_cp: dict[str, datetime] = {}  # CryptoPanic md5 → first_seen (60 min window)
        self._latest_headlines: list[str] = []
        self._consecutive_failures: int = 0

        # Mark cryptopanic degraded immediately if no API key configured
        if not os.getenv("CRYPTOPANIC_API_KEY", ""):
            self._monitor.report_degraded("cryptopanic", "API key not configured")

        # Configure Gemini
        gemini_key = os.getenv("GEMINI_API_KEY", "")
        self._gemini = None
        if not gemini_key:
            print(
                "[news_feed] GEMINI_API_KEY not set — "
                "using raw RSS titles as fallback (no Gemini filtering)",
                file=sys.stderr,
            )
        else:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FutureWarning)
                    import google.generativeai as genai
                    genai.configure(api_key=gemini_key)
                    self._gemini = genai.GenerativeModel(
                        config.GEMINI_MODEL,
                        system_instruction=_GEMINI_SYSTEM,
                    )
                print(
                    f"[news_feed] Gemini configured: model={config.GEMINI_MODEL}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[news_feed] Gemini init failed: {exc} — falling back to raw titles",
                    file=sys.stderr,
                )

    # ── Start / scheduling ────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Run an immediate poll then schedule recurring polls via APScheduler.
        Returns after scheduling — APScheduler drives subsequent polls.
        """
        print("[news_feed] Starting multi-source aggregator", flush=True)
        await self._poll_all_sources()

        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            self._poll_all_sources,
            IntervalTrigger(minutes=config.NEWS_POLL_INTERVAL_MIN),
            id="news_poll",
        )
        scheduler.start()
        print(
            f"[news_feed] Scheduled: poll every {config.NEWS_POLL_INTERVAL_MIN}m",
            flush=True,
        )

    # ── Main poll ─────────────────────────────────────────────────────────────

    async def _poll_all_sources(self) -> None:
        """
        Concurrently fetch all RSS, Nitter, and CryptoPanic sources.
        CryptoPanic items are processed first (higher quality) and use a 2×
        longer deduplication window. Deduplicates across both caches so a
        story from CryptoPanic won't re-appear via RSS.
        """
        now = datetime.now(timezone.utc)

        # Build coroutine list: RSS + Nitter + CryptoPanic (CP is always last)
        rss_coros    = [self._fetch_rss(url)         for url  in config.RSS_FEEDS]
        nitter_coros = [self._fetch_nitter(acct)     for acct in config.NITTER_ACCOUNTS]
        n_std = len(rss_coros) + len(nitter_coros)

        all_results = await asyncio.gather(
            *(rss_coros + nitter_coros + [self._fetch_cryptopanic()]),
            return_exceptions=True,
        )

        std_results = all_results[:n_std]
        cp_result   = all_results[n_std]   # single CryptoPanic result

        # ── Standard sources (RSS + Nitter) ───────────────────────────────────
        raw_items_std: list[dict] = []
        any_success_std = False
        for r in std_results:
            if isinstance(r, list):
                raw_items_std.extend(r)
                any_success_std = True  # connection succeeded (even if empty)

        # ── CryptoPanic source ────────────────────────────────────────────────
        raw_items_cp: list[dict] = []
        cp_api_configured = bool(os.getenv("CRYPTOPANIC_API_KEY", ""))
        if isinstance(cp_result, list):
            raw_items_cp = cp_result
            if cp_api_configured:
                # Successful API call (list returned, possibly empty) → healthy
                self._monitor.report_healthy("cryptopanic")
        elif isinstance(cp_result, Exception) and cp_api_configured:
            self._monitor.report_degraded("cryptopanic", str(cp_result))

        # ── Expire dedup caches ───────────────────────────────────────────────
        cutoff_std = now - timedelta(minutes=config.NEWS_DEDUP_WINDOW_MIN)
        cutoff_cp  = now - timedelta(minutes=config.NEWS_DEDUP_WINDOW_MIN * 2)
        self._seen_hashes    = {h: t for h, t in self._seen_hashes.items()    if t > cutoff_std}
        self._seen_hashes_cp = {h: t for h, t in self._seen_hashes_cp.items() if t > cutoff_cp}

        cutoff_age = now - timedelta(minutes=config.NEWS_MAX_AGE_MIN)
        new_items: list[dict] = []

        # Process CryptoPanic first — higher quality, longer dedup window
        for item in raw_items_cp:
            h = _md5(item["title"])
            if h in self._seen_hashes_cp or h in self._seen_hashes:
                continue
            pub = item.get("published") or now
            if pub < cutoff_age:
                continue
            self._seen_hashes_cp[h] = now
            new_items.append(item)

        # Then standard sources — skip anything already covered by CP
        for item in raw_items_std:
            h = _md5(item["title"])
            if h in self._seen_hashes or h in self._seen_hashes_cp:
                continue
            pub = item.get("published") or now
            if pub < cutoff_age:
                continue
            self._seen_hashes[h] = now
            new_items.append(item)

        if new_items:
            headlines = await self._summarise_with_gemini(new_items)
            self._latest_headlines = (self._latest_headlines + headlines)[-50:]
            cp_count  = sum(1 for i in new_items if i.get("source") == "cryptopanic")
            std_count = len(new_items) - cp_count
            print(
                f"[news_feed] +{len(headlines)} new headlines "
                f"(cp={cp_count} std={std_count} "
                f"total cache: {len(self._latest_headlines)})",
                flush=True,
            )

        # ── Health reporting (news feed — standard sources only) ──────────────
        if any_success_std:
            self._consecutive_failures = 0
            self._monitor.report_healthy("news")
        else:
            self._consecutive_failures += 1
            print(
                f"[news_feed] All RSS/Nitter sources failed "
                f"({self._consecutive_failures}/{config.NEWS_MAX_FAILED_POLLS})",
                file=sys.stderr,
            )
            if self._consecutive_failures >= config.NEWS_MAX_FAILED_POLLS:
                self._monitor.report_degraded(
                    "news",
                    f"All RSS and Nitter sources failed after "
                    f"{self._consecutive_failures} consecutive polls — "
                    "50% size mode active (per feed integrity policy)",
                )

    # ── Fetchers ──────────────────────────────────────────────────────────────

    async def _fetch_rss(self, url: str) -> list[dict]:
        """
        Fetch and parse an RSS feed via feedparser (sync, run in executor).
        Returns a list of raw item dicts. Returns [] on any error.
        """
        try:
            import feedparser
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, url)
            items = []
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                if not title:
                    continue
                items.append({
                    "title":     title,
                    "summary":   getattr(entry, "summary", ""),
                    "published": _parse_published(entry),
                    "source":    url,
                })
            return items
        except Exception as exc:
            print(f"[news_feed] RSS fetch failed ({url}): {exc}", file=sys.stderr)
            return []

    async def _fetch_nitter(self, username: str) -> list[dict]:
        """
        Fetch a Nitter RSS feed for the given Twitter username.
        Tries primary URL, then fallback URL.
        Failures logged at DEBUG level only — Nitter failures are expected.
        Returns [] on any error.
        """
        try:
            import feedparser
            loop = asyncio.get_event_loop()

            primary_url = f"{config.NITTER_BASE_URL}/{username}/rss"
            fallback_url = f"{config.NITTER_FALLBACK_URL}/{username}/rss"

            feed = None
            for url in (primary_url, fallback_url):
                try:
                    result = await loop.run_in_executor(None, feedparser.parse, url)
                    if result.entries:
                        feed = result
                        break
                except Exception as exc:
                    if _DEBUG:
                        print(
                            f"[news_feed] Nitter @{username} ({url}): {exc}",
                            file=sys.stderr,
                        )

            if not feed or not feed.entries:
                return []

            items = []
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                if not title:
                    continue
                items.append({
                    "title":     f"@{username}: {title}",
                    "summary":   getattr(entry, "summary", ""),
                    "published": _parse_published(entry),
                    "source":    f"nitter:{username}",
                })
            return items

        except Exception as exc:
            if _DEBUG:
                print(
                    f"[news_feed] Nitter @{username} unexpected error: {exc}",
                    file=sys.stderr,
                )
            return []

    async def _fetch_cryptopanic(self) -> list[dict]:
        """
        Fetch latest posts from the CryptoPanic Developer API v4.

        Requires CRYPTOPANIC_API_KEY environment variable.
        Returns [] silently if key is not configured.
        Returns [] and logs a warning on any API error.

        Each returned item:
          title, summary (body), published (datetime), source="cryptopanic",
          votes_positive, votes_negative
        """
        api_key = os.getenv("CRYPTOPANIC_API_KEY", "")
        if not api_key:
            return []

        params = {
            "auth_token":  api_key,
            "filter":      config.CRYPTOPANIC_FILTER,
            "currencies":  config.CRYPTOPANIC_CURRENCIES,
            "public":      "true",
            "limit":       str(config.CRYPTOPANIC_MAX_POSTS),
        }

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    config.CRYPTOPANIC_BASE_URL, params=params
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.json()

            results = data.get("results", [])
            items: list[dict] = []
            for r in results:
                title = (r.get("title") or "").strip()
                if not title:
                    continue
                published_raw = r.get("published_at", "")
                try:
                    published = datetime.fromisoformat(
                        published_raw.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    published = datetime.now(timezone.utc)

                votes = r.get("votes") or {}
                items.append({
                    "title":          title,
                    "summary":        r.get("body") or "",
                    "published":      published,
                    "source":         "cryptopanic",
                    "votes_positive": int(votes.get("positive") or 0),
                    "votes_negative": int(votes.get("negative") or 0),
                })

            if _DEBUG:
                print(
                    f"[news_feed] CryptoPanic: {len(items)} posts fetched",
                    flush=True,
                )
            return items

        except Exception as exc:
            print(
                f"[news_feed] CryptoPanic fetch failed: {exc}",
                file=sys.stderr,
            )
            return []

    # ── Gemini summarisation ──────────────────────────────────────────────────

    async def _summarise_with_gemini(self, raw_items: list[dict]) -> list[str]:
        """
        Use Gemini Flash to filter and clean raw headlines into concise,
        relevant summaries under 15 words each.

        Falls back to raw titles if Gemini is unavailable or fails.
        """
        if self._gemini is None:
            return [item["title"] for item in raw_items[:10]]

        user_content = json.dumps([
            {
                "title":   i["title"],
                "summary": i.get("summary", "")[:200],
            }
            for i in raw_items[:20]
        ])

        try:
            loop = asyncio.get_event_loop()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                import google.generativeai as genai
                response = await loop.run_in_executor(
                    None,
                    lambda: self._gemini.generate_content(
                        user_content,
                        generation_config=genai.types.GenerationConfig(
                            response_mime_type="application/json"
                        ),
                    ),
                )
            text = response.text.strip()
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(h) for h in parsed[:10] if h]
            return [item["title"] for item in raw_items[:10]]
        except json.JSONDecodeError:
            print("[news_feed] Gemini returned non-JSON — using raw titles", file=sys.stderr)
            return [item["title"] for item in raw_items[:10]]
        except Exception as exc:
            print(f"[news_feed] Gemini summarisation error: {exc}", file=sys.stderr)
            return [item["title"] for item in raw_items[:10]]

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_latest_headlines(self, count: int = None) -> list[str]:
        """
        Return the latest N headlines (default: config.CLAUDE_NEWS_HEADLINES_COUNT).
        Returns an empty list if no headlines have been fetched yet.
        """
        n = count if count is not None else config.CLAUDE_NEWS_HEADLINES_COUNT
        return self._latest_headlines[-n:]

    def is_degraded(self) -> bool:
        """Return True if consecutive failure count >= NEWS_MAX_FAILED_POLLS."""
        return self._consecutive_failures >= config.NEWS_MAX_FAILED_POLLS
