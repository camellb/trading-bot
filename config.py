# ── Active configuration — Polymarket prediction-market bot ──────────────────

# ── Run mode ──────────────────────────────────────────────────────────────────
# 'shadow' — simulate fills at observed market prices, no real money.
# 'live'   — route orders through Polymarket CLOB (requires credentials).
PM_MODE = "shadow"

# Shadow-mode starting bankroll — simulated. Not a real deposit.
PM_SHADOW_STARTING_CASH = 1000.0

# Live-mode starting bankroll — conservative, increased after track record.
PM_LIVE_STARTING_CASH   = 200.0


# ── Sizing (Kelly + guardrails) ──────────────────────────────────────────────
# See execution/pm_sizer.py for how these compose.

# ── Live thresholds (tight — real money) ────────────────────────────────────
PM_LIVE_MIN_EDGE_BPS       = 500.0    # 5 percentage points
PM_LIVE_MIN_CONFIDENCE     = 0.45

# ── Shadow thresholds (loose — maximise sample size in simulation) ──────────
PM_SHADOW_MIN_EDGE_BPS     = 150.0    # 1.5pp — capture more marginal edges for calibration data
PM_SHADOW_MIN_CONFIDENCE   = 0.20     # lowered to maximise sample size

# Kelly scaling — quarter Kelly is the standard defensive choice.
PM_KELLY_FRACTION = 0.25

# Max single-position size as fraction of current bankroll.
PM_MAX_POSITION_PCT = 0.10        # 10% — simulation needs fuller Kelly sizing

# Absolute min/max per bet.
PM_MIN_TRADE_USD = 1.0            # $1 min in simulation — maximise sample size
PM_MAX_TRADE_USD = 100.0          # $100 cap — simulation can test larger positions

# ── Edge ceiling ─────────────────────────────────────────────────────────────
# Refuse bets where Claude claims more than this much edge vs the market.
# When Claude claims 50+pp of edge, it's almost certainly wrong — the
# market is rarely that mispriced. This prevents catastrophic Kelly sizing
# on bad edge estimates.
PM_MAX_EDGE_BPS = 2500.0          # 25pp — default fallback for unknown archetypes

# Per-archetype edge ceilings (basis points).
# Markets with verifiable ground-truth data can justify larger disagreements
# with the crowd than speculative markets. Weather has NOAA/ECMWF forecasts,
# crypto has on-chain data, etc. Speculative markets (geopolitics) get tighter
# ceilings because crowd aggregation is strong in those domains.
PM_ARCHETYPE_MAX_EDGE_BPS: dict[str, float] = {
    "weather": 5000.0,          # verifiable forecast data can genuinely beat crowds
    "price_threshold": 4000.0,  # real-time exchange/on-chain data
    "crypto": 4000.0,           # on-chain + exchange APIs
    "macro_release": 3500.0,    # published economic indicators
    "scientific": 3500.0,       # peer-reviewed data, clinical trial registries
    "sports_match": 3000.0,     # scores verifiable, but odds markets are efficient
    "sports_prop": 3000.0,      # stats verifiable
    "binary_event": 2500.0,     # default
    "entertainment": 2500.0,    # mixed verifiability
    "legal": 2500.0,            # interpretive but verifiable
    "geopolitical": 2000.0,     # speculative — crowd aggregation is strong
    "other": 2500.0,            # conservative default
}

# Extreme edge justification — above this threshold (bps), the evaluator
# sends Claude a follow-up prompt asking for specific verifiable evidence.
# If Claude can cite concrete data the trade proceeds; if justification is
# weak, the trade is skipped even if within the archetype ceiling.
PM_EXTREME_EDGE_JUSTIFICATION_BPS = 1500.0

# Max simultaneous open positions across all markets.
PM_MAX_CONCURRENT_POSITIONS = 100

# Max positions in same event group (correlated markets, e.g. multiple
# Champions League winner options or multiple Iran deal timeframes).
PM_MAX_PER_EVENT = 10


