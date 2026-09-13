"""Keyless web search backends for the research pipeline.

Two HTML endpoints that need no API key, tried in order:

    1. DuckDuckGo  https://html.duckduckgo.com/html/
    2. Bing        https://www.bing.com/search

Each backend sits behind a small circuit breaker. When a backend fails
`threshold` times in a row (connect timeout, 403, 5xx, parse failure)
it is skipped for `cooldown_s` seconds and the next backend is tried
immediately. Without this, an unreachable search host costs every
market a full connect timeout per query: on 2026-09-13 the sidecar
log showed 7,114 consecutive DuckDuckGo failures and zero successes
(html + lite endpoints both unreachable from the user's ISP while
Bing answered in 0.3 s), and each market paid ~30 s for no research.

Result shape is shared by every backend and by the callers in
research/fetcher.py (`_format_ddg_results`, `_pick_urls_for_category`):

    {"href": "https://example.com/story", "title": "...", "body": "..."}
"""

from __future__ import annotations

import base64
import binascii
import sys
import threading
import time
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

import requests

# ── Shared request settings ──────────────────────────────────────────────────

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
)

DDG_SEARCH_URL = "https://html.duckduckgo.com/html/"
DDG_SEARCH_TIMEOUT = (3, 10)
DDG_SEARCH_HEADERS = {"User-Agent": BROWSER_USER_AGENT}

BING_SEARCH_URL = "https://www.bing.com/search"
BING_SEARCH_TIMEOUT = (3, 10)
BING_SEARCH_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Circuit breaker ──────────────────────────────────────────────────────────

