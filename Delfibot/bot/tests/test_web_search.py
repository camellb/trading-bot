import base64
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parents[1]))

from research import web_search


def _b64url(url: str) -> str:
    return "a1" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


_BING_HTML = f"""
<ol id="b_results">
  <li class="b_algo" data-id iid="SERP.1">
    <link rel="stylesheet" href="https://r.bing.com/x.css" />
    <div class="b_tpcn"><a class="tilk" href="https://example.com">Example</a></div>
    <h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u={_b64url('https://example.com/story')}&amp;ntb=1"
           h="ID=SERP,1">Example <strong>title</strong></a></h2>
    <div class="b_caption"><p class="b_lineclamp2">Example result summary.</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://direct.example.org/page">Direct link</a></h2>
    <div class="b_caption"><p>Second snippet.</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://www.bing.com/ck/a?u=a1notbase64!!">Broken</a></h2>
  </li>
</ol>
"""


class _FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def test_bing_parser_extracts_title_url_and_snippet() -> None:
    parser = web_search.BingResultsParser()
    parser.feed(_BING_HTML)
    parser.close()
    assert parser.results == [
        {"href": "https://example.com/story", "title": "Example title",
         "body": "Example result summary."},
        {"href": "https://direct.example.org/page", "title": "Direct link",
         "body": "Second snippet."},
    ]


def test_unwrap_bing_url_decodes_click_wrapper() -> None:
    wrapped = f"https://www.bing.com/ck/a?!&&p=x&u={_b64url('https://news.site/a?b=1')}&ntb=1"
    assert web_search.unwrap_bing_url(wrapped) == "https://news.site/a?b=1"
    assert web_search.unwrap_bing_url("https://plain.example/x") == "https://plain.example/x"
    assert web_search.unwrap_bing_url("https://www.bing.com/ck/a?u=a1%%%") == ""


def test_breaker_opens_after_threshold_and_recovers() -> None:
    now = [100.0]
    br = web_search.Breaker("t", threshold=2, cooldown_s=30, clock=lambda: now[0])
    assert not br.is_open()
    br.record_failure("one")
    assert not br.is_open()
    br.record_failure("two")
    assert br.is_open()
    now[0] += 29
    assert br.is_open()
    now[0] += 2
    assert not br.is_open()
    br.record_failure("again")
    br.record_success()
    br.record_failure("after success")
    assert not br.is_open()  # success reset the count


def test_search_falls_back_to_bing_when_duckduckgo_fails(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url, params, headers, timeout):
        calls.append(url)
        if url == web_search.DDG_SEARCH_URL:
            raise requests.ConnectTimeout("connect timeout")
        assert url == web_search.BING_SEARCH_URL
        assert params["q"] == "bitcoin news"
        return _FakeResponse(_BING_HTML)

    monkeypatch.setattr(web_search.requests, "get", fake_get)
    ddg = web_search.Breaker("ddg", threshold=3, cooldown_s=60)
    bing = web_search.Breaker("bing", threshold=3, cooldown_s=60)
    backends = [
        web_search.Backend("DuckDuckGo", web_search.duckduckgo_search_sync, ddg),
        web_search.Backend("Bing", web_search.bing_search_sync, bing),
    ]
    results = web_search.search_sync("bitcoin news", 8, backends=backends)
    assert [r["href"] for r in results] == [
        "https://example.com/story", "https://direct.example.org/page",
    ]
    assert calls == [web_search.DDG_SEARCH_URL, web_search.BING_SEARCH_URL]


def test_search_skips_backend_while_breaker_open(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url, params, headers, timeout):
        calls.append(url)
        if url == web_search.DDG_SEARCH_URL:
            return _FakeResponse("forbidden", status=403)
        return _FakeResponse(_BING_HTML)

    monkeypatch.setattr(web_search.requests, "get", fake_get)
    ddg = web_search.Breaker("ddg", threshold=2, cooldown_s=600)
    bing = web_search.Breaker("bing", threshold=2, cooldown_s=600)
    backends = [
        web_search.Backend("DuckDuckGo", web_search.duckduckgo_search_sync, ddg),
        web_search.Backend("Bing", web_search.bing_search_sync, bing),
    ]
    web_search.search_sync("q1", 8, backends=backends)
    web_search.search_sync("q2", 8, backends=backends)
    assert ddg.is_open()
    calls.clear()
    web_search.search_sync("q3", 8, backends=backends)
    assert calls == [web_search.BING_SEARCH_URL]


def test_search_returns_empty_when_every_backend_fails(monkeypatch) -> None:
    def fake_get(url, params, headers, timeout):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(web_search.requests, "get", fake_get)
    backends = [
        web_search.Backend("DuckDuckGo", web_search.duckduckgo_search_sync,
                           web_search.Breaker("d")),
        web_search.Backend("Bing", web_search.bing_search_sync,
                           web_search.Breaker("b")),
    ]
    assert web_search.search_sync("anything", 8, backends=backends) == []


def test_duckduckgo_parser_still_unwraps_redirects() -> None:
    html = """
    <div class="result results_links">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fstory">Example title</a>
      <a class="result__snippet">Example result summary.</a>
    </div>
    """
    parser = web_search.DuckDuckGoResultsParser()
    parser.feed(html)
    parser.close()
    assert parser.results == [{
        "href": "https://example.com/story",
        "title": "Example title",
        "body": "Example result summary.",
    }]
