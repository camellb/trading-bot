# ── Active configuration ──────────────────────────────────────────────────────
# Constants below are used by the live bot.
# See backtester/ for backtest-specific overrides.

# ── Exchange ──────────────────────────────────────────────────────────────────
EXCHANGE = "okx"
TRADING_PAIRS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
PAPER_MODE = True

# ── Regime classifier (Layer A) ───────────────────────────────────────────────
ADX_TREND_THRESHOLD = 25
ADX_AMBIGUOUS_LOW = 20
ADX_AMBIGUOUS_HIGH = 25
FUNDING_CROWDED_PERCENTILE = 75
FUNDING_SHORTS_CROWDED_PERCENTILE = 25
FUNDING_EXTREME_PERCENTILE = 90
REALIZED_VOL_LOW_PCT = 20
REALIZED_VOL_HIGH_PCT = 80
RANGE_UNSTABLE_VOL_THRESHOLD = 60  # realized vol percentile above which RANGE→RANGE_UNSTABLE

# ── Event overlay (Layer E) ───────────────────────────────────────────────────
EVENT_PRE_WINDOW_HOURS = 2
EVENT_POST_WINDOW_HOURS = 1
CLAUDE_SEVERITY_BLOCK_THRESHOLD = 10
CLAUDE_SEVERITY_EVENT_RISK_THRESHOLD = 8
CLAUDE_SEVERITY_REDUCE_THRESHOLD = 5

# Scheduled macro windows (FOMC/CPI/PPI) always block entirely regardless of score

# ── Crypto confirmation (Layer C) ────────────────────────────────────────────
BASIS_PREMIUM_MAX_PCT = 0.3    # max mark/index premium (%) before LONG is blocked
BASIS_DISCOUNT_MAX_PCT = -0.1  # max mark/index discount (%) before SHORT is blocked

# ── Execution filter (Layer D) ────────────────────────────────────────────────
MAX_SPREAD_PCT = 0.05
MAX_SLIPPAGE_PCT = 0.10
MIN_DEPTH_MULTIPLE = 5
MAX_OB_CONTRA_IMBALANCE = 0.60
API_HEARTBEAT_MAX_AGE_S = 5

# ── Risk engine (Layer F) ─────────────────────────────────────────────────────
STARTING_CAPITAL_USD = 500.0   # Paper portfolio value; replaced by live CCXT balance in M5
ATR_STOP_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 2.5
MAX_POSITION_PCT = 0.05
MAX_SIMULTANEOUS_POSITIONS = 2
DAILY_LOSS_CAP_USD = 50.0  # PROVISIONAL — review before going live

# ── SOL-specific overrides (SOL is more volatile than BTC/ETH) ───────────────
SOL_ATR_STOP_MULTIPLIER = 2.0   # wider stops for SOL volatility (vs 1.5 for BTC/ETH)
SOL_ATR_TP_MULTIPLIER   = 3.0   # wider TP proportionally (vs 2.5 for BTC/ETH)
SOL_MAX_POSITION_PCT    = 0.03  # 3% max per trade (vs 5% for BTC/ETH)

# ── Portfolio scaling ────────────────────────────────────────────────────────
PORTFOLIO_SCALE_ENABLED       = True   # False → use fixed STARTING_CAPITAL_USD
PORTFOLIO_MIN_TRADE_USD       = 5.0    # minimum trade size; below this, skip
PORTFOLIO_MAX_TRADE_USD       = 500.0  # maximum single trade size (safety cap)
PORTFOLIO_DAILY_CAP_PCT       = 0.10   # daily loss cap as % of portfolio (10%)
PORTFOLIO_ADVISORY_PROFIT_PCT = 0.20   # suggest withdrawal when up 20%
PORTFOLIO_ADVISORY_REFILL_PCT = -0.15  # suggest adding funds when down 15%

# ── Size multipliers ──────────────────────────────────────────────────────────
SIZE_MULTIPLIER_CROWDED = 0.50
SIZE_MULTIPLIER_RANGE_UNSTABLE = 0.25
SIZE_MULTIPLIER_EVENT_RISK = 0.25

# ── Conviction-based position sizing ─────────────────────────────────────────
CONVICTION_FULL_THRESHOLD   = 0.85   # >= 85% conviction → full size
CONVICTION_HIGH_THRESHOLD   = 0.65   # >= 65% conviction → 80% size
CONVICTION_MEDIUM_THRESHOLD = 0.45   # >= 45% conviction → 60% size
                                     # <  45% conviction → 40% size (minimum)
CONVICTION_SIZE_MULTIPLIERS = {
    "full":   1.00,
    "high":   0.80,
    "medium": 0.60,
    "low":    0.40,
}

