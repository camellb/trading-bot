"""
Research fetcher — multi-source context for Polymarket markets.

Sources (in priority order):
    1. DuckDuckGo web search — category-specific queries, full page extraction
       via trafilatura for top results.
    2. Wikipedia — entity-anchored lead sections via public API.
    3. ESPN / CoinGecko — structured data for sports and crypto markets.
    4. RSS headlines — keyword-matched from cached news_event_log.
    5. NewsAPI — optional, if NEWSAPI_KEY is set.
    6. Historical base rates — past resolution rates by category from DB.

Keyword extraction uses Gemini Flash (preferred) or Claude (fallback),
with regex heuristics as a last resort.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote_plus

import aiohttp
from sqlalchemy import text

import config
from db.engine import get_engine
from research import live_crypto, live_equity

try:
    from ddgs import DDGS
    _DDGS_AVAILABLE = True
except ImportError:
    _DDGS_AVAILABLE = False

try:
    import trafilatura
    _TRAFILATURA_AVAILABLE = True
except ImportError:
    _TRAFILATURA_AVAILABLE = False


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "how", "in", "is", "it", "of", "on", "or", "that",
    "the", "this", "to", "was", "were", "when", "which", "who", "will",
    "with", "what", "whether", "before", "after", "between", "any", "all",
    "any", "can", "do", "does", "did", "than", "then", "their", "they",
    "these", "those", "more", "less", "there", "about", "over", "under",
    "per", "so", "if", "but", "into", "onto", "out", "up", "down", "new",
    "yes", "no", "be", "been", "not", "also", "one", "two", "three",
}


@dataclass
class ResearchBundle:
    question:        str
    web_search:      list[str]     = field(default_factory=list)
    web_pages:       list[str]     = field(default_factory=list)
    wikipedia:       Optional[str] = None
    news_snippets:   list[str]     = field(default_factory=list)
    base_rate_note:  Optional[str] = None
    external_news:   list[str]     = field(default_factory=list)
    crypto_prices:    Optional[str] = None
    sports_context:   Optional[str] = None
    live_market_data: Optional[str] = None
    keywords:         list[str]     = field(default_factory=list)
    sources:          list[str]     = field(default_factory=list)

    def to_prompt_block(self) -> str:
        """Format everything into a compact context block for Claude."""
        parts: list[str] = []
        if self.live_market_data:
            parts.append(
                f"-- LIVE MARKET DATA (REAL-TIME) --\n"
                f"{self.live_market_data.strip()}"
            )
        if self.web_pages:
            pages = "\n\n".join(self.web_pages[:3])
            parts.append(f"-- Detailed web research --\n{pages}")
        if self.web_search:
            web = "\n".join(f"• {s}" for s in self.web_search[:8])
            parts.append(f"-- Web search results (current) --\n{web}")
        # CoinGecko spot fallback — only if live_market_data did not fire.
        if self.crypto_prices and not self.live_market_data:
            parts.append(f"-- Spot price (CoinGecko) --\n{self.crypto_prices.strip()}")
        if self.sports_context:
            parts.append(f"-- Sports data --\n{self.sports_context.strip()}")
        if self.wikipedia:
            parts.append(f"-- Wikipedia --\n{self.wikipedia.strip()}")
        if self.news_snippets:
            headlines = "\n".join(f"• {h}" for h in self.news_snippets[:8])
            parts.append(f"-- Recent headlines (RSS) --\n{headlines}")
        if self.external_news:
            ext = "\n".join(f"• {h}" for h in self.external_news[:5])
            parts.append(f"-- Recent headlines (NewsAPI) --\n{ext}")
        if self.base_rate_note:
            parts.append(f"-- Historical base rate --\n{self.base_rate_note}")
        if not parts:
            return "(no external research available)"
        return "\n\n".join(parts)


# ── Helpers ──────────────────────────────────────────────────────────────────
_VS_RE = re.compile(r"(.+?)\s+vs\.?\s+(.+?)(?:\s*\?|$)", re.IGNORECASE)

_SPORT_TEAM_HINTS: list[tuple[str, str]] = [
    # NHL
    ("ducks", "Anaheim Ducks NHL"), ("predators", "Nashville Predators NHL"),
    ("kings", "Los Angeles Kings NHL"), ("knights", "Vegas Golden Knights NHL"),
    ("stars", "Dallas Stars NHL"), ("blues", "St. Louis Blues NHL"),
    ("flames", "Calgary Flames NHL"), ("jets", "Winnipeg Jets NHL"),
    ("wild", "Minnesota Wild NHL"), ("avalanche", "Colorado Avalanche NHL"),
    ("oilers", "Edmonton Oilers NHL"), ("canucks", "Vancouver Canucks NHL"),
    ("kraken", "Seattle Kraken NHL"), ("sharks", "San Jose Sharks NHL"),
    ("coyotes", "Arizona Coyotes NHL"), ("panthers", "Florida Panthers NHL"),
    ("lightning", "Tampa Bay Lightning NHL"), ("hurricanes", "Carolina Hurricanes NHL"),
    ("capitals", "Washington Capitals NHL"), ("rangers", "New York Rangers NHL"),
    ("islanders", "New York Islanders NHL"), ("devils", "New Jersey Devils NHL"),
    ("flyers", "Philadelphia Flyers NHL"), ("penguins", "Pittsburgh Penguins NHL"),
    ("bruins", "Boston Bruins NHL"), ("sabres", "Buffalo Sabres NHL"),
    ("senators", "Ottawa Senators NHL"), ("canadiens", "Montreal Canadiens NHL"),
    ("maple leafs", "Toronto Maple Leafs NHL"), ("red wings", "Detroit Red Wings NHL"),
    ("blue jackets", "Columbus Blue Jackets NHL"), ("blackhawks", "Chicago Blackhawks NHL"),
    # NBA
    ("heat", "Miami Heat NBA"), ("lakers", "Los Angeles Lakers NBA"),
    ("celtics", "Boston Celtics NBA"), ("warriors", "Golden State Warriors NBA"),
    ("bucks", "Milwaukee Bucks NBA"), ("nets", "Brooklyn Nets NBA"),
    ("suns", "Phoenix Suns NBA"), ("nuggets", "Denver Nuggets NBA"),
    ("thunder", "Oklahoma City Thunder NBA"), ("mavericks", "Dallas Mavericks NBA"),
    ("spurs", "San Antonio Spurs NBA"), ("rockets", "Houston Rockets NBA"),
    ("clippers", "Los Angeles Clippers NBA"), ("grizzlies", "Memphis Grizzlies NBA"),
    ("timberwolves", "Minnesota Timberwolves NBA"), ("pelicans", "New Orleans Pelicans NBA"),
    ("magic", "Orlando Magic NBA"), ("hawks", "Atlanta Hawks NBA"),
    ("cavaliers", "Cleveland Cavaliers NBA"), ("pacers", "Indiana Pacers NBA"),
    ("raptors", "Toronto Raptors NBA"), ("pistons", "Detroit Pistons NBA"),
    ("hornets", "Charlotte Hornets NBA"), ("wizards", "Washington Wizards NBA"),
    ("trail blazers", "Portland Trail Blazers NBA"), ("jazz", "Utah Jazz NBA"),
    ("knicks", "New York Knicks NBA"), ("sixers", "Philadelphia 76ers NBA"),
    ("bulls", "Chicago Bulls NBA"), ("kings", "Sacramento Kings NBA"),
    # MLB
    ("royals", "Kansas City Royals MLB"), ("pirates", "Pittsburgh Pirates MLB"),
    ("nationals", "Washington Nationals MLB"), ("tigers", "Detroit Tigers MLB"),
    ("angels", "Los Angeles Angels MLB"), ("rays", "Tampa Bay Rays MLB"),
    ("padres", "San Diego Padres MLB"), ("braves", "Atlanta Braves MLB"),
    ("cardinals", "St. Louis Cardinals MLB"), ("cubs", "Chicago Cubs MLB"),
    ("dodgers", "Los Angeles Dodgers MLB"), ("astros", "Houston Astros MLB"),
    ("yankees", "New York Yankees MLB"), ("mets", "New York Mets MLB"),
    ("red sox", "Boston Red Sox MLB"), ("white sox", "Chicago White Sox MLB"),
    ("twins", "Minnesota Twins MLB"), ("mariners", "Seattle Mariners MLB"),
    ("guardians", "Cleveland Guardians MLB"), ("orioles", "Baltimore Orioles MLB"),
    ("reds", "Cincinnati Reds MLB"), ("brewers", "Milwaukee Brewers MLB"),
    ("phillies", "Philadelphia Phillies MLB"), ("giants", "San Francisco Giants MLB"),
    ("rockies", "Colorado Rockies MLB"), ("marlins", "Miami Marlins MLB"),
    ("diamondbacks", "Arizona Diamondbacks MLB"), ("athletics", "Oakland Athletics MLB"),
    ("blue jays", "Toronto Blue Jays MLB"),
    # NFL
    ("chiefs", "Kansas City Chiefs NFL"), ("eagles", "Philadelphia Eagles NFL"),
    ("bills", "Buffalo Bills NFL"), ("dolphins", "Miami Dolphins NFL"),
    ("ravens", "Baltimore Ravens NFL"), ("bengals", "Cincinnati Bengals NFL"),
    ("browns", "Cleveland Browns NFL"), ("steelers", "Pittsburgh Steelers NFL"),
    ("texans", "Houston Texans NFL"), ("colts", "Indianapolis Colts NFL"),
    ("jaguars", "Jacksonville Jaguars NFL"), ("titans", "Tennessee Titans NFL"),
    ("broncos", "Denver Broncos NFL"), ("chargers", "Los Angeles Chargers NFL"),
    ("raiders", "Las Vegas Raiders NFL"), ("packers", "Green Bay Packers NFL"),
    ("bears", "Chicago Bears NFL"), ("lions", "Detroit Lions NFL"),
    ("vikings", "Minnesota Vikings NFL"), ("saints", "New Orleans Saints NFL"),
    ("falcons", "Atlanta Falcons NFL"), ("buccaneers", "Tampa Bay Buccaneers NFL"),
    ("panthers", "Carolina Panthers NFL"),
    ("cowboys", "Dallas Cowboys NFL"), ("commanders", "Washington Commanders NFL"),
    ("seahawks", "Seattle Seahawks NFL"), ("rams", "Los Angeles Rams NFL"),
    ("49ers", "San Francisco 49ers NFL"), ("cardinals", "Arizona Cardinals NFL"),
    ("rangers", "Texas Rangers MLB"), ("giants", "New York Giants NFL"),
]

_LEAGUE_SPORT_MAP = {"NHL": "nhl", "NBA": "nba", "NFL": "nfl", "MLB": "mlb"}


def _detect_sports_matchup(question: str) -> Optional[dict]:
    """Detect 'X vs. Y' sports patterns and return Gemini-compatible metadata."""
    m = _VS_RE.search(question)
    if not m:
        return None
    side_a = m.group(1).strip().rstrip(".")
    side_b = m.group(2).strip().rstrip(".")

    def _lookup(name: str) -> Optional[tuple[str, str]]:
        low = name.lower()
        for key, val in _SPORT_TEAM_HINTS:
            if key in low or low in key:
                return val, val.split()[-1]
        return None

    a_info = _lookup(side_a)
    b_info = _lookup(side_b)
    if not a_info and not b_info:
        return None

    search_terms = []
    teams = []
    league = None
    if a_info:
        search_terms.append(a_info[0])
        teams.append(a_info[0].rsplit(" ", 1)[0])
        league = a_info[1]
    else:
        search_terms.append(side_a)
    if b_info:
        search_terms.append(b_info[0])
        teams.append(b_info[0].rsplit(" ", 1)[0])
        league = b_info[1]
    else:
        search_terms.append(side_b)

    sport = _LEAGUE_SPORT_MAP.get(league or "", "other")
    return {
        "search_terms": search_terms,
        "category": "sports",
        "sport": sport,
        "teams": teams,
    }


def extract_keywords(question: str, max_kw: int = 4) -> list[str]:
    """
    Rough entity/keyword extraction. Grabs Title-Cased phrases first
    (often proper nouns), falls back to noun-like tokens.
    """
    # Title-cased multi-word phrases first.
    phrases = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", question)
    if phrases:
        uniq = []
        for p in phrases:
            if p not in uniq:
                uniq.append(p)
        return uniq[:max_kw]

    # Single capitalised tokens.
    caps = [w for w in re.findall(r"\b[A-Z][a-zA-Z]+\b", question)
            if w.lower() not in STOPWORDS]
    if caps:
        uniq = []
        for w in caps:
            if w not in uniq:
                uniq.append(w)
        return uniq[:max_kw]

    # Fallback: non-stopword tokens.
    tokens = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", question)
              if w.lower() not in STOPWORDS]
    uniq: list[str] = []
    for t in tokens:
        if t not in uniq:
            uniq.append(t)
    return uniq[:max_kw]



# ── Wikipedia ────────────────────────────────────────────────────────────────
async def _fetch_wikipedia(session: aiohttp.ClientSession,
                           keyword: str) -> Optional[str]:
    """Return a short lead-section extract for `keyword`, or None."""
    url = (
        "https://en.wikipedia.org/w/api.php"
        "?action=query&format=json&prop=extracts&exintro=1&explaintext=1"
        f"&redirects=1&titles={quote_plus(keyword)}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception as exc:
        print(f"[research] wiki fetch {keyword!r} failed: {exc}", file=sys.stderr)
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    if not pages:
        return None
    for _, page in pages.items():
        extract = (page.get("extract") or "").strip()
        if extract:
            return extract[:2000]
    return None


# ── News headlines from RSS cache ────────────────────────────────────────────
def _fetch_rss_matches(keywords: list[str], limit: int = 8) -> list[str]:
    """
    Look for recent rss-cached headlines whose title contains any keyword.
    We query news_event_log (populated by the news feed) for the last 48h.
    """
    if not keywords:
        return []
    try:
        like_clauses = []
        params: dict = {}
        for i, kw in enumerate(keywords):
            key = f"kw{i}"
            like_clauses.append(f"headline ILIKE :{key}")
            params[key] = f"%{kw}%"
        clause = " OR ".join(like_clauses)
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                f"SELECT headline, source, logged_at "
                f"FROM news_event_log "
                f"WHERE logged_at >= NOW() - INTERVAL '48 hours' "
                f"  AND ({clause}) "
                f"ORDER BY logged_at DESC "
                f"LIMIT :lim"
            ), {**params, "lim": int(limit)}).fetchall()
        out = []
        for r in rows:
            headline = str(r[0] or "").strip()
            src      = str(r[1] or "").strip()
            if headline:
                tag = f" [{src}]" if src else ""
                out.append(f"{headline}{tag}")
        return out
    except Exception as exc:
        print(f"[research] rss match failed: {exc}", file=sys.stderr)
        return []


# ── NewsAPI (optional) ───────────────────────────────────────────────────────
async def _fetch_newsapi(session: aiohttp.ClientSession,
                         keyword: str,
                         api_key: str,
                         limit: int = 5) -> list[str]:
    """
    Optional: fetch top headlines via newsapi.org (free tier, 100 req/day).
    Only called if NEWSAPI_KEY is set.
    """
    url = (
        "https://newsapi.org/v2/everything"
        f"?q={quote_plus(keyword)}"
        f"&from={(datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()}"
        f"&language=en&sortBy=publishedAt&pageSize={int(limit)}"
        f"&apiKey={api_key}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return []
            data = await r.json()
    except Exception as exc:
        print(f"[research] newsapi {keyword!r} failed: {exc}", file=sys.stderr)
        return []
    arts = (data.get("articles") or [])[:limit]
    return [
        f"{(a.get('title') or '').strip()} [{(a.get('source') or {}).get('name','?')}]"
        for a in arts
        if (a.get("title") or "").strip()
    ]


# ── Base rate lookup ─────────────────────────────────────────────────────────
def _fetch_base_rate(category: Optional[str]) -> Optional[str]:
    """
    For a given market category, compute the historical YES-resolution rate
    across prior resolved Polymarket predictions. Low-n categories produce
    weak signals; we surface the count so Claude can discount.
    """
    if not category:
        return None
    try:
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT COUNT(*) AS n, AVG(resolved_outcome::float) AS rate "
                "FROM predictions "
                "WHERE source = 'polymarket' "
                "  AND category = :cat "
                "  AND resolved_at IS NOT NULL"
            ), {"cat": category}).fetchone()
            n    = int(row[0] or 0)
            rate = float(row[1]) if row[1] is not None else None
        if n < 3 or rate is None:
            return f"category={category}: insufficient history (n={n})"
        return (f"category={category}: historical accuracy {rate*100:.0f}% "
                f"(n={n} resolved predictions)")
    except Exception as exc:
        print(f"[research] base rate failed: {exc}", file=sys.stderr)
        return None


# ── LLM keyword extraction (shared prompt) ─────────────────────────────────
_KW_EXTRACTION_PROMPT = (
    "Given this prediction market question, return a JSON object:\n"
    '{"search_terms": ["precise Wikipedia search terms, e.g. Anaheim Ducks (NHL) not just Ducks"],'
    ' "category": "sports|politics|crypto|economics|entertainment|science|other",'
    ' "sport": "nhl|nba|nfl|mlb|soccer|mma|tennis|other|null",'
    ' "teams": ["full team names if a sports matchup, else empty"]}\n'
    "Rules:\n"
    "- search_terms should be specific enough to find the RIGHT Wikipedia article\n"
    "- For sports: include league name, e.g. 'Anaheim Ducks NHL hockey' not 'Ducks'\n"
    "- For people: include their role, e.g. 'Joe Biden politician' not 'Biden'\n"
    "- Max 3 search terms\n"
    "- Return ONLY the JSON object, no markdown"
)


async def _extract_keywords_llm(
    question: str,
    call_fn,
    label: str,
) -> Optional[dict]:
    """Shared LLM keyword extraction: run call_fn in executor, parse JSON."""
    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, call_fn)
        obj = json.loads(raw)
        if isinstance(obj, dict) and "search_terms" in obj:
            return obj
    except Exception as exc:
        print(f"[research] {label} keyword extraction failed: {exc}", file=sys.stderr)
    return None


_gemini_client = None
_anthropic_kw_client = None


async def _extract_keywords_gemini(question: str) -> Optional[dict]:
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return None
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=gemini_key)

    prompt = f"Question: {question}\n\n{_KW_EXTRACTION_PROMPT}"
    client = _gemini_client

    def _call():
        return client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config={"response_mime_type": "application/json",
                    "max_output_tokens": 300, "temperature": 0.1},
        ).text

    return await _extract_keywords_llm(question, _call, "gemini")


async def _extract_keywords_claude(question: str) -> Optional[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    global _anthropic_kw_client
    if _anthropic_kw_client is None:
        import anthropic
        _anthropic_kw_client = anthropic.Anthropic(api_key=api_key)

    prompt = f"Question: {question}\n\n{_KW_EXTRACTION_PROMPT}"
    client = _anthropic_kw_client

    def _call():
        return client.messages.create(
            model=config.CLAUDE_MODEL, max_tokens=200, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        ).content[0].text

    return await _extract_keywords_llm(question, _call, "claude")


# ── ESPN sports data ────────────────────────────────────────────────────────
_ESPN_SPORTS = {
    "nhl":    "hockey/nhl",
    "nba":    "basketball/nba",
    "nfl":    "football/nfl",
    "mlb":    "baseball/mlb",
    "soccer": "soccer/usa.1",
}


async def _fetch_espn_scoreboard(
    session: aiohttp.ClientSession,
    sport: str,
    teams: list[str],
) -> Optional[str]:
    """Fetch current/upcoming matchup data from ESPN for detected sport."""
    sport_path = _ESPN_SPORTS.get(sport)
    if not sport_path:
        return None
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception as exc:
        print(f"[research] espn fetch failed: {exc}", file=sys.stderr)
        return None

    events = data.get("events") or []
    team_lower = {t.lower() for t in teams}
    lines: list[str] = []
    for ev in events[:20]:
        name = ev.get("name", "")
        competitors = []
        for comp in (ev.get("competitions") or [{}])[0].get("competitors", []):
            team = comp.get("team", {})
            competitors.append({
                "name": team.get("displayName", ""),
                "abbr": team.get("abbreviation", ""),
                "record": comp.get("records", [{}])[0].get("summary", "") if comp.get("records") else "",
                "score": comp.get("score", ""),
                "winner": comp.get("winner", False),
            })
        comp_names = [c["name"].lower() for c in competitors]
        if team_lower and not any(t in cn or cn in t for t in team_lower for cn in comp_names):
            continue
        status = (ev.get("status") or {}).get("type", {}).get("description", "")
        date_str = ev.get("date", "")[:10]
        comp_lines = []
        for c in competitors:
            rec = f" ({c['record']})" if c["record"] else ""
            score = f" — score: {c['score']}" if c["score"] and c["score"] != "0" else ""
            comp_lines.append(f"  {c['name']}{rec}{score}")
        lines.append(f"- {name} [{date_str}, {status}]\n" + "\n".join(comp_lines))

    if not lines:
        standings_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/standings"
        try:
            async with session.get(standings_url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    sdata = await r.json()
                    for group in (sdata.get("children") or []):
                        for entry in (group.get("standings", {}).get("entries") or [])[:30]:
                            team_name = entry.get("team", {}).get("displayName", "").lower()
                            if team_lower and not any(t in team_name or team_name in t for t in team_lower):
                                continue
                            stats = {s["name"]: s.get("displayValue", "") for s in (entry.get("stats") or [])}
                            display = entry.get("team", {}).get("displayName", "")
                            w = stats.get("wins", "?")
                            l = stats.get("losses", "?")
                            lines.append(f"- {display}: {w}W-{l}L (standings)")
        except Exception:
            pass

    if not lines:
        return None
    return f"ESPN {sport.upper()} data:\n" + "\n".join(lines[:5])


# ── Crypto prices (CoinGecko) ───────────────────────────────────────────────
_CRYPTO_RE = re.compile(
    r"\b(btc|bitcoin|eth|ethereum|sol|solana|crypto|cryptocurrency)\b",
    re.IGNORECASE,
)


async def _fetch_crypto_prices(
    session: aiohttp.ClientSession,
    question: str,
) -> Optional[str]:
    """
    If *question* mentions any crypto keyword, fetch live BTC/ETH/SOL
    prices from CoinGecko's free API. Returns a formatted string or None.
    """
    if not _CRYPTO_RE.search(question):
        return None

    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum,solana"
        "&vs_currencies=usd"
        "&include_24hr_change=true"
    )
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception as exc:
        print(f"[research] coingecko fetch failed: {exc}", file=sys.stderr)
        return None

    lines: list[str] = []
    for cg_id, symbol in [
        ("bitcoin", "BTC"),
        ("ethereum", "ETH"),
        ("solana", "SOL"),
    ]:
        info = data.get(cg_id)
        if not info:
            continue
        price = info.get("usd")
        change = info.get("usd_24h_change")
        if price is None:
            continue
        price_str = f"${price:,.0f}" if price >= 10 else f"${price:,.2f}"
        change_str = (
            f" (24h: {change:+.1f}%)" if change is not None else ""
        )
        lines.append(f"- {cg_id.capitalize()} ({symbol}): {price_str}{change_str}")

    if not lines:
        return None
    return "Current crypto prices (live):\n" + "\n".join(lines)


# ── Live market data (OKX crypto + yfinance equity) ─────────────────────────
_EQUITY_RE = re.compile(
    r"\b(spx|s&p\s*500|sp500|spy|nasdaq|ndx|qqq|dow|dji|djia)\b",
    re.IGNORECASE,
)

_CRYPTO_TOKEN_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|sol)\b",
    re.IGNORECASE,
)


def _detect_crypto_symbols(question: str) -> list[str]:
    """Return unique CCXT spot symbols implied by a question."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _CRYPTO_TOKEN_RE.findall(question):
        sym = live_crypto.resolve_symbol(tok)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _detect_equity_tickers(question: str) -> list[str]:
    """Return unique Yahoo tickers implied by a question."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _EQUITY_RE.findall(question):
        # Normalise match ("S&P 500" / "sp500" / "spx" all → ^GSPC).
        key = re.sub(r"\s+", "", raw).upper()
        key = key.replace("S&P500", "SPX").replace("SP500", "SPX")
        tk = live_equity.resolve_ticker(key)
        if tk and tk not in seen:
            seen.add(tk)
            out.append(tk)
    return out


async def _fetch_live_market_data(
    question:            str,
    days_to_resolution:  Optional[float],
) -> tuple[Optional[str], list[str]]:
    """
    Gate live crypto (<2 days) and live equity (<3 days) fetches.

    Returns (block, sources). Both empty when no applicable symbol/ticker
    matches or both adapters fail.
    """
    if days_to_resolution is None:
        return None, []

    blocks: list[str] = []
    sources: list[str] = []

    if days_to_resolution < 2.0:
        symbols = _detect_crypto_symbols(question)
        if symbols:
            results = await asyncio.gather(
                *(live_crypto.get_context(s) for s in symbols),
                return_exceptions=True,
            )
            for sym, ctx in zip(symbols, results):
                if isinstance(ctx, live_crypto.LiveCryptoContext):
                    blocks.append(ctx.to_prompt_block())
                    sources.append(f"okx:{sym}")

    if days_to_resolution < 3.0:
        tickers = _detect_equity_tickers(question)
        if tickers:
            results = await asyncio.gather(
                *(live_equity.get_context(t) for t in tickers),
                return_exceptions=True,
            )
            for tk, ctx in zip(tickers, results):
                if isinstance(ctx, live_equity.LiveEquityContext):
                    blocks.append(ctx.to_prompt_block())
                    sources.append(f"yfinance:{tk}")

    if not blocks:
        return None, sources
    return "\n\n".join(blocks), sources


# ── DuckDuckGo web search ──────────────────────────────────────────────────
def _ddg_search_sync(query: str, max_results: int = 8) -> list[dict]:
    if not _DDGS_AVAILABLE:
        return []
    try:
        with DDGS() as ddg:
            return list(ddg.text(query, max_results=max_results, region="wt-wt") or [])
    except Exception as exc:
        print(f"[research] ddgs search failed for {query[:60]!r}: {exc}",
              file=sys.stderr)
        return []


def _format_ddg_results(results: list[dict], max_snippet: int = 300) -> list[str]:
    seen_titles: set[str] = set()
    out: list[str] = []
    for r in results:
        title = (r.get("title") or "").strip()
        body = (r.get("body") or "").strip()
        href = (r.get("href") or "").strip()
        if not title or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        source = href.split("/")[2] if href.count("/") >= 2 else ""
        if body:
            tag = f" [{source}]" if source else ""
            out.append(f"{title}: {body[:max_snippet]}{tag}")
        else:
            out.append(title)
    return out


# ── Category-specific search strategies ────────────────────────────────────
def _build_search_queries(
    question: str,
    keywords: list[str],
    category: Optional[str],
    sport: Optional[str],
    teams: list[str],
) -> list[str]:
    """
    Build targeted search queries based on market category.
    Different categories need fundamentally different information.
    """
    queries = [question]
    cat = (category or "other").lower()

    if cat == "sports" or sport:
        sport_name = sport or ""
        if teams:
            team_str = " vs ".join(teams[:2])
            queries.append(f"{team_str} odds prediction {sport_name} 2025")
            queries.append(f"{team_str} recent form stats {sport_name}")
            queries.append(f"{team_str} head to head record")
        elif keywords:
            queries.append(f"{' '.join(keywords[:2])} odds prediction today")
            queries.append(f"{' '.join(keywords[:2])} stats form 2025")

    elif cat == "politics":
        if keywords:
            queries.append(f"{' '.join(keywords[:3])} latest polls 2025")
            queries.append(f"{' '.join(keywords[:3])} political analysis forecast")

    elif cat in ("geopolitics", "economics"):
        if keywords:
            queries.append(f"{' '.join(keywords[:3])} latest developments 2025")
            queries.append(f"{' '.join(keywords[:3])} expert analysis outlook")

    elif cat == "crypto":
        if keywords:
            queries.append(f"{' '.join(keywords[:2])} price prediction analysis 2025")
            queries.append(f"{' '.join(keywords[:2])} on-chain metrics sentiment")

    elif cat == "entertainment":
        if keywords:
            queries.append(f"{' '.join(keywords[:3])} ratings predictions odds")

    elif cat == "science":
        if keywords:
            queries.append(f"{' '.join(keywords[:3])} latest research results 2025")

    else:
        if keywords:
            queries.append(f"{' '.join(keywords[:3])} latest news 2025")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower())
            unique.append(q)
    return unique


_SKIP_DOMAINS = {"youtube.com", "twitter.com", "x.com", "reddit.com",
                 "facebook.com", "instagram.com", "tiktok.com"}

# Domains that DDG may surface but we refuse to trafilatura-scrape full pages
# from. Distinct from _SKIP_DOMAINS: these domains may still appear in the
# short web-search snippet list (capped at 300 chars, low leak surface), but
# their full page body reintroduces the market-price anchor we strip from
# polymarket.com. CoinGecko renders Polymarket price widgets directly on
# asset pages — e.g. CoinGecko's BTC page embeds the live Polymarket BTC
# price-ladder with percentages. OKX live data (research/live_crypto.py)
# already provides superior spot/candle/order-book data for short-horizon
# crypto markets, and the CoinGecko /simple/price API (_fetch_crypto_prices)
# still contributes a structured spot number. The full-page scrape adds
# encyclopedia boilerplate Wikipedia covers better, plus the widget leak.
_SCRAPE_BLOCKLIST = {"coingecko.com"}

_CATEGORY_PRIORITY_DOMAINS: dict[str, set[str]] = {
    "sports": {
        "sofascore.com", "flashscore.com", "espn.com", "sportsreference.com",
        "stevegtennis.com", "tennistonic.com", "oddsportal.com",
        "tennisstats.com", "covers.com", "the-odds-api.com",
        "transfermarkt.com", "whoscored.com", "basketball-reference.com",
        "baseball-reference.com", "hockey-reference.com", "fbref.com",
    },
    "politics": {
        "realclearpolitics.com", "fivethirtyeight.com", "natesilver.net",
        "polymarket.com", "metaculus.com", "predictit.org",
        "bbc.com", "reuters.com", "apnews.com", "politico.com",
    },
    "geopolitics": {
        "reuters.com", "apnews.com", "bbc.com", "aljazeera.com",
        "foreignaffairs.com", "cfr.org", "crisisgroup.org",
    },
    "crypto": {
        "coindesk.com", "theblock.co", "messari.io",
        "glassnode.com", "defillama.com", "cryptoquant.com",
    },
}
_ALL_PRIORITY_DOMAINS = frozenset().union(*_CATEGORY_PRIORITY_DOMAINS.values())


def _pick_urls_for_category(
    results: list[dict],
    category: Optional[str],
    max_urls: int = 3,
) -> list[str]:
    cat = (category or "other").lower()
    cat_domains = _CATEGORY_PRIORITY_DOMAINS.get(cat, set())
    all_priority = cat_domains | _ALL_PRIORITY_DOMAINS

    priority: list[str] = []
    others: list[str] = []
    seen: set[str] = set()

    for r in results:
        href = (r.get("href") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        try:
            domain = href.split("/")[2].lower()
        except (IndexError, AttributeError):
            continue
        if any(skip in domain for skip in _SKIP_DOMAINS):
            continue
        if any(block in domain for block in _SCRAPE_BLOCKLIST):
            continue
        if any(prio in domain for prio in all_priority):
            priority.append(href)
        else:
            others.append(href)

    return (priority + others)[:max_urls]


async def _fetch_web_search_raw(
    question: str,
    keywords: list[str] | None = None,
    category: Optional[str] = None,
    sport: Optional[str] = None,
    teams: list[str] | None = None,
) -> list[dict]:
    if not _DDGS_AVAILABLE:
        return []

    queries = _build_search_queries(
        question, keywords or [], category, sport, teams or [],
    )

    loop = asyncio.get_running_loop()

    def _parallel_search():
        all_results: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(len(queries), 4)) as pool:
            futures = [pool.submit(_ddg_search_sync, q, 8) for q in queries]
            for f in futures:
                try:
                    all_results.extend(f.result())
                except Exception:
                    pass
        return all_results

    return await loop.run_in_executor(None, _parallel_search)



def _extract_text_from_html(html: str, max_chars: int = 3000) -> str:
    """Extract main content from HTML using trafilatura, with regex fallback."""
    if _TRAFILATURA_AVAILABLE:
        extracted = trafilatura.extract(html, include_comments=False,
                                        include_tables=True, favor_recall=True)
        if extracted and len(extracted) > 80:
            return extracted[:max_chars]
    # Regex fallback
    t = re.sub(r'<(script|style|nav|footer|header)[^>]*>.*?</\1>', ' ',
               html, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = re.sub(r'&[#\w]+;', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:max_chars]


# Polymarket pages leak the crowd's price into the research bundle — both as
# explicit percentage ladders and as narrative "trader consensus" phrases. The
# evaluator prompt explicitly tells Claude not to anchor on the market price,
# so we strip price-like tokens from polymarket.com scrapes before they enter
# the bundle. Other sources (BBC, Reuters, Wikipedia) may legitimately contain
# percentages (approval ratings, poll numbers) and are left untouched.
_POLYMARKET_SCRUB_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b\d{1,3}%"),
    # Volume displays: "$200,535 Vol.", "$200,535 Volume", and the reverse
    # layouts trafilatura sometimes produces ("Volume\n$200,535").
    re.compile(r"\$[\d,]+\s*Vol(?:ume|\.)?", re.IGNORECASE),
    re.compile(r"\bVol(?:ume|\.)?\s*\n?\s*\$[\d,]+", re.IGNORECASE),
    re.compile(r"\btrader consensus (?:favors|for|against|at|on|is)\b",
               re.IGNORECASE),
    re.compile(r"\bmarket consensus (?:favors|for|against|at|on|is)\b",
               re.IGNORECASE),
    re.compile(r"\bbettors (?:favor|price|think|expect)\b", re.IGNORECASE),
    re.compile(r"\btraders (?:favor|price|think|expect)\b", re.IGNORECASE),
    re.compile(r"\bchance on Polymarket\b", re.IGNORECASE),
    re.compile(r"\bPolymarket (?:price|implied probability|odds)\b",
               re.IGNORECASE),
]

_REDACTED = "[redacted]"


def _scrub_polymarket_text(text: str) -> str:
    """Remove price/consensus leaks from a polymarket.com scrape.

    Applies the price/volume/consensus regexes, then drops any paragraph
    whose tokens are >50% redacted (unreadable garbage that would otherwise
    waste prompt tokens and add noise).
    """
    scrubbed = text
    for pat in _POLYMARKET_SCRUB_PATTERNS:
        scrubbed = pat.sub(_REDACTED, scrubbed)

    kept: list[str] = []
    for para in scrubbed.split("\n"):
        if not para.strip():
            kept.append(para)
            continue
        tokens = para.split()
        redacted = sum(1 for t in tokens if _REDACTED in t)
        if tokens and redacted / len(tokens) > 0.5:
            continue
        kept.append(para)
    return "\n".join(kept)


async def _fetch_page_text(
    session: aiohttp.ClientSession,
    url: str,
    max_chars: int = 3000,
) -> Optional[str]:
    """Fetch a URL and extract its text content."""
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=8),
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"},
        ) as r:
            if r.status != 200:
                return None
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return None
            html = await r.text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[research] page fetch failed {url[:80]}: {exc}", file=sys.stderr)
        return None

    text = _extract_text_from_html(html, max_chars)
    if len(text) < 100:
        return None

    domain = url.split("/")[2] if url.count("/") >= 2 else url
    if "polymarket.com" in domain.lower():
        text = _scrub_polymarket_text(text)
        if len(text) < 100:
            return None
    return f"[{domain}]\n{text}"


async def _fetch_top_pages(
    session: aiohttp.ClientSession,
    search_results: list[dict],
    category: Optional[str] = None,
    max_pages: int = 3,
) -> list[str]:
    """Fetch full text content from the most relevant search result pages."""
    urls = _pick_urls_for_category(search_results, category, max_urls=max_pages)
    if not urls:
        return []

    tasks = [_fetch_page_text(session, url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    pages: list[str] = []
    for res in results:
        if isinstance(res, str):
            pages.append(res)
    return pages


# ── Public entrypoint ────────────────────────────────────────────────────────
async def fetch_research(
    question:            str,
    category:            Optional[str] = None,
    max_wiki_kws:        int           = 2,
    days_to_resolution:  Optional[float] = None,
) -> ResearchBundle:
    """
    Build a research bundle for a market question. Safe to call on the hot
    path — network errors are swallowed and the bundle degrades gracefully.
    """
    bundle = ResearchBundle(question=question)

    # 0. Gemini-powered keyword extraction (domain-aware, replaces regex).
    gemini_meta = await _extract_keywords_gemini(question)
    if gemini_meta:
        bundle.keywords = (gemini_meta.get("search_terms") or [])[:4]
        detected_category = gemini_meta.get("category")
        detected_sport = gemini_meta.get("sport")
        detected_teams = gemini_meta.get("teams") or []
        bundle.sources.append("gemini_keywords")
    else:
        sports_meta = _detect_sports_matchup(question)
        if sports_meta:
            bundle.keywords = sports_meta["search_terms"][:4]
            detected_category = "sports"
            detected_sport = sports_meta.get("sport")
            detected_teams = sports_meta.get("teams") or []
            bundle.sources.append("sports_heuristic")
        else:
            # Try Claude for keyword extraction before falling back to regex
            claude_meta = await _extract_keywords_claude(question)
            if claude_meta:
                bundle.keywords = (claude_meta.get("search_terms") or [])[:4]
                detected_category = claude_meta.get("category")
                detected_sport = claude_meta.get("sport")
                detected_teams = claude_meta.get("teams") or []
                bundle.sources.append("claude_keywords")
            else:
                bundle.keywords = extract_keywords(question)
                detected_category = None
                detected_sport = None
                detected_teams = []

    loop = asyncio.get_running_loop()

    # DB lookups — run off event loop to avoid blocking.
    base_rate_fut = loop.run_in_executor(
        None, _fetch_base_rate, category or detected_category)
    rss_fut = loop.run_in_executor(None, _fetch_rss_matches, bundle.keywords)

    # Web search (runs in thread pool with parallel queries).
    web_raw_task = asyncio.create_task(
        _fetch_web_search_raw(
            question, keywords=bundle.keywords or None,
            category=detected_category, sport=detected_sport,
            teams=detected_teams,
        )
    )

    wiki_task_keywords = bundle.keywords[:max_wiki_kws]
    newsapi_key        = os.environ.get("NEWSAPI_KEY")
    async with aiohttp.ClientSession(
        headers={"User-Agent": "trading-bot/1.0 (research-fetcher)"}
    ) as session:
        # Start wiki/crypto/sports immediately so they overlap with DDG.
        wiki_tasks = {kw: asyncio.create_task(_fetch_wikipedia(session, kw))
                      for kw in wiki_task_keywords}
        crypto_task = asyncio.create_task(_fetch_crypto_prices(session, question))
        live_md_task = asyncio.create_task(
            _fetch_live_market_data(question, days_to_resolution)
        )
        sports_task = None
        if detected_sport and detected_sport != "null" and detected_teams:
            sports_task = asyncio.create_task(
                _fetch_espn_scoreboard(session, detected_sport, detected_teams))
        newsapi_task = None
        if newsapi_key and bundle.keywords:
            newsapi_task = asyncio.create_task(
                _fetch_newsapi(session, bundle.keywords[0], newsapi_key, limit=5))

        # Collect DB results.
        bundle.base_rate_note = await base_rate_fut
        bundle.news_snippets = await rss_fut

        # Wait for web search, then fetch top pages concurrently.
        try:
            web_raw = await web_raw_task
        except Exception as exc:
            print(f"[research] web search failed: {exc}", file=sys.stderr)
            web_raw = []

        if web_raw:
            bundle.web_search = _format_ddg_results(web_raw, max_snippet=300)[:15]
            bundle.sources.append(f"ddg_web:{len(bundle.web_search)}")

        page_task = asyncio.create_task(
            _fetch_top_pages(session, web_raw, category=detected_category, max_pages=3)
        ) if web_raw else None

        # Await all remaining tasks (most already running).
        for kw, task in wiki_tasks.items():
            try:
                res = await task
            except Exception:
                res = None
            if res:
                if bundle.wikipedia:
                    bundle.wikipedia += f"\n\n[{kw}]\n{res}"
                else:
                    bundle.wikipedia = f"[{kw}]\n{res}"
                bundle.sources.append(f"wikipedia:{kw}")

        try:
            crypto_res = await crypto_task
        except Exception:
            crypto_res = None
        if isinstance(crypto_res, str):
            bundle.crypto_prices = crypto_res
            bundle.sources.append("coingecko")

        try:
            live_md_res = await live_md_task
        except Exception:
            live_md_res = (None, [])
        if live_md_res and isinstance(live_md_res, tuple):
            live_block, live_sources = live_md_res
            if live_block:
                bundle.live_market_data = live_block
                bundle.sources.extend(live_sources)

        if sports_task:
            try:
                sports_res = await sports_task
            except Exception:
                sports_res = None
            if isinstance(sports_res, str):
                bundle.sports_context = sports_res
                bundle.sources.append(f"espn:{detected_sport}")

        if newsapi_task:
            try:
                ext = await newsapi_task
            except Exception:
                ext = None
            if isinstance(ext, list) and ext:
                bundle.external_news = ext
                bundle.sources.append("newsapi")

        if page_task:
            try:
                pages = await page_task
            except Exception:
                pages = []
            if pages:
                bundle.web_pages = pages
                bundle.sources.append(f"pages:{len(pages)}")

    if bundle.news_snippets:
        bundle.sources.append(f"rss:{len(bundle.news_snippets)}")
    if bundle.base_rate_note:
        bundle.sources.append("base_rate")

    return bundle


# ── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("question", help="Market question to research")
    ap.add_argument("--category", default=None)
    args = ap.parse_args()

    async def _main():
        b = await fetch_research(args.question, args.category)
        print("Keywords:", b.keywords)
        print("Sources: ", b.sources)
        print()
        print(b.to_prompt_block())

    asyncio.run(_main())
