"""
Fine-grained market archetype classifier.

Claude's evaluator emits a coarse `category` tag (sports / crypto /
geopolitics / ...). That granularity is too broad for the sizer's skip
list: skipping "sports" kills NBA playoffs to exclude tennis qualifiers.

This module produces a finer label from the question text, the event
slug, and (optionally) the coarse category. It is a pure function - no
I/O, no model calls - so it is cheap to run on every evaluation and
deterministic in tests.

Downstream uses:
  1. `execution/pm_sizer.size_position` reads the archetype against
     `user_config.archetype_skip_list` - a user can add 'tennis_qualifier'
     without muting all sports.
  2. `execution/pm_executor._open_simulation` persists it on
     `pm_positions.market_archetype` for post-hoc analytics.
  3. `engine/pm_analyst._log_market_evaluation` persists it on
     `market_evaluations.market_archetype` so skipped trades are also
     tagged (useful for "what did we turn down?" queries).

The taxonomy is deliberately flat - seventeen labels. If a branch of the
taxonomy grows we split; we do not nest.
"""

from __future__ import annotations

import re
from typing import Optional


# ── Tennis ──────────────────────────────────────────────────────────────────

_TENNIS_QUALIFIER_RE = re.compile(r"qualif(?:y|ication|ier|ying)", re.IGNORECASE)

# ATP / WTA branding anywhere in the question or event slug.
_ATP_RE = re.compile(r"\batp\b", re.IGNORECASE)
_WTA_RE = re.compile(r"\bwta\b", re.IGNORECASE)

# Named tennis tournaments - main draws we want to keep distinct from
# challenger / ITF / qualifier tiers. Matched case-insensitively.
_TENNIS_MAIN_DRAW_TOURNAMENTS = (
    "roland garros", "french open", "wimbledon",
    "australian open", "us open",
    "atp finals", "wta finals",
    "indian wells", "miami open",
    "monte carlo", "cincinnati masters",
    "shanghai masters", "paris masters",
    "madrid open", "rome masters", "italian open",
)

# Low-tier venue prefixes we've seen in event slugs (e.g. "atp-rybakov-…").
# Questions for these read like "Savannah: X vs Y" - the tournament
# city is the prefix before the colon.
_TENNIS_LOWER_TIER_CITY_HINTS = (
    "savannah", "abidjan", "oeiras", "shymkent",
    "guangzhou", "antalya", "zadar", "little rock",
    "busan", "tallahassee", "mexico city challenger",
    "francavilla", "sardegna open",
)

# ── Team sports ─────────────────────────────────────────────────────────────

_NBA_TEAMS = (
    "lakers", "celtics", "warriors", "bucks", "suns", "heat", "76ers",
    "sixers", "knicks", "nets", "raptors", "bulls", "cavaliers", "pistons",
    "pacers", "hawks", "hornets", "magic", "wizards", "nuggets", "timberwolves",
    "thunder", "blazers", "trail blazers", "jazz", "kings", "mavericks",
    "rockets", "spurs", "clippers", "pelicans", "grizzlies",
)

_MLB_TEAMS = (
    "yankees", "red sox", "blue jays", "orioles", "rays",
    "white sox", "guardians", "tigers", "royals", "twins",
    "astros", "angels", "athletics", "mariners", "rangers",
    "braves", "marlins", "mets", "phillies", "nationals",
    "cubs", "reds", "brewers", "pirates", "cardinals",
    "diamondbacks", "rockies", "dodgers", "padres", "giants",
)

_NFL_TEAMS = (
    "patriots", "bills", "dolphins", "jets", "ravens", "bengals", "browns",
    "steelers", "texans", "colts", "jaguars", "titans", "broncos", "chiefs",
    "raiders", "chargers", "cowboys", "eagles", "commanders", "bears",
    "lions", "packers", "vikings", "falcons", "panthers", "saints",
    "buccaneers", "49ers", "seahawks",
)

_NHL_TEAMS = (
    "maple leafs", "bruins", "canadiens", "senators",
    "islanders", "devils", "flyers", "penguins", "capitals",
    "red wings", "lightning", "hurricanes", "blue jackets",
    "oilers", "flames", "canucks", "wild", "avalanche",
    "stars", "blues", "predators", "coyotes",
    "sharks", "knights", "kraken",
)

# ── Cricket / soccer / esports ──────────────────────────────────────────────

_CRICKET_RE = re.compile(
    r"\b(cricket|ipl|psl|pakistan super league|indian premier league|"
    r"test match|t20|odi|bbl|big bash)\b",
    re.IGNORECASE,
)