class Breaker:
    """Consecutive-failure circuit breaker with a fixed cooldown.

    `is_open()` is True while the backend should be skipped. A success
    closes it and resets the failure count. Thread-safe: research runs
    search queries from several executor threads at once.
    """

    def __init__(self, name: str, *, threshold: int = 3,
                 cooldown_s: float = 600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._last_log = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return self._clock() < self._open_until

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0

    def record_failure(self, detail: str = "") -> None:
        with self._lock:
            self._failures += 1
            now = self._clock()
            tripped = False
            if self._failures >= self.threshold:
                self._open_until = now + self.cooldown_s
                self._failures = 0
                tripped = True
            # At most one stderr line per minute per backend. The old
            # per-query print was the single largest contributor to a
            # 53 MB sidecar log (2026-09-13).
            if tripped or now - self._last_log >= 60.0:
                self._last_log = now
                state = (f"paused for {int(self.cooldown_s)}s"
                         if tripped else "failure")
                print(f"[research] {self.name} search {state}: {detail[:160]}",
                      file=sys.stderr)

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0


# ── DuckDuckGo ───────────────────────────────────────────────────────────────

class DuckDuckGoResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._result: dict | None = None
        self._capture: str | None = None
        self._capture_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._append_result()
            self._result = {
                "href": unwrap_duckduckgo_url(attr_map.get("href") or ""),
                "title": "",
                "body": "",
            }
            self._capture = "title"
            self._capture_tag = tag
        elif self._result and "result__snippet" in classes:
            self._capture = "body"
            self._capture_tag = tag

    def handle_data(self, data: str) -> None:
        if self._result is not None and self._capture is not None:
            self._result[self._capture] += data

    def handle_endtag(self, tag: str) -> None:
        if self._capture_tag == tag:
            self._capture = None
            self._capture_tag = None

    def close(self) -> None:
        super().close()
        self._append_result()

    def _append_result(self) -> None:
        if self._result is None:
            return
        self._result["title"] = " ".join(self._result["title"].split())
        self._result["body"] = " ".join(self._result["body"].split())
        if self._result["href"] and self._result["title"]:
            self.results.append(self._result)
        self._result = None


def unwrap_duckduckgo_url(raw_url: str) -> str:
    if raw_url.startswith("//"):
        raw_url = f"https:{raw_url}"
    parsed = urlsplit(raw_url)
    if parsed.netloc.endswith("duckduckgo.com"):
        redirect_url = parse_qs(parsed.query).get("uddg", [""])[0]
        if redirect_url:
            return redirect_url
    return raw_url


def duckduckgo_search_sync(query: str, max_results: int = 8) -> list[dict]:
    """One DuckDuckGo HTML query. Raises requests.RequestException on
    transport / HTTP failure so the caller's breaker can count it."""
    response = requests.get(
        DDG_SEARCH_URL,
        params={"q": query},
        headers=DDG_SEARCH_HEADERS,
        timeout=DDG_SEARCH_TIMEOUT,
    )
    response.raise_for_status()
    parser = DuckDuckGoResultsParser()
    parser.feed(response.text)
    parser.close()
    return parser.results[:max_results]


# ── Bing ─────────────────────────────────────────────────────────────────────

class BingResultsParser(HTMLParser):
    """Parses Bing's HTML SERP.

    Result markup (verified 2026-09-13):

        <li class="b_algo">
          <h2><a href="https://www.bing.com/ck/a?...&u=a1aHR0cHM6Ly9...">Title</a></h2>
          <div class="b_caption"><p class="b_lineclamp2">Snippet</p></div>
        </li>

    The `href` is a click-tracking wrapper whose `u` parameter is
    "a1" + urlsafe-base64 of the real URL; `unwrap_bing_url` decodes it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._depth = 0          # nesting depth inside the current <li class="b_algo">
        self._result: dict | None = None
        self._in_h2 = False
        self._in_caption = False
        self._capture: str | None = None
        self._capture_tag: str | None = None
        self._capture_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        if self._result is None:
            if tag == "li" and "b_algo" in classes:
                self._result = {"href": "", "title": "", "body": ""}
                self._depth = 1
            return
        self._depth += 1
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._result["href"]:
            self._result["href"] = unwrap_bing_url(attr_map.get("href") or "")
            self._capture = "title"
            self._capture_tag = "a"
            self._capture_depth = self._depth
        elif tag == "div" and "b_caption" in classes:
            self._in_caption = True
        elif tag == "p" and self._in_caption and not self._result["body"]:
            self._capture = "body"
            self._capture_tag = "p"
            self._capture_depth = self._depth

    def handle_data(self, data: str) -> None:
        if self._result is not None and self._capture is not None:
            self._result[self._capture] += data

    def handle_endtag(self, tag: str) -> None:
        if self._result is None:
            return
        if self._capture is not None and tag == self._capture_tag \
                and self._depth == self._capture_depth:
            self._capture = None
            self._capture_tag = None
        if tag == "h2":
            self._in_h2 = False
        if tag == "div" and self._in_caption and self._depth <= 2:
            self._in_caption = False
        self._depth -= 1
        if self._depth <= 0:
            self._append_result()

    def close(self) -> None:
        super().close()
        self._append_result()

    def _append_result(self) -> None:
        if self._result is None:
            return
        r = self._result
        r["title"] = " ".join(r["title"].split())
        r["body"] = " ".join(r["body"].split())
        if r["href"].startswith("http") and r["title"]:
            self.results.append(r)
        self._result = None
        self._depth = 0
        self._in_h2 = False
        self._in_caption = False
        self._capture = None
        self._capture_tag = None


def unwrap_bing_url(raw_url: str) -> str:
    """Decode Bing's /ck/a click-tracking wrapper to the destination URL."""
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        u = parse_qs(parsed.query).get("u", [""])[0]
        if u.startswith("a1"):
            payload = u[2:]
            payload += "=" * (-len(payload) % 4)
            try:
                decoded = base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                return ""
            if decoded.startswith("http"):
                return decoded
        return ""
    return raw_url


def bing_search_sync(query: str, max_results: int = 8) -> list[dict]:
    """One Bing HTML query. Raises requests.RequestException on
    transport / HTTP failure so the caller's breaker can count it."""
    response = requests.get(
        BING_SEARCH_URL,
        params={"q": query, "count": str(max(max_results, 10)), "setlang": "en"},
        headers=BING_SEARCH_HEADERS,
        timeout=BING_SEARCH_TIMEOUT,
    )
    response.raise_for_status()
    parser = BingResultsParser()
    parser.feed(response.text)
    parser.close()
    return parser.results[:max_results]


# ── Backend chain ────────────────────────────────────────────────────────────

class Backend:
    def __init__(self, name: str, fn: Callable[[str, int], list[dict]],
                 breaker: Breaker) -> None:
        self.name = name
        self.fn = fn
        self.breaker = breaker


ddg_breaker = Breaker("DuckDuckGo")
bing_breaker = Breaker("Bing")

BACKENDS: list[Backend] = [
    Backend("DuckDuckGo", duckduckgo_search_sync, ddg_breaker),
    Backend("Bing", bing_search_sync, bing_breaker),
]


def search_sync(query: str, max_results: int = 8,
                backends: Optional[list[Backend]] = None) -> list[dict]:
    """Run one query through the backend chain.

    Tries each backend whose breaker is closed, in order, and returns
    the first non-empty result list. Transport / HTTP failures count
    against that backend's breaker and fall through to the next one;
    an empty result set falls through too (a 0.3 s Bing call is cheap
    and catches queries DuckDuckGo has no index for). Never raises.
    """
    for backend in (backends if backends is not None else BACKENDS):
        if backend.breaker.is_open():
            continue
        try:
            results = backend.fn(query, max_results)
        except requests.RequestException as exc:
            backend.breaker.record_failure(f"{type(exc).__name__}: {exc}")
            continue
        except Exception as exc:  # parser / decode surprises
            backend.breaker.record_failure(f"{type(exc).__name__}: {exc}")
            continue
        backend.breaker.record_success()
        if results:
            return results
    return []


def reset_breakers() -> None:
    for backend in BACKENDS:
        backend.breaker.reset()
