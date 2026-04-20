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

Keyword extraction uses Claude Haiku (preferred) or Gemini Flash (fallback),
with regex heuristics as a last resort.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote_plus, urlparse

import aiohttp
from sqlalchemy import text

import config
from db.engine import get_engine

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
    crypto_prices:   Optional[str] = None
    sports_context:  Optional[str] = None
    keywords:        list[str]     = field(default_factory=list)
    sources:         list[str]     = field(default_factory=list)
    sentiment_summary: Optional[str] = None
    related_markets: list[dict]    = field(default_factory=list)
    polymarket_context: Optional[str] = None   # scraped from Polymarket event page
    resolution_source_context: Optional[str] = None  # scraped from resolution authority URL
    event_description: Optional[str] = None    # Gamma API event-level description
    quality_score: float = 0.0                 # 0–1 composite research quality score
    resolution_source_score: float = 1.0       # 0–1 resolution source reliability score

    def to_prompt_block(self, max_chars: int = 7500) -> str:
        """Format everything into a compact context block for Claude.

        Sections are ordered by signal-to-noise ratio. The block is
        budget-aware: lower-priority sections are dropped if the total
        would exceed *max_chars* (the evaluator truncates at 8000 anyway,
        but building within budget keeps the highest-value content).
        """
        sections: list[str] = []

        # 1. Polymarket page — highest signal. Trader discussion, analysis,
        #    injury reports, consensus view. Universal across all market types.
        if self.polymarket_context:
            sections.append(f"-- Polymarket event page context --\n{self.polymarket_context.strip()}")
        # 2. Event description from Gamma API.
        if self.event_description:
            sections.append(f"-- Event description --\n{self.event_description.strip()}")
        # 3. Resolution source — authoritative data the market resolves from.
        if self.resolution_source_context:
            sections.append(f"-- Resolution source data --\n{self.resolution_source_context.strip()}")
        # 4. Sports-specific data (ESPN, SofaScore, API-Football, PandaScore).
        if self.sports_context:
            sections.append(f"-- Sports data --\n{self.sports_context.strip()}")
        # 5. Live crypto prices.
        if self.crypto_prices:
            sections.append(f"-- Live market data --\n{self.crypto_prices.strip()}")
        # 6. Full web pages (DDG results extracted via trafilatura).
        if self.web_pages:
            pages = "\n\n".join(self.web_pages[:4])
            sections.append(f"-- Detailed web research --\n{pages}")
        # 7. Web search snippets.
        if self.web_search:
            web = "\n".join(f"• {s}" for s in self.web_search[:8])
            sections.append(f"-- Web search results (current) --\n{web}")
        # 8. Related markets in the same event.
        if self.related_markets:
            rm_lines = []
            for rm in self.related_markets[:5]:
                q = rm.get("question", "?")
                p = rm.get("yes_price", "?")
                vol = rm.get("volume_24h", 0)
                price_str = f"{float(p):.2f}" if isinstance(p, (int, float)) else str(p)
                vol_str = f"${vol:,.0f}" if isinstance(vol, (int, float)) else str(vol)
                rm_lines.append(f"• {q} — YES at {price_str} (24h vol: {vol_str})")
            sections.append(f"-- Related markets in this event --\n" + "\n".join(rm_lines))
        # 9. Recent headlines.
        if self.news_snippets:
            headlines = "\n".join(f"• {h}" for h in self.news_snippets[:6])
            sections.append(f"-- Recent headlines (RSS) --\n{headlines}")
        if self.external_news:
            ext = "\n".join(f"• {h}" for h in self.external_news[:5])
            sections.append(f"-- Recent headlines (NewsAPI) --\n{ext}")
        # 10. Wikipedia (lower priority — general background, not current).
        if self.wikipedia:
            sections.append(f"-- Wikipedia --\n{self.wikipedia.strip()}")
        # 11. Sentiment + base rate (compact).
        if self.sentiment_summary:
            sections.append(f"-- News sentiment --\n{self.sentiment_summary}")
        if self.base_rate_note:
            sections.append(f"-- Historical category base rate --\n{self.base_rate_note}")

        if not sections:
            return "(no external research available)"

        # Build within budget: add sections in priority order, stop when full.
        result_parts: list[str] = []
        total = 0
        for sec in sections:
            sec_len = len(sec) + 2  # account for "\n\n" separator
            if total + sec_len > max_chars and result_parts:
                break
            result_parts.append(sec)
            total += sec_len

        return "\n\n".join(result_parts)


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
            return f"category={category}: insufficient YES/NO resolution history (n={n})"
        return (
            f"category={category}: historical YES resolution rate {rate*100:.0f}% "
            f"(n={n} resolved predictions in this category; use as a weak base rate, not model accuracy)"
        )
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

        # Guard: None or empty → skip parsing entirely
        if not raw or not raw.strip():
            return None

        raw = raw.strip()

        # Strip markdown code fences (```json ... ``` or ``` ... ```)
        if raw.startswith("```"):
            lines = raw.split("\n")
            # Remove first line (```json or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            raw = "\n".join(lines).strip()

        if not raw:
            return None

        obj = json.loads(raw)
        if isinstance(obj, dict) and "search_terms" in obj:
            return obj
    except Exception as exc:
        global _gemini_backoff_until
        exc_str = str(exc)
        if label == "gemini" and ("RESOURCE_EXHAUSTED" in exc_str or "429" in exc_str
                                   or "NOT_FOUND" in exc_str or "404" in exc_str
                                   or "UNAVAILABLE" in exc_str or "503" in exc_str):
            _gemini_backoff_until = time.time() + 1800  # 30 min backoff
            print("[research] Gemini unavailable — backing off 30min, will use Claude fallback",
                  file=sys.stderr)
        elif isinstance(exc, json.JSONDecodeError) and label == "gemini":
            _gemini_backoff_until = time.time() + 1800
            print("[research] Gemini returned malformed JSON — backing off 30min",
                  file=sys.stderr)
        else:
            print(f"[research] {label} keyword extraction failed: {exc}", file=sys.stderr)
    return None


_gemini_client = None
_gemini_backoff_until: Optional[float] = None  # epoch time; skip Gemini until then
_anthropic_kw_client = None


async def _extract_keywords_gemini(question: str) -> Optional[dict]:
    global _gemini_backoff_until
    # Skip Gemini during backoff (quota exhausted)
    if _gemini_backoff_until and time.time() < _gemini_backoff_until:
        return None

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
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json",
                    "max_output_tokens": 300, "temperature": 0.1},
        )
        # Gemini 2.5 Flash: resp.text can be None, empty, or raise when
        # response has no candidates (safety filter, empty finish).
        try:
            return resp.text or ""
        except (ValueError, AttributeError):
            # No candidates in response
            return ""

    result = await _extract_keywords_llm(question, _call, "gemini")
    return result


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
            model=config.CLAUDE_KEYWORD_MODEL, max_tokens=200, temperature=0,
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