_ESPORTS_RE = re.compile(
    r"\b(lol|league of legends|valorant|dota|counter.?strike|cs:?go|cs2|"
    r"esports world cup|lcs|lec|lck|lpl|"
    r"rocket league|overwatch)\b",
    re.IGNORECASE,
)

_SOCCER_RE = re.compile(
    r"\b(premier league|la liga|bundesliga|serie a|ligue 1|mls|"
    r"champions league|europa league|world cup|uefa|concacaf|"
    r"afcon|copa america|fc\b)",
    re.IGNORECASE,
)

# ── Non-sports archetypes ───────────────────────────────────────────────────

# Crypto - tickers and named coins. Matched first because the price
# question pattern would otherwise route them to `price_threshold`.
_CRYPTO_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|sol|dogecoin|doge|"
    r"litecoin|ltc|cardano|ada|polkadot|dot|chainlink|link|"
    r"avalanche|avax|polygon|matic|ripple|xrp|tron|trx|"
    r"binance coin|bnb|usdt|usdc|stablecoin|altcoin|"
    r"coinbase|binance|kraken|crypto|defi|memecoin)\b",
    re.IGNORECASE,
)

# Stocks / equities. Tickers + common phrasings.
_STOCKS_RE = re.compile(
    r"\b(s&p\s*500|s\&p|nasdaq|dow jones|djia|russell\s*2000|"
    r"nyse|ftse|nikkei|hang seng|stock|share price|stocks|"
    r"tesla|tsla|apple|aapl|microsoft|msft|nvidia|nvda|"
    r"amazon|amzn|google|googl|alphabet|meta|fb|netflix|nflx|"
    r"openai|anthropic|spacex|reddit|rddt|ipo|earnings|"
    r"dividend|market cap)\b",
    re.IGNORECASE,
)

# Macro / monetary. Fed, CPI, GDP, rates, jobs, inflation.
_MACRO_RE = re.compile(
    r"\b(federal reserve|fed\s+(?:rate|cut|hike|chair)|fomc|"
    r"interest rate|rate cut|rate hike|basis points|"
    r"cpi|inflation|gdp|unemployment|jobs report|nonfarm|"
    r"recession|consumer price|producer price|ppi|"
    r"powell|yellen|treasury|yield curve|10[-\s]?year)\b",
    re.IGNORECASE,
)

# FX / commodities.
_FX_COMMODITIES_RE = re.compile(
    r"\b(usd/|eur/|gbp/|jpy/|cad/|chf/|aud/|nzd/|cny/|"
    r"dxy|dollar index|gold|silver|crude oil|brent|wti|"
    r"natural gas|copper|wheat|corn|soybean|coffee|sugar|"
    r"cattle|platinum|palladium|"
    r"\boil\b)\b",
    re.IGNORECASE,
)

# Election - any election-day or candidate-vs-candidate market.
_ELECTION_RE = re.compile(
    r"\b(election|presidential\s+(?:race|election|candidate)|"
    r"senate\s+(?:race|election|seat)|house\s+(?:race|seat|election)|"
    r"primary|caucus|nominee|nomination|"
    r"trump|biden|harris|desantis|vance|haley|kamala|"
    r"vote\s+(?:share|count|tally)|electoral|swing state|"
    r"governor\s+(?:race|election))\b",
    re.IGNORECASE,
)

# Policy / legislation - bills, executive orders, court rulings.
_POLICY_RE = re.compile(
    r"\b(bill\s+(?:pass|signed|vetoed)|executive order|"
    r"supreme court\s+(?:rule|decide|grant|deny)|"
    r"scotus|legislation pass|signed into law|veto|filibuster|"
    r"impeachment|indictment|sentenced|tariff|sanctions\s+(?:on|against))\b",
    re.IGNORECASE,
)

_GEOPOLITICAL_RE = re.compile(
    r"\b(diplomatic|treaty|blockade|ceasefire|"
    r"summit|accord|strait|nuclear deal|peace talks|"
    r"war\b|invasion|missile strike|hostage|"
    r"prime minister|parliament|coup)\b",
    re.IGNORECASE,
)

# Tech releases - product launches, AI capabilities, model releases.
_TECH_RELEASE_RE = re.compile(
    r"\b(gpt[-\s]?\d|claude\s+\d|gemini\s+\d|llama\s+\d|mistral|"
    r"product launch|launch\s+(?:date|by)|release\s+(?:date|by)|"
    r"announce\s+(?:product|launch)|"
    r"vision pro|iphone\s+\d|airpods|tesla cybertruck|"
    r"starship\s+(?:launch|flight)|space[- ]?x launch|"
    r"agi|asi|model release|api launch|beta release)\b",
    re.IGNORECASE,
)

