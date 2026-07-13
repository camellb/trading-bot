import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from research import fetcher


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_duckduckgo_search_parses_result_redirects(monkeypatch) -> None:
    html = """
    <div class="result results_links">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fstory">Example title</a>
      <a class="result__snippet">Example result summary.</a>
    </div>
    """

    def fake_get(url, params, headers, timeout):
        assert url == fetcher._DDG_SEARCH_URL
        assert params == {"q": "example query"}
        assert headers == fetcher._DDG_SEARCH_HEADERS
        assert timeout == fetcher._DDG_SEARCH_TIMEOUT
        return _FakeResponse(html)

    monkeypatch.setattr(fetcher.requests, "get", fake_get)

    assert fetcher._ddg_search_sync("example query") == [{
        "href": "https://example.com/story",
        "title": "Example title",
        "body": "Example result summary.",
    }]


def test_duckduckgo_parser_skips_incomplete_results() -> None:
    parser = fetcher._DuckDuckGoResultsParser()
    parser.feed('<a class="result__a" href="https://example.com"></a>')
    parser.close()

    assert parser.results == []