# ── Risk management ─────────────────────────────────────────────────────────
# Portfolio-level risk controls enforced by execution/risk_manager.py.
# These sit between the sizer and the executor — every trade must pass.

# Daily loss limit: refuse new positions if realised losses today exceed
# this fraction of starting bankroll. Resets at midnight UTC.
PM_DAILY_LOSS_LIMIT_PCT = 100.0       # disabled in simulation — maximise sample size

# Weekly loss limit: same, but on a Monday-to-Sunday UTC window.
PM_WEEKLY_LOSS_LIMIT_PCT = 100.0      # disabled in simulation — maximise sample size

# Consecutive loss cooldown: after this many consecutive losses, reduce
# position sizes by PM_LOSS_STREAK_SIZE_MULT for the next N trades
# (where N = PM_LOSS_STREAK_THRESHOLD). Resets on restart.
PM_LOSS_STREAK_THRESHOLD = 999        # disabled in simulation — don't throttle sample size
PM_LOSS_STREAK_SIZE_MULT = 0.5        # 50% size during cooldown (if threshold is ever hit)

# Portfolio heat: max fraction of bankroll deployed in open positions.
# Prevents over-commitment even when individual positions are small.
PM_MAX_PORTFOLIO_HEAT_PCT = 1.0   # 100% — shadow mode, full bankroll available

# Archetype concentration: max open positions with the same archetype.
PM_MAX_PER_ARCHETYPE = 30

# Drawdown circuit breaker: halt ALL trading if bankroll drops below
# this fraction of peak bankroll. Disabled in shadow mode (test mode).
PM_DRAWDOWN_HALT_PCT = 0.01       # effectively off — shadow mode, let it run


# ── Per-archetype edge overrides (basis points) ─────────────────────────────
# If a market's archetype is in this dict, use the override instead of the
# global PM_SHADOW_MIN_EDGE_BPS / PM_LIVE_MIN_EDGE_BPS.
# Archetypes: price_threshold, binary_event, sports_match, sports_prop,
#   geopolitical, macro_release, crypto, entertainment, scientific,
#   legal, weather, other
PM_ARCHETYPE_EDGE_OVERRIDES: dict[str, float] = {
    # Simulation: lowered to maximise sample size. After Bayesian shrinkage
    # + spread/fees, raw 7pp edge becomes ~2.8pp effective. Thresholds here
    # are EFFECTIVE edge (post spread+fees), not raw.
    # In live mode tighten back up (sports_match=500+, geopolitical=400+).
    "sports_match": 200.0,
    "sports_prop": 200.0,
    "price_threshold": 100.0,
    "geopolitical": 200.0,
    "macro_release": 150.0,
}

# Per-archetype confidence floor overrides (same structure).
PM_ARCHETYPE_CONFIDENCE_OVERRIDES: dict[str, float] = {}

# ── Resolution quality filter ───────────────────────────────────────────────
# Markets with resolution_quality_score below this are skipped.
PM_MIN_RESOLUTION_QUALITY = 0.1        # lowered for simulation — accept more markets

# ── Execution realism ──────────────────────────────────────────────────────
# Estimated half-spread in price units (0..1). Shadow fills adjust entry
# price by this amount to simulate realistic execution.
PM_SHADOW_SPREAD_ESTIMATE = 0.005   # 0.5 cent half-spread estimate (conservative)
# Estimated round-trip fee rate (fraction of notional).
PM_SHADOW_FEE_RATE = 0.002          # 20 bps maker+taker combined

# ── Confidence recalibration ───────────────────────────────────────────────
# Dampen confidence for archetypes where the bot has historically been
# overconfident. Maps archetype -> dampening factor (0..1, where 1.0 = no
# change). Updated by self-improvement loop.
# Data: confidence is anti-predictive at high levels (correlation = -0.26
# for NO bets). High-confidence bucket (0.75+): 4 bets, 2 wins, -$188 P&L.
PM_CONFIDENCE_DAMPEN: dict[str, float] = {
    "sports_match": 0.6,          # Sports markets are well-calibrated
    "sports_prop": 0.7,           # Props slightly less efficient
}