# Awards.
_AWARDS_RE = re.compile(
    r"\b(oscar|academy award|emmy|grammy|tony award|golden globe|"
    r"cannes|palme d'or|nobel prize|pulitzer|booker prize|"
    r"man of the year|person of the year|ballon d'or|mvp|"
    r"best picture|best actor|best actress|best director)\b",
    re.IGNORECASE,
)

# Entertainment - box office, music charts, streaming numbers.
_ENTERTAINMENT_RE = re.compile(
    r"\b(box office|gross\s+\$|opening weekend|"
    r"billboard|number one|spotify\s+(?:streams|chart)|"
    r"netflix\s+(?:show|series)|streaming\s+(?:numbers|chart)|"
    r"taylor swift|beyonce|drake|kanye|rihanna|"
    r"album release|tour\s+(?:dates|stops))\b",
    re.IGNORECASE,
)

# Weather events.
_WEATHER_RE = re.compile(
    r"\b(hurricane|tornado|cyclone|typhoon|blizzard|"
    r"snowfall|rainfall|temperature\s+(?:reach|exceed)|"
    r"heat wave|cold snap|polar vortex|"
    r"category\s+\d|wind speed|noaa)\b",
    re.IGNORECASE,
)

_PRICE_THRESHOLD_RE = re.compile(
    r"(reach|exceed|hit|cross|above|below|over|under)\s+\$[\d,]+",
    re.IGNORECASE,
)

_ACTIVITY_COUNT_RE = re.compile(
    r"(post|tweet|publish)\s+\d+\s*[-–to]+\s*\d+\s+"
    r"(tweets|posts|messages|videos)",
    re.IGNORECASE,
)
_ACTIVITY_COUNT_RANGE_RE = re.compile(
    r"\b(?:more than|fewer than|less than|at least|under|over)\s+\d+\s+"
    r"(tweets|posts|messages|videos)",
    re.IGNORECASE,
)


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    """Substring-match any of `needles` in `text`, case-insensitive."""
    t = text.lower()
    return any(n in t for n in needles)


def classify_archetype(
    question: str,
    *,
    category: Optional[str] = None,
    event_slug: Optional[str] = None,
) -> str:
    """
    Return a fine-grained archetype label for a prediction market.

    Pure function. Falls through to 'binary_event' when nothing else
    matches - so the skip list can include that label to exclude
    the long tail if desired.
    """
    q = (question or "").strip()
    if not q:
        return "binary_event"

    es = (event_slug or "").lower()
    cat = (category or "").lower()

    # Sport-level only. Earlier versions split tennis into
    # qualifier / main_draw / lower_tier and basketball into game / prop,
    # but the user asked for one label per sport ("just based on sport:
    # Tennis, Baseball, Football, Soccer"). The UI renders these as
    # plain sport chips without sub-tier dashes.
    if _TENNIS_QUALIFIER_RE.search(q):
        return "tennis"

    is_tennis_by_brand = bool(_ATP_RE.search(q) or _ATP_RE.search(es) or
                              _WTA_RE.search(q) or _WTA_RE.search(es))
    is_tennis_by_tournament = _has_any(q, _TENNIS_MAIN_DRAW_TOURNAMENTS)
    is_tennis_by_lower_city = any(
        q.lower().startswith(city + ":") or q.lower().startswith(city + ",")
        for city in _TENNIS_LOWER_TIER_CITY_HINTS
    )

    if is_tennis_by_tournament or is_tennis_by_lower_city or is_tennis_by_brand:
        return "tennis"

    # Team sports - one label per sport regardless of game / prop shape.
    if _has_any(q, _NBA_TEAMS):
        return "basketball"
    if _has_any(q, _MLB_TEAMS):
        return "baseball"
    if _has_any(q, _NFL_TEAMS):
        return "football"
    if _has_any(q, _NHL_TEAMS):
        return "hockey"

    # Sport-family regexes.
    if _CRICKET_RE.search(q):
        return "cricket"
    if _ESPORTS_RE.search(q):
        return "esports"
    if _SOCCER_RE.search(q):
        return "soccer"

    # Sports catch-all - the evaluator tagged it as sports but no
    # specific pattern matched.
    if cat == "sports":
        return "sports_other"

    # Order matters here. Crypto / stocks / macro / fx are matched
    # BEFORE the generic price_threshold pattern because most crypto
    # markets are phrased as "Will BTC reach $X by Y" - that would
    # otherwise route to the generic bucket and lose the venue
    # signal the per-archetype multipliers depend on.

    # Crypto.
    if cat == "crypto" or _CRYPTO_RE.search(q):
        return "crypto"

    # Stocks / equities.
    if cat in ("stocks", "equities") or _STOCKS_RE.search(q):
        return "stocks"

    # Macro / monetary.
    if cat in ("macro", "economics", "monetary") or _MACRO_RE.search(q):
        return "macro"

    # FX / commodities.
    if cat in ("commodities", "forex", "fx") or _FX_COMMODITIES_RE.search(q):
        return "fx_commodities"

    # Elections (politics-flavoured but specifically electoral races).
    if _ELECTION_RE.search(q) or cat == "election":
        return "election"

    # Policy events - bills, executive orders, court rulings.
    if cat == "policy" or _POLICY_RE.search(q):
        return "policy_event"

    # Geopolitical (war / treaties / conflict / coups).
    if cat == "geopolitics" or _GEOPOLITICAL_RE.search(q):
        return "geopolitical_event"

    # Tech releases - AI models, product launches.
    if cat in ("tech", "technology", "ai") or _TECH_RELEASE_RE.search(q):
        return "tech_release"

    # Awards.
    if cat == "awards" or _AWARDS_RE.search(q):
        return "awards"

    # Entertainment / pop culture / media.
    if cat in ("entertainment", "music", "movies", "tv") or _ENTERTAINMENT_RE.search(q):
        return "entertainment"

    # Weather events.
    if cat == "weather" or _WEATHER_RE.search(q):
        return "weather_event"

    # Generic numeric patterns (fall through if nothing more specific
    # matched). price_threshold for $X price markets that aren't
    # crypto/stocks/macro; activity_count for "post X-Y tweets" etc.
    if _PRICE_THRESHOLD_RE.search(q):
        return "price_threshold"
    if _ACTIVITY_COUNT_RE.search(q) or _ACTIVITY_COUNT_RANGE_RE.search(q):
        return "activity_count"

    # Politics catch-all that isn't election/policy/geopolitics.
    if cat == "politics":
        return "policy_event"

    return "binary_event"


