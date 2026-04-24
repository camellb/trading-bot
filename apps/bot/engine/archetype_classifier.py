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

_GEOPOLITICAL_RE = re.compile(
    r"\b(diplomatic|sanctions|treaty|blockade|ceasefire|"
    r"summit|accord|strait|nuclear deal|peace talks|"
    r"president|prime minister|parliament|election)\b",
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

    # Non-sports archetypes.
    if _PRICE_THRESHOLD_RE.search(q):
        return "price_threshold"
    if _ACTIVITY_COUNT_RE.search(q) or _ACTIVITY_COUNT_RANGE_RE.search(q):
        return "activity_count"

    if cat in ("geopolitics", "politics") or _GEOPOLITICAL_RE.search(q):
        return "geopolitical_event"

    return "binary_event"


# Canonical set of archetypes this classifier can produce. Exposed so the
# dashboard/UI can offer a checkbox list without going out of sync. Flat,
# sport-level labels - one label per sport. Legacy fine-grained labels
# ("tennis_qualifier", "basketball_prop", ...) may still appear in the
# historical DB rows; they are surfaced by `/api/archetypes` via the
# `discovered` field (DISTINCT scan of pm_positions + market_evaluations).
ARCHETYPES: tuple[str, ...] = (
    "tennis",
    "basketball",
    "baseball",
    "football",
    "hockey",
    "cricket",
    "esports",
    "soccer",
    "sports_other",
    "price_threshold",
    "activity_count",
    "geopolitical_event",
    "binary_event",
)