# ── Disagreement penalty ───────────────────────────────────────────────────
# When Claude's edge exceeds this threshold (in decimal, 0..1), apply a
# linear penalty that shrinks the Kelly fraction. This makes the bot MORE
# conservative when it disagrees most with the market — precisely where
# Kelly sizing is most dangerous if the edge estimate is wrong.
# penalty = min(1.0, threshold / edge)  →  halved at 2x, quartered at 4x.
PM_DISAGREEMENT_PENALTY_THRESHOLD = 0.15  # 15pp — start penalizing above this

# ── Market discovery ─────────────────────────────────────────────────────────
PM_SCAN_LIMIT           = 100         # candidates per scan
PM_MIN_VOLUME_24H_USD   = 5_000.0     # liquidity filter
PM_MIN_DAYS_TO_END      = 0           # include markets resolving in hours
PM_MAX_DAYS_TO_END      = 7           # 7 days — fast resolution, no capital lockup
PM_SKIP_EXISTING_DAYS   = 1           # re-evaluate daily for fast-resolving markets


# ── Scheduler cadence ────────────────────────────────────────────────────────
# Market scan: how often we look for new opportunities.
# Simulation: scan aggressively to maximise evaluation throughput.
PM_SCAN_INTERVAL_MINUTES = 5

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
PM_TEST_END               = "2026-04-27T20:00:00+08:00"  # 7-day simulation test deadline

GO_LIVE_MIN_RESOLVED      = 150       # sample size
GO_LIVE_MAX_BRIER         = 0.22
GO_LIVE_MIN_REALIZED_PNL  = 0.0       # synthetic P&L net of simulated fees


# ── Anthropic ────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_KEYWORD_MODEL = "claude-haiku-4-5-20251001"  # cheap + reliable for keyword extraction
CLAUDE_MAX_TOKENS = 700


# ── Gemini (optional fallback for keyword extraction / news pre-filter) ──────
GEMINI_MODEL = "gemini-2.5-flash"


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
NEWS_DEGRADED_SIZE_MULTIPLIER = 0.5

# ── CryptoPanic (optional crypto news aggregator) ───────────────────────────
CRYPTOPANIC_BASE_URL     = "https://cryptopanic.com/api/v1/posts/"
CRYPTOPANIC_FILTER       = "hot"
CRYPTOPANIC_CURRENCIES   = "BTC,ETH,SOL"
CRYPTOPANIC_MAX_POSTS    = 20


# ── Feed staleness (used by feed_health_monitor) ─────────────────────────────
HEARTBEAT_TIMEOUT_S = 5
MACRO_CALENDAR_REFRESH_DAYS = 7


# ── Obsidian memory vault ────────────────────────────────────────────────────
OBSIDIAN_VAULT_PATH = "~/Documents/trading-bot-memory"


# ── Ensemble forecasting ────────────────────────────────────────────────────
# Use multiple models and aggregate via logit-averaging for more robust
# probability estimates. Disagreement between models penalises confidence.
ENSEMBLE_ENABLED = True
ENSEMBLE_SECONDARY_MODEL = "gemini-2.5-flash"  # diverse model family
ENSEMBLE_DISAGREEMENT_THRESHOLD = 0.15  # logit std above this → confidence penalty

# ── Research quality scoring ────────────────────────────────────────────────
# Research bundle quality affects confidence multiplier.
# Low-quality research (few sources, stale data) → smaller positions.
RESEARCH_QUALITY_LOW_THRESHOLD = 0.4
RESEARCH_QUALITY_LOW_CONFIDENCE_MULT = 0.6
RESEARCH_QUALITY_MED_CONFIDENCE_MULT = 0.85