# Canonical set of archetypes this classifier can produce. Exposed so the
# dashboard/UI can offer a checkbox list without going out of sync. Flat,
# sport-level labels - one label per sport. Legacy fine-grained labels
# ("tennis_qualifier", "basketball_prop", ...) may still appear in the
# historical DB rows; migration 022 rewrites them in place, and
# `canonicalize_archetype()` below collapses any that slip through at
# runtime (belt-and-suspenders so UI + analytics never see a legacy
# label even if a row predates the migration).
ARCHETYPES: tuple[str, ...] = (
    # Sports - one label per sport, no nesting (CLAUDE.md taxonomy rule).
    "tennis",
    "basketball",
    "baseball",
    "football",
    "hockey",
    "cricket",
    "esports",
    "soccer",
    "sports_other",
    # Finance / markets.
    "crypto",
    "stocks",
    "macro",
    "fx_commodities",
    # Politics / society.
    "election",
    "policy_event",
    "geopolitical_event",
    # Tech / culture.
    "tech_release",
    "awards",
    "entertainment",
    # Catch-alls.
    "weather_event",
    "price_threshold",
    "activity_count",
    "binary_event",
)


# Legacy-to-canonical mapping. Source of truth for migration 022 and for
# any runtime collapse (e.g., /api/archetypes discovery). Keep in sync
# with ops/supabase/migrations/022_archetype_consolidation.sql.
LEGACY_ARCHETYPE_MAP: dict[str, str] = {
    "tennis_qualifier":  "tennis",
    "tennis_main_draw":  "tennis",
    "tennis_lower_tier": "tennis",
    "basketball_prop":   "basketball",
    "basketball_game":   "basketball",
    "baseball_game":     "baseball",
    "baseball_prop":     "baseball",
    "football_game":     "football",
    "football_prop":     "football",
    "hockey_game":       "hockey",
    "hockey_prop":       "hockey",
    "esports_match":     "esports",
    "soccer_match":      "soccer",
    "cricket_match":     "cricket",
    "sports_match":      "sports_other",
    "sports_prop":       "sports_other",
    "geopolitical":      "geopolitical_event",
}


def canonicalize_archetype(label: Optional[str]) -> Optional[str]:
    """
    Collapse a possibly-legacy archetype label to its canonical form.

    Returns None for None / empty input. Returns the canonical label when
    `label` is in LEGACY_ARCHETYPE_MAP. Otherwise returns `label`
    unchanged (including labels outside ARCHETYPES, so future classifier
    additions are not silently dropped).

    Keep in sync with the mapping in migration 022.
    """
    if not label:
        return None
    return LEGACY_ARCHETYPE_MAP.get(label, label)
