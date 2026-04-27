# ── Active configuration - Polymarket prediction-market bot ──────────────────

# ── Run mode ──────────────────────────────────────────────────────────────────
# 'simulation' - simulate fills at observed market prices, no real money.
# 'live'       - route orders through Polymarket CLOB (requires credentials).
PM_MODE = "simulation"

# Simulation-mode starting bankroll - simulated. Not a real deposit.
PM_SIMULATION_STARTING_CASH = 1000.0

# Live-mode starting bankroll - conservative, increased after track record.
PM_LIVE_STARTING_CASH   = 200.0


# ── Sizing ───────────────────────────────────────────────────────────────────
# V1 doctrine (locked 2026-04-27, see memory/doctrine_back_the_forecast.md).
# Side selection: the market favourite, period. Single skip gate: forecaster
# direction agreement (claude_p and market_p_yes must lie on the same side
# of 0.50). Sizing: flat - stake = bankroll * base_stake_pct *
# archetype_multiplier, capped at max_stake_pct, with a $2 absolute floor.
# No confidence softener, no min_p_win, no Kelly, no EV-as-primary-gate.
# See execution/pm_sizer.py for the implementation and
# engine/user_config.py for the per-user knobs.

# Max simultaneous open positions across all markets.
PM_MAX_CONCURRENT_POSITIONS = 100

# Max positions in same event group (correlated markets, e.g. multiple
# Champions League winner options or multiple Iran deal timeframes).
PM_MAX_PER_EVENT = 3


# ── Market discovery ─────────────────────────────────────────────────────────
PM_SCAN_LIMIT           = 100         # how many markets per scan (locked value)
PM_MIN_VOLUME_24H_USD   = 5_000.0     # liquidity filter (relaxed for short-horizon)
PM_MIN_DAYS_TO_END      = 0           # include markets resolving in hours
PM_MAX_DAYS_TO_END      = 7           # 7-day simulation test - short-horizon only
PM_SKIP_EXISTING_DAYS   = 1           # re-evaluate daily for fast-resolving markets

# Tag-balanced scan quotas. Without this, a top-by-volume scan is ~80%
# sports because sports markets dominate Polymarket's 24h volume. Sports
# have been a net loser per archetype, so we cap their share and reserve
# slots for politics, geopolitics, crypto, economy, world, and culture.
# Keys are Polymarket Gamma top-level tag_id values; values are the max
# number of markets to keep from that tag in a single scan AFTER all
# uncertainty / horizon / liquidity gates are applied. A market that
# carries multiple top-level tags is fetched per tag bucket but
# deduplicated by id before being returned. Surplus from a tag that
# returns fewer markets than its quota is NOT redistributed to other
# tags, because that would silently undo the bias correction.
#
# Setting this to an empty dict falls back to the legacy untagged
# top-by-volume scan (kept available for emergencies and tests).
PM_SCAN_TAG_QUOTAS: dict[int, int] = {
    1:      20,   # Sports       (NBA / soccer / tennis - capped despite high volume)
    2:      30,   # Politics     (Fed, elections, policy)
    21:     15,   # Crypto       (BTC / ETH price thresholds)
    100265: 15,   # Geopolitics  (ceasefires, treaties, summits)
    100328: 10,   # Economy      (Fed rates, GDP, jobs)
    596:    5,    # Culture      (Oscars, awards, celebrity)
    101970: 5,    # World        (broader catch-all)
}


# ── Scheduler cadence ────────────────────────────────────────────────────────
# Market scan: how often we look for new opportunities.
# 5 min keeps the dashboard feeling live. Cost-safe because
# PM_SKIP_EXISTING_DAYS=1 means each market is Claude-evaluated at most
# once every 24h - re-scans only hit Claude for genuinely new markets.
PM_SCAN_INTERVAL_MINUTES = 5

# System-wide scanner kill-switch. Admin-controlled via /admin/scanner.
# False halts the scheduled scan for all users; per-user bot_enabled still
# applies on top of this.
PM_SCAN_ENABLED = True

# Position resolver: checks for settled markets and updates P&L.
# Resolution checks are a free Polymarket REST read per open position, so we
# can poll much more aggressively than the evaluation scan. Users expect
# positions to settle within ~a minute of the market resolving on Polymarket.
PM_RESOLVE_INTERVAL_MINUTES = 15
# Near-live resolver for short-horizon markets (< 24h to end). This is the
# common case for how Delfi trades, so the fast path carries the experience.
PM_RESOLVE_FAST_INTERVAL_SECONDS = 60


# ── Self-improvement ─────────────────────────────────────────────────────────
# Minimum resolved predictions before the weekly loop applies any changes.
# Statistical significance tuning.
SELF_IMPROVE_MIN_RESOLVED = 15

# Target Brier score we aim to stay below. Uninformed baseline is 0.25;
# well-calibrated forecasters typically score 0.15–0.22 on PM-style markets.
SELF_IMPROVE_TARGET_BRIER = 0.22


# ── Go-live gate ─────────────────────────────────────────────────────────────
# Thresholds required before flipping PM_MODE from 'simulation' to 'live'.
# These are ADVISORY only - there's no automated promotion. They appear in
# the weekly summary to tell you when the engine has earned real capital.
PM_TEST_END               = "2026-04-24T20:10:00+08:00"  # 7-day simulation test deadline

GO_LIVE_MIN_RESOLVED      = 30        # sample size
GO_LIVE_MAX_BRIER         = 0.22
GO_LIVE_MIN_REALIZED_PNL  = 0.0       # synthetic P&L net of simulated fees


# ── Anthropic ────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 700


# ── Gemini (optional, for news pre-filter) ───────────────────────────────────
GEMINI_MODEL = "gemini-flash-latest"


# ── News aggregator - RSS sources ────────────────────────────────────────────
# Used by research/fetcher.py to surface recent headlines relevant to a
# market's keywords. News is an ENRICHMENT signal, not a trigger - the bot
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

# Nitter feeds (Twitter mirror) - optional.
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