# ── Variance-adjusted Kelly ────────────────────────────────────────────────
# Per-archetype estimator variance — how noisy our probability estimates are.
# Higher variance → more conservative sizing. Initial conservative estimates;
# will be calibrated from resolved prediction data over time.
PM_ARCHETYPE_ESTIMATOR_VARIANCE: dict[str, float] = {
    # Simulation: low variance to allow meaningful Kelly fractions.
    # Key insight: variance_penalty = σ²/payoff². For typical payoff ~0.5,
    # penalty = σ²/0.25 = 4σ². So σ²=0.012 gives penalty=0.048, which
    # exceeds typical kelly_full of 0.04-0.06 → zero trades.
    # Values here allow penalty ≈ 0.012-0.020, leaving room for sizing.
    # Will be calibrated from per-archetype (predicted - actual)^2 data.
    "sports_match": 0.005,
    "sports_prop": 0.004,
    "geopolitical": 0.003,
    "crypto": 0.003,
    "price_threshold": 0.002,
    "macro_release": 0.003,
    "binary_event": 0.003,
    "entertainment": 0.004,
    "scientific": 0.004,
    "legal": 0.004,
    "weather": 0.004,
    "other": 0.004,
}

# Bayesian shrinkage — how much to trust model vs market price per archetype.
# 0.0 = fully trust market, 1.0 = fully trust model. Low values for efficient
# markets (sports), higher for markets where model can plausibly add value.
PM_ARCHETYPE_MODEL_TRUST: dict[str, float] = {
    # Simulation: higher trust to generate more trades for calibration data.
    # Once we have enough data, tighten these based on per-archetype Brier.
    "sports_match": 0.50,
    "sports_prop": 0.55,
    "geopolitical": 0.75,
    "crypto": 0.65,
    "price_threshold": 0.80,
    "macro_release": 0.70,
    "binary_event": 0.65,
    "entertainment": 0.60,
    "scientific": 0.70,
    "legal": 0.70,
    "weather": 0.70,
    "other": 0.55,
}

# ── Correlation-aware portfolio sizing ──────────────────────────────────────
# Estimated correlation between positions for diversification-aware sizing.
PM_CORRELATION_SAME_EVENT = 0.70
PM_CORRELATION_SAME_ARCHETYPE = 0.30
PM_CORRELATION_SAME_CATEGORY = 0.15
PM_CORRELATION_DEFAULT = 0.05
PM_DRY_POWDER_RESERVE_PCT = 0.10  # hold back 10% in simulation (20% in live)

# ── Resolution source risk scores ───────────────────────────────────────────
# Multiply effective edge by this factor based on how reliable the resolution
# source is. Unreliable sources → smaller positions.
PM_RESOLUTION_SOURCE_SCORES: dict[str, float] = {
    "coingecko.com": 0.95,
    "binance.com": 0.95,
    "espn.com": 0.90,
    "nba.com": 0.90,
    "mlb.com": 0.90,
    "premierleague.com": 0.90,
    "reuters.com": 0.85,
    "bloomberg.com": 0.85,
    "apnews.com": 0.85,
    "bbc.co.uk": 0.85,
    "twitter.com": 0.40,
    "x.com": 0.40,
}
PM_RESOLUTION_SOURCE_DEFAULT_SCORE = 0.85  # most markets resolve fine, don't over-penalize unknowns

# ── Self-improvement hardening ──────────────────────────────────────────────
# Power analysis gate — require sufficient data per archetype before tuning.
SELF_IMPROVE_MIN_POWER_SAMPLES = 30
SELF_IMPROVE_MAX_RELATIVE_CHANGE = 0.15  # tighter ±15% per change

# ── Go-live gate improvements ───────────────────────────────────────────────
# Require P&L to exceed estimated execution costs, not just > $0.
GO_LIVE_COST_PER_TRADE_BPS = 150  # estimated round-trip execution cost


# ── Legacy knobs kept for back-compat with cross-cutting modules ─────────────
# macro_context.py uses these; safe to keep until that module is retargeted.
FUNDING_CACHE_REFRESH_HOURS = 4
CLAUDE_NEWS_HEADLINES_COUNT = 5