# ── API-Football (worldwide football/soccer coverage) ─────────────────────
# Free tier: 100 req/day, all endpoints, 1100+ leagues worldwide.
# Sign up at https://dashboard.api-football.com/ for a free key.
# Set API_FOOTBALL_KEY in .env.

_APIFOOTBALL_TEAM_CACHE: dict[str, int | None] = {}


async def _fetch_apifootball(
    session: aiohttp.ClientSession,
    teams: list[str],
    question: str,
) -> Optional[str]:
    """Fetch match/team data from API-Football for soccer markets."""
    api_key = os.environ.get("API_FOOTBALL_KEY", "")
    if not api_key:
        return None
    # Only for soccer/football markets
    q_lower = question.lower()
    soccer_signals = {"fc ", " fc", "united", "city", "real ", "atletico",
                      "borussia", "bayern", "juventus", "inter ", "ac ",
                      "psg", "lyon", "marseille", "ligue", "premier league",
                      "la liga", "serie a", "bundesliga", "eredivisie",
                      "championship", "soccer", "football", "o/u ", "spread:",
                      "both teams", "win on 202", "end in a draw"}
    if not teams and not any(s in q_lower for s in soccer_signals):
        return None

    headers = {"x-apisports-key": api_key}
    base = "https://v3.football.api-sports.io"
    lines: list[str] = []

    # Strategy: search for fixtures involving these teams
    search_teams = teams[:2] if teams else []
    # Extract team name from question if no teams detected
    if not search_teams:
        # Try "Will X win on DATE?" pattern
        win_match = re.search(r"Will (.+?) (?:win|vs|end)", question, re.IGNORECASE)
        if win_match:
            search_teams = [win_match.group(1).strip()]

    for team_name in search_teams[:2]:
        # Search for team ID (cached)
        if team_name in _APIFOOTBALL_TEAM_CACHE:
            team_id = _APIFOOTBALL_TEAM_CACHE[team_name]
        else:
            try:
                async with session.get(
                    f"{base}/teams", params={"search": team_name},
                    headers=headers, timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status != 200:
                        _APIFOOTBALL_TEAM_CACHE[team_name] = None
                        continue
                    data = await r.json()
                    results = data.get("response") or []
                    team_id = results[0]["team"]["id"] if results else None
                    _APIFOOTBALL_TEAM_CACHE[team_name] = team_id
            except Exception as exc:
                print(f"[research] api-football team search failed: {exc}",
                      file=sys.stderr)
                _APIFOOTBALL_TEAM_CACHE[team_name] = None
                continue

        if not team_id:
            continue

        # Fetch next fixtures for this team
        try:
            async with session.get(
                f"{base}/fixtures",
                params={"team": team_id, "next": "3"},
                headers=headers, timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    continue
                data = await r.json()
                fixtures = data.get("response") or []
        except Exception as exc:
            print(f"[research] api-football fixtures failed: {exc}",
                  file=sys.stderr)
            continue

        for fix in fixtures[:2]:
            league = fix.get("league", {})
            teams_data = fix.get("teams", {})
            home = teams_data.get("home", {})
            away = teams_data.get("away", {})
            fixture = fix.get("fixture", {})
            goals = fix.get("goals", {})
            score_str = ""
            if goals.get("home") is not None:
                score_str = f" | Score: {goals['home']}-{goals['away']}"
            status = fixture.get("status", {}).get("long", "")
            date = fixture.get("date", "")[:10]
            lines.append(
                f"- {home.get('name', '?')} vs {away.get('name', '?')} "
                f"[{league.get('name', '?')}, {league.get('country', '?')}] "
                f"{date} {status}{score_str}"
            )

        # Fetch team standings in their league
        try:
            async with session.get(
                f"{base}/standings",
                params={"team": team_id, "season": "2025"},
                headers=headers, timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    for lg in (data.get("response") or []):
                        for standings_group in (lg.get("league", {}).get("standings") or []):
                            for entry in standings_group:
                                if entry.get("team", {}).get("id") == team_id:
                                    t = entry
                                    all_stats = t.get("all", {})
                                    form = t.get("form", "")
                                    lines.append(
                                        f"- {t['team']['name']}: {t.get('description', '')} "
                                        f"Rank #{t.get('rank', '?')} | "
                                        f"{all_stats.get('played', 0)}P "
                                        f"{all_stats.get('win', 0)}W "
                                        f"{all_stats.get('draw', 0)}D "
                                        f"{all_stats.get('lose', 0)}L | "
                                        f"GF:{all_stats.get('goals', {}).get('for', 0)} "
                                        f"GA:{all_stats.get('goals', {}).get('against', 0)} | "
                                        f"Form: {form[-5:]}"
                                    )
                                    break
        except Exception:
            pass

        # H2H if we have two teams
        if len(search_teams) >= 2 and team_name == search_teams[0]:
            team2_name = search_teams[1]
            team2_id = _APIFOOTBALL_TEAM_CACHE.get(team2_name)
            if team2_id:
                try:
                    async with session.get(
                        f"{base}/fixtures/headtohead",
                        params={"h2h": f"{team_id}-{team2_id}", "last": "5"},
                        headers=headers, timeout=aiohttp.ClientTimeout(total=8),
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            h2h = data.get("response") or []
                            if h2h:
                                h2h_lines = []
                                for fix in h2h:
                                    ht = fix.get("teams", {}).get("home", {})
                                    at = fix.get("teams", {}).get("away", {})
                                    g = fix.get("goals", {})
                                    d = fix.get("fixture", {}).get("date", "")[:10]
                                    h2h_lines.append(
                                        f"  {d}: {ht.get('name','?')} {g.get('home','?')}"
                                        f"-{g.get('away','?')} {at.get('name','?')}"
                                    )
                                lines.append(
                                    f"- Last {len(h2h)} H2H meetings:\n"
                                    + "\n".join(h2h_lines)
                                )
                except Exception:
                    pass

    if not lines:
        return None
    return f"API-Football data:\n" + "\n".join(lines)


# ── PandaScore (esports data) ─────────────────────────────────────────────
# Free tier: 1000 req/hour, covers LoL, CS2, Dota2, Valorant, R6, etc.
# Sign up at https://pandascore.co/ for a free token.
# Set PANDASCORE_TOKEN in .env.

_ESPORT_SLUGS = {
    "counter-strike": "csgo",
    "cs2": "csgo",
    "cs:go": "csgo",
    "valorant": "valorant",
    "dota": "dota2",
    "dota 2": "dota2",
    "league of legends": "lol",
    "lol:": "lol",
    "lol ": "lol",
    "r6": "r6siege",
    "rainbow six": "r6siege",
    "overwatch": "ow",
    "king of glory": "kog",
    "call of duty": "codmw",
    "rocket league": "rl",
    "starcraft": "starcraft-2",
}


def _detect_esport(question: str) -> Optional[str]:
    """Detect esport from market question, return PandaScore videogame slug."""
    q_lower = question.lower()
    for keyword, slug in _ESPORT_SLUGS.items():
        if keyword in q_lower:
            return slug
    # Check for tournament names that imply esports
    esport_tournaments = {
        "iem ": "csgo", "esl ": "csgo", "blast ": "csgo",
        "cct ": "csgo", "pgl ": "dota2",
        "vct ": "valorant", "vcl ": "valorant",
        "lpl ": "lol", "lck ": "lol", "lec ": "lol",
        "lcs ": "lol", "cblol": "lol", "msi ": "lol",
        "worlds ": "lol", "ti ": "dota2",
    }
    for keyword, slug in esport_tournaments.items():
        if keyword in q_lower:
            return slug
    return None


async def _fetch_pandascore(
    session: aiohttp.ClientSession,
    question: str,
    teams: list[str],
) -> Optional[str]:
    """Fetch esports match data from PandaScore API."""
    token = os.environ.get("PANDASCORE_TOKEN", "")
    if not token:
        return None

    videogame = _detect_esport(question)
    if not videogame:
        return None

    headers = {"Authorization": f"Bearer {token}"}
    base = "https://api.pandascore.co"
    lines: list[str] = []

    # Extract team names from the question for searching
    search_teams = list(teams) if teams else []
    if not search_teams:
        vs_match = _VS_RE.search(question)
        if vs_match:
            search_teams = [
                vs_match.group(1).strip().rstrip("."),
                vs_match.group(2).strip().rstrip("."),
            ]

    # Search for upcoming matches in this game
    try:
        params: dict = {
            "filter[videogame]": videogame,
            "filter[status]": "not_started,running",
            "sort": "begin_at",
            "per_page": "10",
        }
        async with session.get(
            f"{base}/matches", params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            matches = await r.json()
    except Exception as exc:
        print(f"[research] pandascore matches failed: {exc}", file=sys.stderr)
        return None

    # Find matches involving our teams
    team_lower = {t.lower() for t in search_teams}
    relevant_matches = []
    for match in matches:
        opponents = match.get("opponents") or []
        opp_names = [
            (o.get("opponent") or {}).get("name", "").lower()
            for o in opponents
        ]
        opp_acronyms = [
            (o.get("opponent") or {}).get("acronym", "").lower()
            for o in opponents
        ]
        all_identifiers = opp_names + opp_acronyms
        if team_lower and any(
            t in ident or ident in t
            for t in team_lower for ident in all_identifiers if ident
        ):
            relevant_matches.append(match)

    # If no team-specific match found, include top matches in that game
    if not relevant_matches:
        relevant_matches = matches[:3]

    for match in relevant_matches[:3]:
        opponents = match.get("opponents") or []
        opp_strs = []
        for o in opponents:
            opp = o.get("opponent") or {}
            name = opp.get("name", "?")
            # Get recent stats if available
            opp_strs.append(name)

        tournament = match.get("tournament") or {}
        serie = match.get("serie") or {}
        league = match.get("league") or {}
        match_type = match.get("match_type", "")
        n_games = match.get("number_of_games", "")
        status = match.get("status", "")
        begin = (match.get("begin_at") or "")[:16]

        # Results if available
        results = match.get("results") or []
        score_str = ""
        if results and any(r.get("score", 0) > 0 for r in results):
            scores = [f"{r.get('score', 0)}" for r in results]
            score_str = f" | Score: {'-'.join(scores)}"

        lines.append(
            f"- {' vs '.join(opp_strs)} [{league.get('name', '')} "
            f"— {tournament.get('name', '')}] "
            f"{match_type} BO{n_games} | {begin} | {status}{score_str}"
        )

    # Search for team stats
    for team_name in search_teams[:2]:
        try:
            async with session.get(
                f"{base}/teams",
                params={
                    "search[name]": team_name,
                    "filter[videogame]": videogame,
                    "per_page": "1",
                },
                headers=headers, timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    team_data = await r.json()
                    if team_data:
                        t = team_data[0]
                        name = t.get("name", "?")
                        acronym = t.get("acronym", "")
                        location = t.get("location", "")
                        players = t.get("players") or []
                        player_names = [p.get("name", "") for p in players[:5]]
                        lines.append(
                            f"- {name} ({acronym}) [{location}] "
                            f"Roster: {', '.join(player_names)}"
                        )
        except Exception:
            pass

    if not lines:
        return None
    return f"PandaScore esports data ({videogame}):\n" + "\n".join(lines)


# ── SofaScore direct scraping (no API key needed) ─────────────────────────
# SofaScore's API returns 403 for direct requests, but trafilatura can fetch
# their web pages successfully (handles TLS fingerprinting/cookies).
# Strategy: DDG site-search to find the match URL, then trafilatura to fetch.

async def _fetch_sofascore_match(
    teams: list[str],
    question: str,
) -> Optional[str]:
    """
    Fetch match data from SofaScore by finding the match page via DDG
    site-search, then extracting structured data with trafilatura.

    This is the primary sports data source — works for football, tennis,
    basketball, hockey, cricket, esports. No API key required.
    """
    if not _TRAFILATURA_AVAILABLE or not _DDGS_AVAILABLE:
        return None

    # Build search query — need at least one team name
    search_teams = list(teams[:2]) if teams else []
    if not search_teams:
        vs_match = _VS_RE.search(question)
        if vs_match:
            search_teams = [
                vs_match.group(1).strip().rstrip("."),
                vs_match.group(2).strip().rstrip("."),
            ]
    if not search_teams:
        # Try "Will X win on DATE?" pattern
        win_match = re.search(r"Will (.+?) (?:win|vs|end)", question, re.IGNORECASE)
        if win_match:
            search_teams = [win_match.group(1).strip()]

    if not search_teams:
        return None

    # DDG site-search for SofaScore match page (subprocess to avoid GIL starvation)
    query = f"site:sofascore.com {' '.join(search_teams[:2])}"

    try:
        raw_results = await _ddg_search_subprocess(query, max_results=3)
    except Exception as exc:
        print(f"[research] sofascore DDG search failed: {exc}", file=sys.stderr)
        return None

    if not raw_results:
        return None

    # Find the best SofaScore URL
    sofascore_url = None
    for r in raw_results:
        href = (r.get("href") or "").strip()
        if "sofascore.com" in href and ("/match/" in href or "/team/" in href
                                         or "sofascore.com/football/" in href
                                         or "sofascore.com/tennis/" in href
                                         or "sofascore.com/basketball/" in href):
            sofascore_url = href
            break
    if not sofascore_url:
        # Fall back to first result even if pattern doesn't match
        for r in raw_results:
            href = (r.get("href") or "").strip()
            if "sofascore.com" in href:
                sofascore_url = href
                break

    if not sofascore_url:
        return None

    # Fetch page with trafilatura (handles TLS fingerprinting that blocks curl)
    try:
        html = await asyncio.wait_for(
            loop.run_in_executor(None, trafilatura.fetch_url, sofascore_url),
            timeout=15,
        )
    except (asyncio.TimeoutError, Exception) as exc:
        print(f"[research] sofascore fetch failed: {exc}", file=sys.stderr)
        return None

    if not html:
        return None

    lines: list[str] = []

    # Extract __NEXT_DATA__ JSON for structured data (embedded in the page)
    try:
        next_data_match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if next_data_match:
            next_data = json.loads(next_data_match.group(1))
            props = next_data.get("props", {}).get("pageProps", {})
            event = props.get("event") or {}

            # Teams
            home = event.get("homeTeam", {})
            away = event.get("awayTeam", {})
            if home.get("name") and away.get("name"):
                lines.append(f"Match: {home['name']} vs {away['name']}")

            # Tournament / league
            tournament = event.get("tournament", {})
            if tournament.get("name"):
                cat = tournament.get("category", {}).get("name", "")
                lines.append(f"Competition: {cat} — {tournament['name']}")

            # Score / status
            home_score = event.get("homeScore", {})
            away_score = event.get("awayScore", {})
            status = event.get("status", {})
            if home_score.get("current") is not None:
                lines.append(
                    f"Score: {home_score['current']}-{away_score.get('current', '?')} "
                    f"({status.get('description', '')})"
                )

            # Standings positions from team data
            for label, team in [("Home", home), ("Away", away)]:
                pos = team.get("position")
                if pos:
                    lines.append(f"{team.get('name', label)}: League position #{pos}")

            # Round info
            rnd = event.get("roundInfo", {})
            if rnd.get("round"):
                lines.append(f"Round: {rnd.get('name', '')} {rnd['round']}")

            # Venue
            venue = event.get("venue", {})
            if venue.get("stadium", {}).get("name"):
                cap = venue["stadium"].get("capacity", "")
                cap_str = f" (capacity: {cap:,})" if isinstance(cap, int) else ""
                lines.append(f"Venue: {venue['stadium']['name']}{cap_str}")
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Also extract readable text via trafilatura for additional context
    try:
        text = await loop.run_in_executor(
            None, trafilatura.extract, html,
        )
        if text and len(text) > 100:
            # Truncate to key info
            lines.append(f"Page content:\n{text[:2000]}")
    except Exception:
        pass

    if not lines:
        return None
    return f"SofaScore ({sofascore_url.split('/')[2]}):\n" + "\n".join(lines)


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


# ── DuckDuckGo web search ──────────────────────────────────────────────────
def _ddg_search_sync(query: str, max_results: int = 8,
                     timeout: int = 30) -> list[dict]:
    if not _DDGS_AVAILABLE:
        return []
    try:
        with DDGS(timeout=timeout) as ddg:
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


# ── Archetype-specific search hints ───────────────────────────────────────
ARCHETYPE_SEARCH_HINTS: dict[str, list[str]] = {
    "sports_match": ["odds", "injury report", "head to head record", "recent form"],
    "sports_prop": ["stats", "season average", "last 5 games", "projection"],
    "price_threshold": ["price prediction", "forecast", "technical analysis", "analyst target"],
    "crypto": ["price prediction", "on-chain data", "whale activity", "market sentiment"],
    "geopolitical": ["latest news", "diplomatic talks", "official statement", "analyst assessment"],
    "macro_release": ["forecast", "consensus estimate", "previous reading", "economist survey"],
    "entertainment": ["predictions", "odds", "critics", "early reviews"],
    "scientific": ["study results", "trial data", "peer review", "expert opinion"],
    "legal": ["court ruling", "legal analysis", "precedent", "timeline"],
    "weather": ["forecast", "meteorological data", "climate model", "historical average"],
}


# ── Category-specific search strategies ────────────────────────────────────
def _build_search_queries(
    question: str,
    keywords: list[str],
    category: Optional[str],
    sport: Optional[str],
    teams: list[str],
    archetype: Optional[str] = None,
) -> list[str]:
    """
    Build targeted search queries based on market category and archetype.
    Different categories need fundamentally different information.
    When an archetype is known, append targeted hints for deeper research.
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

    # Archetype-specific hint queries — append 1-2 targeted search terms
    # to get more relevant results for the market type.
    if archetype and keywords:
        hints = ARCHETYPE_SEARCH_HINTS.get(archetype, [])
        kw_prefix = " ".join(keywords[:2])
        for hint in hints[:2]:
            queries.append(f"{kw_prefix} {hint}")

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
        "coingecko.com", "coindesk.com", "theblock.co", "messari.io",
        "glassnode.com", "defillama.com", "cryptoquant.com",
    },
}
_ALL_PRIORITY_DOMAINS = frozenset().union(*_CATEGORY_PRIORITY_DOMAINS.values())


def _pick_urls_for_category(
    results: list[dict],
    category: Optional[str],
    max_urls: int = 5,
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
    archetype: Optional[str] = None,
) -> list[dict]:
    """Run DDG searches in a subprocess to avoid GIL starvation.

    The duckduckgo_search library uses primp (a Rust HTTP client) which
    holds the Python GIL during HTTP requests.  In a thread pool this
    blocks the asyncio event loop and prevents ALL timeouts from firing
    — the process hangs until the HTTP request finishes.

    Running in a subprocess gives DDG its own GIL so the parent event
    loop stays responsive.
    """
    if not _DDGS_AVAILABLE:
        return []

    queries = _build_search_queries(
        question, keywords or [], category, sport, teams or [],
        archetype=archetype,
    )

    # Serialize queries as JSON, run a small helper script in a subprocess.
    # The helper does the threaded DDG search and writes JSON results to stdout.
    helper_code = (
        "import sys, json\n"
        "from concurrent.futures import ThreadPoolExecutor, wait as cf_wait\n"
        "queries = json.loads(sys.argv[1])\n"
        "try:\n"
        "    from duckduckgo_search import DDGS\n"
        "except ImportError:\n"
        "    print('[]'); sys.exit(0)\n"
        "def search(q):\n"
        "    try:\n"
        "        with DDGS(timeout=6) as ddg:\n"
        "            return list(ddg.text(q, max_results=6, region='wt-wt') or [])\n"
        "    except Exception:\n"
        "        return []\n"
        "pool = ThreadPoolExecutor(max_workers=min(len(queries), 4))\n"
        "futs = [pool.submit(search, q) for q in queries]\n"
        "done, _ = cf_wait(futs, timeout=10)\n"
        "results = []\n"
        "for f in done:\n"
        "    try: results.extend(f.result())\n"
        "    except: pass\n"
        "pool.shutdown(wait=False, cancel_futures=True)\n"
        "print(json.dumps(results))\n"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", helper_code, json.dumps(queries),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=13,
        )
        if proc.returncode != 0:
            return []
        return json.loads(stdout.decode())
    except asyncio.TimeoutError:
        print("[research] DDG subprocess timed out (13s)", file=sys.stderr)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return []
    except Exception as exc:
        print(f"[research] DDG subprocess failed: {exc}", file=sys.stderr)
        return []


async def _ddg_search_subprocess(
    query: str, max_results: int = 3, timeout_s: int = 8,
) -> list[dict]:
    """Run a single DDG query in a subprocess to avoid GIL starvation.

    Same rationale as _fetch_web_search_raw — primp holds the GIL during
    HTTP requests, blocking the asyncio event loop.  This lightweight
    wrapper handles single queries (e.g. SofaScore site-search).
    """
    if not _DDGS_AVAILABLE:
        return []

    helper_code = (
        "import sys, json\n"
        "try:\n"
        "    from duckduckgo_search import DDGS\n"
        "except ImportError:\n"
        "    print('[]'); sys.exit(0)\n"
        "try:\n"
        f"    with DDGS(timeout={timeout_s}) as ddg:\n"
        f"        r = list(ddg.text(sys.argv[1], max_results={max_results}, region='wt-wt') or [])\n"
        "    print(json.dumps(r))\n"
        "except Exception:\n"
        "    print('[]')\n"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", helper_code, query,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s + 5,
        )
        if proc.returncode != 0:
            return []
        return json.loads(stdout.decode())
    except asyncio.TimeoutError:
        print(f"[research] DDG single-query subprocess timed out ({timeout_s+5}s)",
              file=sys.stderr)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return []
    except Exception as exc:
        print(f"[research] DDG single-query subprocess failed: {exc}",
              file=sys.stderr)
        return []


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

    # trafilatura is CPU-intensive — run off the event loop to avoid blocking
    # the HTTP server and heartbeat.
    loop = asyncio.get_running_loop()
    text = await loop.run_in_executor(None, _extract_text_from_html, html, max_chars)
    if len(text) < 100:
        return None

    domain = url.split("/")[2] if url.count("/") >= 2 else url
    return f"[{domain}]\n{text}"


async def _fetch_top_pages(
    session: aiohttp.ClientSession,
    search_results: list[dict],
    category: Optional[str] = None,
    max_pages: int = 5,
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


# ── News sentiment keyword counting ──────────────────────────────────────────
_POSITIVE_KEYWORDS = [
    "approved", "passed", "confirmed", "agreed", "deal", "win", "wins",
    "surge", "record high", "breakthrough", "signed", "ratified", "victory",
    "succeeds", "succeeded", "advances", "upgraded", "bullish",
]
_NEGATIVE_KEYWORDS = [
    "rejected", "failed", "denied", "blocked", "crash", "collapse",
    "delay", "postpone", "withdraw", "withdrawn", "vetoed", "sanctions",
    "downgrade", "bearish", "losses", "defeated", "canceled", "cancelled",
    "suspended", "stalled",
]


def _compute_sentiment_summary(bundle: ResearchBundle) -> Optional[str]:
    """
    Simple keyword-based sentiment signal from headlines and web snippets.
    No LLM required — just counts positive/negative signal words across
    all collected text sources. Returns a summary string or None if no
    signals found.
    """
    # Gather all text sources into one lowercase blob for scanning.
    texts: list[str] = []
    for snippet in bundle.web_search:
        texts.append(snippet.lower())
    for page in bundle.web_pages:
        # Only scan first 500 chars of each page (headlines/leads).
        texts.append(page[:500].lower())
    for headline in bundle.news_snippets:
        texts.append(headline.lower())
    for headline in bundle.external_news:
        texts.append(headline.lower())

    if not texts:
        return None

    combined = " ".join(texts)
    total_sources = (len(bundle.web_search) + len(bundle.news_snippets)
                     + len(bundle.external_news) + len(bundle.web_pages))

    pos_count = sum(1 for kw in _POSITIVE_KEYWORDS if kw in combined)
    neg_count = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in combined)

    if pos_count == 0 and neg_count == 0:
        return None

    return (
        f"Sentiment: {pos_count} positive / {neg_count} negative "
        f"signals from {total_sources} sources"
    )


# ── Polymarket page scraping (universal context) ─────────────────────────────
# Every Polymarket market has a page at polymarket.com/event/{event_slug}.
# This page contains trader discussion, community analysis, and context that
# is relevant for ANY market type. This is the single most universal research
# source — it works for sports, politics, crypto, entertainment, everything.

def _urllib_fetch(url: str, timeout: int = 12) -> Optional[str]:
    """Fetch a URL using urllib — works when trafilatura/aiohttp fail."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


async def _fetch_polymarket_page(
    event_slug: str | None,
    market_slug: str | None,
) -> Optional[str]:
    """
    Scrape the Polymarket event page for community context and discussion.
    Returns extracted text or None.
    """
    # Build URL: prefer event page (has all related markets + discussion),
    # fall back to individual market page.
    url = None
    if event_slug:
        url = f"https://polymarket.com/event/{event_slug}"
    elif market_slug:
        url = f"https://polymarket.com/market/{market_slug}"
    if not url:
        return None

    loop = asyncio.get_running_loop()

    # Polymarket blocks trafilatura but accepts standard urllib requests.
    try:
        html = await asyncio.wait_for(
            loop.run_in_executor(None, _urllib_fetch, url),
            timeout=15,
        )
    except (asyncio.TimeoutError, Exception) as exc:
        print(f"[research] polymarket page fetch failed: {exc}", file=sys.stderr)
        return None

    if not html:
        return None

    lines: list[str] = []

    # Extract __NEXT_DATA__ for structured event/market data.
    try:
        next_data_match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if next_data_match:
            next_data = json.loads(next_data_match.group(1))
            props = next_data.get("props", {}).get("pageProps", {})

            # Event-level data.
            event = props.get("event") or {}
            if event.get("description"):
                desc = event["description"].strip()
                if len(desc) > 100:  # skip short boilerplate
                    lines.append(f"Event context: {desc[:2000]}")

            # Market-level comments/discussion data if available.
            comments = props.get("comments") or event.get("comments") or []
            if isinstance(comments, list) and comments:
                comment_lines = []
                for c in comments[:8]:
                    body = ""
                    if isinstance(c, dict):
                        body = (c.get("body") or c.get("content") or
                                c.get("text") or "").strip()
                    elif isinstance(c, str):
                        body = c.strip()
                    if body and len(body) > 20:
                        comment_lines.append(f"  - {body[:300]}")
                if comment_lines:
                    lines.append("Trader discussion:\n" + "\n".join(comment_lines))

            # Resolution info from structured data.
            for mkt in (event.get("markets") or []):
                res_src = (mkt.get("resolutionSource") or "").strip()
                if res_src and "http" in res_src:
                    lines.append(f"Resolution source: {res_src}")
                    break
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Also extract readable text via trafilatura (if available) or regex.
    try:
        text = await loop.run_in_executor(
            None, _extract_text_from_html, html, 2500,
        )
        if text and len(text) > 150:
            # Avoid duplicating what we already have from __NEXT_DATA__.
            if not lines:
                lines.append(f"Page content:\n{text[:2500]}")
            elif len(text) > 500:
                # Only add text content if it's substantially different from
                # what __NEXT_DATA__ gave us (i.e. it has discussion/analysis).
                existing = " ".join(lines).lower()
                text_lower = text[:500].lower()
                new_words = set(text_lower.split()) - set(existing.split())
                if len(new_words) > 20:
                    lines.append(f"Additional context:\n{text[:1500]}")
    except Exception:
        pass

    if not lines:
        return None
    return "\n".join(lines)


# ── Resolution source scraping ───────────────────────────────────────────────
# Many Polymarket markets have a `resolutionSource` URL pointing to the
# authoritative data source (e.g., ESPN for sports, Reuters for politics,
# CoinGecko for crypto). Scraping this URL gives the most relevant context
# possible — it's literally what the market will resolve from.

async def _fetch_resolution_source(
    session: aiohttp.ClientSession,
    resolution_source: str | None,
    keywords: list[str],
) -> Optional[str]:
    """
    Fetch and extract text from the market's resolution source URL.
    Returns extracted text or None.
    """
    if not resolution_source or not resolution_source.startswith("http"):
        return None

    # Some resolution sources are just domain roots (e.g., "https://www.espn.com/").
    # For those, do a targeted search on that domain instead.
    is_root_url = resolution_source.rstrip("/").count("/") <= 2

    if is_root_url and keywords:
        # Search within the resolution source domain for specific content.
        # Use subprocess to avoid GIL starvation from primp.
        domain = resolution_source.split("/")[2]
        query = f"site:{domain} {' '.join(keywords[:3])}"
        try:
            raw_results = await _ddg_search_subprocess(query, max_results=3)
        except Exception:
            return None

        if not raw_results:
            return None

        # Fetch the first relevant result.
        for r in raw_results:
            href = (r.get("href") or "").strip()
            if domain in href:
                page_text = await _fetch_page_text(session, href, max_chars=3000)
                if page_text:
                    return f"[Resolution source: {domain}]\n{page_text}"
        return None

    # Direct URL — fetch the page.
    page_text = await _fetch_page_text(session, resolution_source, max_chars=3000)
    if page_text:
        return page_text

    # If aiohttp fails (e.g., Cloudflare), try trafilatura.
    if _TRAFILATURA_AVAILABLE:
        loop = asyncio.get_running_loop()
        try:
            html = await asyncio.wait_for(
                loop.run_in_executor(None, trafilatura.fetch_url, resolution_source),
                timeout=12,
            )
            if html:
                text = await loop.run_in_executor(
                    None, _extract_text_from_html, html, 3000,
                )
                if text and len(text) > 100:
                    domain = resolution_source.split("/")[2] if "/" in resolution_source else resolution_source
                    return f"[{domain}]\n{text}"
        except Exception:
            pass

    return None


# ── Page relevance filtering ─────────────────────────────────────────────────
# DDG often returns pages that are technically "results" but contain zero
# relevant information (e.g., Google support pages, login walls, generic
# index pages). This filter drops pages that don't mention any keywords.

def _is_page_relevant(page_text: str, keywords: list[str], question: str) -> bool:
    """
    Quick relevance check: does the page mention any of the keywords
    or significant words from the question?
    """
    if not page_text or len(page_text) < 100:
        return False

    text_lower = page_text.lower()

    # Check for keyword matches.
    if keywords:
        for kw in keywords:
            if kw.lower() in text_lower:
                return True

    # Check for significant words from the question.
    question_words = {
        w.lower() for w in re.findall(r"[A-Za-z]{4,}", question)
        if w.lower() not in STOPWORDS
    }
    matches = sum(1 for w in question_words if w in text_lower)
    # Need at least 2 question-word matches to be considered relevant.
    return matches >= 2


# ── Related markets from Gamma API ──────────────────────────────────────────
async def _fetch_related_markets(
    session: aiohttp.ClientSession,
    market_id: str,
    event_slug: str | None,
) -> tuple[list[dict], Optional[str]]:
    """
    Fetch other active markets in the same Polymarket event group.
    Uses the /events endpoint (returns correct results; the /markets
    endpoint's event_slug parameter is unreliable).
    Returns (related_markets, event_description):
      - up to 5 related markets with question, price, and volume
      - event-level description text (often contains useful context)
    Skips the current market_id to avoid self-reference.
    """
    if not event_slug:
        return [], None
    url = (
        f"https://gamma-api.polymarket.com/events"
        f"?slug={quote_plus(event_slug)}"
    )
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return [], None
            data = await r.json()
    except Exception as exc:
        print(f"[research] related markets fetch failed: {exc}", file=sys.stderr)
        return [], None

    if not isinstance(data, list) or not data:
        return [], None

    event_obj = data[0] if data else {}

    # Extract event-level description — often contains resolution criteria,
    # background context, and other info not in individual market descriptions.
    event_description = (event_obj.get("description") or "").strip()
    if len(event_description) < 50:
        event_description = None  # too short to be useful
    elif len(event_description) > 2000:
        event_description = event_description[:2000] + "…"

    # The events endpoint wraps markets inside each event object.
    markets = event_obj.get("markets") or []

    related: list[dict] = []
    for m in markets:
        mid = str(m.get("id") or m.get("condition_id") or "")
        if mid == market_id:
            continue
        question = (m.get("question") or "").strip()
        if not question:
            continue
        # Parse YES price from outcomePrices field (JSON string like "[\"0.5\",\"0.5\"]")
        yes_price = None
        raw_prices = m.get("outcomePrices")
        if raw_prices:
            try:
                if isinstance(raw_prices, str):
                    prices = json.loads(raw_prices)
                else:
                    prices = raw_prices
                if isinstance(prices, list) and len(prices) > 0:
                    yes_price = float(prices[0])
            except (json.JSONDecodeError, ValueError, IndexError):
                pass
        if yes_price is None:
            continue
        vol_24h = 0
        try:
            vol_24h = float(m.get("volume24hr") or m.get("volume_num") or 0)
        except (TypeError, ValueError):
            pass
        related.append({
            "question": question[:200],
            "yes_price": yes_price,
            "volume_24h": vol_24h,
        })
        if len(related) >= 5:
            break

    return related, event_description


# ── Research quality scoring ────────────────────────────────────────────────

def score_research_quality(bundle: ResearchBundle) -> float:
    """Compute a 0–1 quality score for a research bundle.

    Rewards breadth and depth of collected context. Higher scores mean
    the evaluator has richer information to work with.
    """
    score = 0.0

    # Substantive sections: +0.15 each
    for section in [
        bundle.polymarket_context,
        bundle.event_description,
        bundle.resolution_source_context,
        bundle.wikipedia,
    ]:
        if section and len(section) > 200:
            score += 0.15

    # Web pages with real content: +0.10 each, up to 3
    substantive_pages = sum(
        1 for p in bundle.web_pages if len(p) > 200
    )
    score += 0.10 * min(substantive_pages, 3)

    # News snippets: +0.05 each, up to 5
    score += 0.05 * min(len(bundle.news_snippets), 5)

    # Live data bonus: +0.10 if any live-data source is present
    if bundle.sports_context or bundle.crypto_prices:
        score += 0.10

    return min(score, 1.0)


def score_resolution_source(source_url: Optional[str]) -> float:
    """Score resolution source reliability based on domain.

    Uses PM_RESOLUTION_SOURCE_SCORES from config for known domains,
    falls back to PM_RESOLUTION_SOURCE_DEFAULT_SCORE for unknown ones.
    """
    if not source_url or not source_url.startswith("http"):
        return config.PM_RESOLUTION_SOURCE_DEFAULT_SCORE

    try:
        hostname = urlparse(source_url).hostname or ""
    except Exception:
        return config.PM_RESOLUTION_SOURCE_DEFAULT_SCORE

    # Strip leading 'www.' for matching
    hostname = hostname.lower().removeprefix("www.")

    scores = config.PM_RESOLUTION_SOURCE_SCORES
    if hostname in scores:
        return scores[hostname]

    # Check if hostname is a subdomain of a known domain
    # e.g., 'api.coingecko.com' should match 'coingecko.com'
    for domain, domain_score in scores.items():
        if hostname.endswith("." + domain):
            return domain_score

    return config.PM_RESOLUTION_SOURCE_DEFAULT_SCORE


# ── Public entrypoint ────────────────────────────────────────────────────────
async def fetch_research(
    question:     str,
    category:     Optional[str] = None,
    max_wiki_kws: int           = 2,
    archetype:    Optional[str] = None,
    market_id:    Optional[str] = None,
    event_slug:   Optional[str] = None,
    market_slug:  Optional[str] = None,
    resolution_source: Optional[str] = None,
) -> ResearchBundle:
    """
    Build a research bundle for a market question. Safe to call on the hot
    path — network errors are swallowed and the bundle degrades gracefully.

    archetype:          market archetype for targeted search hints (e.g. 'sports_match').
    market_id:          Polymarket market ID, used to exclude self from related markets.
    event_slug:         event group slug, used to fetch related markets in the same event.
    market_slug:        individual market slug for Polymarket page scraping.
    resolution_source:  URL of the resolution authority (e.g., ESPN, Reuters).
    """
    bundle = ResearchBundle(question=question)

    # 0. LLM keyword extraction: Claude Haiku (primary), Gemini (fallback).
    claude_meta = await _extract_keywords_claude(question)
    if claude_meta:
        bundle.keywords = (claude_meta.get("search_terms") or [])[:4]
        detected_category = claude_meta.get("category")
        detected_sport = claude_meta.get("sport")
        detected_teams = claude_meta.get("teams") or []
        bundle.sources.append("claude_keywords")
    else:
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
                bundle.keywords = extract_keywords(question)
                detected_category = None
                detected_sport = None
                detected_teams = []

    loop = asyncio.get_running_loop()

    # DB lookups — run off event loop to avoid blocking.
    base_rate_fut = loop.run_in_executor(
        None, _fetch_base_rate, category or detected_category)
    rss_fut = loop.run_in_executor(None, _fetch_rss_matches, bundle.keywords)

    wiki_task_keywords = bundle.keywords[:max_wiki_kws]
    newsapi_key        = os.environ.get("NEWSAPI_KEY")
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            resolver=aiohttp.resolver.ThreadedResolver(),
            ttl_dns_cache=300,
        ),
        headers={"User-Agent": "trading-bot/1.0 (research-fetcher)"},
    ) as session:
        # Fetch related markets FIRST — their questions often contain
        # opponent names and matchup details that the primary question lacks
        # (e.g., "Will FC Metz win?" → related: "FC Metz vs. Paris FC: O/U 2.5").
        related_markets: list[dict] = []
        gamma_event_description: Optional[str] = None
        if event_slug:
            try:
                related_markets, gamma_event_description = await _fetch_related_markets(
                    session, market_id or "", event_slug)
            except Exception:
                related_markets = []
                gamma_event_description = None

        # Enrich keywords and team detection from related market questions.
        # Many single-team markets ("Will X win on DATE?") don't name the
        # opponent, but the related markets do (via "X vs Y" variants).
        if related_markets and (detected_category == "sports" or not detected_teams):
            for rm in related_markets:
                rm_q = rm.get("question", "")
                rm_match = _VS_RE.search(rm_q)
                if rm_match:
                    rm_a = rm_match.group(1).strip().rstrip(".")
                    rm_b = rm_match.group(2).strip().rstrip(".")
                    # Add opponent to detected_teams if not already present.
                    existing_lower = {t.lower() for t in detected_teams}
                    for team in [rm_a, rm_b]:
                        if team.lower() not in existing_lower:
                            detected_teams.append(team)
                            existing_lower.add(team.lower())
                    # If we didn't have teams before, also add a better keyword.
                    if len(bundle.keywords) < 4:
                        vs_kw = f"{rm_a} vs {rm_b}"
                        if vs_kw.lower() not in {k.lower() for k in bundle.keywords}:
                            bundle.keywords.insert(0, vs_kw)
                    break  # one matchup extraction is enough

        # Web search (runs in thread pool with parallel queries).
        web_raw_task = asyncio.create_task(
            _fetch_web_search_raw(
                question, keywords=bundle.keywords or None,
                category=detected_category, sport=detected_sport,
                teams=detected_teams, archetype=archetype,
            )
        )

        # Start wiki/crypto/sports/esports immediately so they overlap with DDG.
        wiki_tasks = {kw: asyncio.create_task(_fetch_wikipedia(session, kw))
                      for kw in wiki_task_keywords}
        crypto_task = asyncio.create_task(_fetch_crypto_prices(session, question))
        sports_task = None
        if detected_sport and detected_sport != "null" and detected_teams:
            sports_task = asyncio.create_task(
                _fetch_espn_scoreboard(session, detected_sport, detected_teams))
        # API-Football: worldwide football/soccer data (free, 100 req/day).
        apifootball_task = asyncio.create_task(
            _fetch_apifootball(session, detected_teams, question))
        # PandaScore: esports data (free, 1000 req/hour).
        pandascore_task = asyncio.create_task(
            _fetch_pandascore(session, question, detected_teams))
        # SofaScore: direct page scraping via trafilatura (no API key needed).
        # Covers football, tennis, basketball, hockey, cricket, esports.
        sofascore_task = asyncio.create_task(
            _fetch_sofascore_match(detected_teams, question))
        newsapi_task = None
        if newsapi_key and bundle.keywords:
            newsapi_task = asyncio.create_task(
                _fetch_newsapi(session, bundle.keywords[0], newsapi_key, limit=5))

        # Polymarket page scraping — universal context for ALL market types.
        # This is the single most reliable research source since every market
        # has a Polymarket page with community discussion and context.
        polymarket_page_task = asyncio.create_task(
            _fetch_polymarket_page(event_slug, market_slug))

        # Resolution source — fetch the authoritative data source the market
        # resolves from (e.g., ESPN for sports, Reuters for politics).
        resolution_source_task = asyncio.create_task(
            _fetch_resolution_source(session, resolution_source, bundle.keywords))

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
            _fetch_top_pages(session, web_raw, category=detected_category, max_pages=5)
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

        if sports_task:
            try:
                sports_res = await sports_task
            except Exception:
                sports_res = None
            if isinstance(sports_res, str):
                bundle.sports_context = sports_res
                bundle.sources.append(f"espn:{detected_sport}")

        # API-Football — worldwide football/soccer.
        try:
            apifb_res = await apifootball_task
        except Exception:
            apifb_res = None
        if isinstance(apifb_res, str):
            # Append to sports_context (or create it)
            if bundle.sports_context:
                bundle.sports_context += f"\n\n{apifb_res}"
            else:
                bundle.sports_context = apifb_res
            bundle.sources.append("api-football")

        # PandaScore — esports.
        try:
            panda_res = await pandascore_task
        except Exception:
            panda_res = None
        if isinstance(panda_res, str):
            if bundle.sports_context:
                bundle.sports_context += f"\n\n{panda_res}"
            else:
                bundle.sports_context = panda_res
            bundle.sources.append("pandascore")

        # SofaScore — direct page scraping (no API key).
        try:
            sofa_res = await sofascore_task
        except Exception:
            sofa_res = None
        if isinstance(sofa_res, str):
            if bundle.sports_context:
                bundle.sports_context += f"\n\n{sofa_res}"
            else:
                bundle.sports_context = sofa_res
            bundle.sources.append("sofascore")

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
                # Filter out irrelevant pages — DDG often returns garbage
                # (Google support pages, login walls, generic index pages).
                filtered = [
                    p for p in pages
                    if _is_page_relevant(p, bundle.keywords, question)
                ]
                if filtered:
                    bundle.web_pages = filtered
                    bundle.sources.append(f"pages:{len(filtered)}")
                elif pages:
                    # Keep at most 2 unfiltered pages as a fallback — better
                    # than nothing, but clearly less valuable.
                    bundle.web_pages = pages[:2]
                    bundle.sources.append(f"pages:{len(pages[:2])}(unfiltered)")

        # Polymarket page — universal context (community discussion, event context).
        try:
            pm_page_res = await polymarket_page_task
        except Exception:
            pm_page_res = None
        if isinstance(pm_page_res, str) and len(pm_page_res) > 50:
            bundle.polymarket_context = pm_page_res
            bundle.sources.append("polymarket_page")

        # Resolution source — authoritative data the market resolves from.
        try:
            res_src_result = await resolution_source_task
        except Exception:
            res_src_result = None
        if isinstance(res_src_result, str) and len(res_src_result) > 50:
            bundle.resolution_source_context = res_src_result
            bundle.sources.append("resolution_source")

        # Related markets — already fetched at the top of the function.
        if related_markets:
            bundle.related_markets = related_markets
            bundle.sources.append(f"related_markets:{len(related_markets)}")

        # Event description from Gamma API — often has resolution criteria
        # and background context not in individual market descriptions.
        if gamma_event_description:
            bundle.event_description = gamma_event_description
            bundle.sources.append("event_description")

        # Fallback search pass: if the first search returned very few results,
        # try alternative keyword formulations to fill in gaps.
        if len(bundle.web_search) < 3 and bundle.keywords:
            try:
                alt_query = f"{' '.join(bundle.keywords[:3])} latest update analysis"
                alt_raw = await _fetch_web_search_raw(
                    alt_query, keywords=bundle.keywords,
                    category=detected_category, sport=detected_sport,
                    teams=detected_teams, archetype=archetype,
                )
                if alt_raw:
                    alt_formatted = _format_ddg_results(alt_raw, max_snippet=300)
                    # Merge without duplicates.
                    existing = set(s.lower() for s in bundle.web_search)
                    for item in alt_formatted:
                        if item.lower() not in existing:
                            bundle.web_search.append(item)
                            existing.add(item.lower())
                    bundle.web_search = bundle.web_search[:15]
                    # Fetch additional pages from fallback results if we
                    # still have room.
                    if len(bundle.web_pages) < 5 and alt_raw:
                        extra_pages = await _fetch_top_pages(
                            session, alt_raw,
                            category=detected_category,
                            max_pages=5 - len(bundle.web_pages),
                        )
                        if extra_pages:
                            bundle.web_pages.extend(extra_pages)
                    bundle.sources.append("ddg_fallback")
            except Exception as exc:
                print(f"[research] fallback search failed: {exc}",
                      file=sys.stderr)

    if bundle.news_snippets:
        bundle.sources.append(f"rss:{len(bundle.news_snippets)}")
    if bundle.base_rate_note:
        bundle.sources.append("base_rate")

    # Sentiment summary from collected headlines and snippets.
    try:
        bundle.sentiment_summary = _compute_sentiment_summary(bundle)
        if bundle.sentiment_summary:
            bundle.sources.append("sentiment")
    except Exception as exc:
        print(f"[research] sentiment computation failed: {exc}",
              file=sys.stderr)

    # Score research quality and resolution source reliability.
    bundle.quality_score = score_research_quality(bundle)
    bundle.resolution_source_score = score_resolution_source(resolution_source)

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