# ── Feed integrity ────────────────────────────────────────────────────────────
NEWS_DEGRADED_SIZE_MULTIPLIER = 0.50
TICKER_STALE_THRESHOLD_S = 30
KLINE_STALE_THRESHOLD_MIN = 20
MARKPRICE_STALE_THRESHOLD_S = 30
HEARTBEAT_TIMEOUT_S = 5
NEWS_MAX_FAILED_POLLS = 3

# ── OKX margin and leverage ──────────────────────────────────────────────────
OKX_LEVERAGE            = 3          # leverage for all perpetual positions
OKX_MARGIN_MODE         = "isolated" # "isolated" | "cross"

# ── Order execution ──────────────────────────────────────────────────────────
ORDER_TYPE              = "limit"  # "limit" (maker, 0.02%) | "market" (taker, 0.05%)
LIMIT_ORDER_TIMEOUT_S   = 30       # cancel if not filled within this many seconds
LIMIT_ORDER_SLIP_TICKS  = 1        # place N ticks inside spread for fast maker fill

# ── Backtester fees ──────────────────────────────────────────────────────────
BACKTEST_MAKER_FEE_PCT  = 0.0002   # 0.02% OKX maker fee (limit orders)
BACKTEST_TAKER_FEE_PCT  = 0.0005   # 0.05% OKX taker fee (market orders)
BACKTEST_USE_MAKER_FEES = True     # use maker fees (matches ORDER_TYPE = "limit")

# ── Timing ────────────────────────────────────────────────────────────────────
NEWS_POLL_INTERVAL_MIN = 5
FUNDING_CACHE_REFRESH_HOURS = 4
MACRO_CALENDAR_REFRESH_DAYS = 7

# ── Anthropic ────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 100
CLAUDE_NEWS_HEADLINES_COUNT = 5

# ── Gemini (news pre-filter) ──────────────────────────────────────────────────
GEMINI_MODEL = "gemini-2.0-flash"

# ── Deribit implied volatility (DVOL) ────────────────────────────────────────
DERIBIT_BASE_URL              = "https://www.deribit.com/api/v2"
DERIBIT_IV_HIGH_THRESHOLD     = 80.0    # DVOL >= 80 → high IV → 70% size
DERIBIT_IV_EXTREME_THRESHOLD  = 100.0   # DVOL >= 100 → extreme IV → 35% size
DERIBIT_IV_SPIKE_PCT          = 0.20    # 20%+ hourly increase = IV spike
DERIBIT_IV_SIZE_MULTIPLIER    = 0.70    # high IV → 70% of computed size
DERIBIT_IV_EXTREME_MULTIPLIER = 0.35    # extreme IV → 35% of computed size
DERIBIT_IV_CACHE_SECONDS      = 300     # re-fetch no more often than every 5 min

# ── CryptoPanic ───────────────────────────────────────────────────────────────
CRYPTOPANIC_BASE_URL   = "https://cryptopanic.com/api/developer/v4/posts/"
CRYPTOPANIC_FILTER     = "important"    # "important" | "rising" | "hot" | "bullish" | "bearish"
CRYPTOPANIC_CURRENCIES = "BTC,ETH,SOL" # filter to BTC, ETH, and SOL news
CRYPTOPANIC_MAX_POSTS  = 20            # max posts per fetch

# ── News aggregator — RSS sources ─────────────────────────────────────────────
RSS_FEEDS = [
    # Crypto
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/feed",
    "https://cryptoslate.com/feed/",
    "https://coingape.com/feed/",
    "https://messari.io/rss",
    # Macro / Finance
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://www.ft.com/markets?format=rss",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "https://www.investing.com/rss/news_301.rss",
]

# ── News aggregator — Nitter accounts ─────────────────────────────────────────
NITTER_ACCOUNTS = [
    # Crypto
    "WatcherGuru", "whale_alert", "lookonchain", "tier10k",
    "WuBlockchain", "CoinDesk", "Cointelegraph", "DocumentingBTC",
    "VitalikButerin", "brian_armstrong", "CryptoCred", "RektCapital",
    # Macro / Finance
    "NickTimiraos", "stlouisfed", "LynAldenContact", "MacroAlf",
    "elerianm", "LizAnnSonders", "RayDalio", "GoldmanSachs",
    "elonmusk", "realDonaldTrump", "federalreserve",
]

NITTER_BASE_URL = "https://nitter.net"
NITTER_FALLBACK_URL = "https://nitter.privacydev.net"
NEWS_DEDUP_WINDOW_MIN = 30
NEWS_MAX_AGE_MIN = 60

# ── Obsidian memory vault ─────────────────────────────────────────────────────
OBSIDIAN_VAULT_PATH = "~/Documents/trading-bot-memory"
