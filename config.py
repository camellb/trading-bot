# ── Active configuration — Polymarket prediction-market bot ──────────────────

# ── Run mode ──────────────────────────────────────────────────────────────────
# 'shadow' — simulate fills at observed market prices, no real money.
# 'live'   — route orders through Polymarket CLOB (requires credentials).
PM_MODE = "shadow"

# Shadow-mode starting bankroll — simulated. Not a real deposit.
PM_SHADOW_STARTING_CASH = 1000.0

# Live-mode starting bankroll — conservative, increased after track record.
PM_LIVE_STARTING_CASH   = 200.0


# ── Sizing ───────────────────────────────────────────────────────────────────
# All sizing is positive-EV with flat stakes. See execution/pm_sizer.py for
# the math and engine/user_config.py for per-user thresholds (min_ev_threshold,
# base_stake_pct, max_stake_pct). No global Kelly, no edge floors — those are
# a deliberately abandoned paradigm.

# Max simultaneous open positions across all markets.
PM_MAX_CONCURRENT_POSITIONS = 100

# Max positions in same event group (correlated markets, e.g. multiple
# Champions League winner options or multiple Iran deal timeframes).
PM_MAX_PER_EVENT = 3


# ── Market discovery ─────────────────────────────────────────────────────────
PM_SCAN_LIMIT           = 40          # how many markets per scan
PM_MIN_VOLUME_24H_USD   = 5_000.0     # liquidity filter (relaxed for short-horizon)
PM_MIN_DAYS_TO_END      = 0           # include markets resolving in hours
PM_MAX_DAYS_TO_END      = 7           # 7-day shadow test — short-horizon only
PM_SKIP_EXISTING_DAYS   = 1           # re-evaluate daily for fast-resolving markets


# ── Scheduler cadence ────────────────────────────────────────────────────────
# Market scan: how often we look for new opportunities.
# 1H is a good balance — PM markets move slowly, but news-driven pricing
# shifts can create short-lived edges.
PM_SCAN_INTERVAL_MINUTES = 30

# Position resolver: checks for settled markets and updates P&L.
PM_RESOLVE_INTERVAL_HOURS = 1
# Fast resolver for short-horizon markets (< 1 day to end).
PM_RESOLVE_FAST_INTERVAL_MINUTES = 10


# ── Self-improvement ─────────────────────────────────────────────────────────
# Minimum resolved predictions before the weekly loop applies any changes.
# Statistical significance tuning.
SELF_IMPROVE_MIN_RESOLVED = 15

# Target Brier score we aim to stay below. Uninformed baseline is 0.25;
# well-calibrated forecasters typically score 0.15–0.22 on PM-style markets.
SELF_IMPROVE_TARGET_BRIER = 0.22


# ── Go-live gate ─────────────────────────────────────────────────────────────
# Thresholds required before flipping PM_MODE from 'shadow' to 'live'.
# These are ADVISORY only — there's no automated promotion. They appear in
# the weekly summary to tell you when the engine has earned real capital.
PM_TEST_END               = "2026-04-24T20:10:00+08:00"  # 7-day shadow test deadline

GO_LIVE_MIN_RESOLVED      = 30        # sample size
GO_LIVE_MAX_BRIER         = 0.22
GO_LIVE_MIN_REALIZED_PNL  = 0.0       # synthetic P&L net of simulated fees


# ── Anthropic ────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 700


# ── Gemini (optional, for news pre-filter) ───────────────────────────────────
GEMINI_MODEL = "gemini-2.0-flash"


# ── News aggregator — RSS sources ────────────────────────────────────────────
# Used by research/fetcher.py to surface recent headlines relevant to a
# market's keywords. News is an ENRICHMENT signal, not a trigger — the bot
# decides based on Claude's probability vs market price, not news directly.
RSS_FEEDS = [
    # Crypto
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    # Macro / Finance
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://www.ft.com/markets?format=rss",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    # Politics / Geopolitics (PM-heavy categories)
    "https://feeds.reuters.com/reuters/politicsNews",
    "https://feeds.reuters.com/Reuters/worldNews",
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.politico.com/rss/politicopicks.xml",
    # Sports (PM has many sports markets)
    "https://www.espn.com/espn/rss/news",
]

# Nitter feeds (Twitter mirror) — optional.
NITTER_ACCOUNTS = [
    "NickTimiraos", "LynAldenContact", "MacroAlf", "elerianm",
    "LizAnnSonders", "realDonaldTrump", "federalreserve",
    "WatcherGuru", "lookonchain", "WuBlockchain", "tier10k",
]
NITTER_BASE_URL     = "https://nitter.net"
NITTER_FALLBACK_URL = "https://nitter.privacydev.net"
NEWS_DEDUP_WINDOW_MIN = 30
NEWS_MAX_AGE_MIN      = 60
NEWS_POLL_INTERVAL_MIN = 5
NEWS_MAX_FAILED_POLLS = 3

# ── CryptoPanic (optional crypto news aggregator) ───────────────────────────
CRYPTOPANIC_BASE_URL     = "https://cryptopanic.com/api/v1/posts/"
CRYPTOPANIC_FILTER       = "hot"
CRYPTOPANIC_CURRENCIES   = "BTC,ETH,SOL"
CRYPTOPANIC_MAX_POSTS    = 20


# ── Feed staleness (used by feed_health_monitor) ─────────────────────────────
HEARTBEAT_TIMEOUT_S = 5
MACRO_CALENDAR_REFRESH_DAYS = 7




# ── Legacy knobs kept for back-compat with cross-cutting modules ─────────────
# macro_context.py uses these; safe to keep until that module is retargeted.
FUNDING_CACHE_REFRESH_HOURS = 4
CLAUDE_NEWS_HEADLINES_COUNT = 5
