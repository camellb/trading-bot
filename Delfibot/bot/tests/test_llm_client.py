import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from engine import llm_client


def _resp(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=None)


def test_extract_anthropic_text_skips_thinking_blocks() -> None:
    # Sonnet 5 runs adaptive thinking by default, so the first content
    # block is a thinking block with no `.text`. 2026-09-12 outage:
    # `response.content[0].text` raised AttributeError 335 times.
    resp = _resp([
        SimpleNamespace(type="thinking", thinking="", signature="x"),
        SimpleNamespace(type="text", text='{"probability_yes": 0.62}'),
    ])
    assert llm_client.extract_anthropic_text(resp) == '{"probability_yes": 0.62}'


def test_extract_anthropic_text_joins_multiple_text_blocks() -> None:
    resp = _resp([
        SimpleNamespace(type="text", text="part one"),
        SimpleNamespace(type="redacted_thinking", data="..."),
        SimpleNamespace(type="text", text="part two"),
    ])
    assert llm_client.extract_anthropic_text(resp) == "part one\npart two"


def test_extract_anthropic_text_raises_when_no_text() -> None:
    resp = _resp([SimpleNamespace(type="thinking", thinking="")], stop_reason="max_tokens")
    with pytest.raises(llm_client.EmptyLLMResponse) as ei:
        llm_client.extract_anthropic_text(resp)
    assert "max_tokens" in str(ei.value)


class _Exc(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def test_classify_cooldown_quota_billing_auth_model() -> None:
    quota = _Exc("429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
                 "generate_content_free_tier_requests, limit: 20", status_code=429)
    billing = _Exc("Error code: 400 - Your credit balance is too low to access the "
                   "Anthropic API.", status_code=400)
    auth = _Exc("Error code: 401 - invalid x-api-key", status_code=401)
    model = _Exc("Error code: 404 - model: claude-x not_found", status_code=404)
    plain_429 = _Exc("429 rate limit exceeded, retry shortly", status_code=429)
    outage = _Exc("503 UNAVAILABLE overloaded", status_code=503)

    assert llm_client.classify_cooldown(quota)[1] == "quota"
    assert llm_client.classify_cooldown(quota)[0] >= 600
    assert llm_client.classify_cooldown(billing)[1] == "billing"
    assert llm_client.classify_cooldown(auth)[1] == "auth"
    assert llm_client.classify_cooldown(model)[1] == "model"
    assert llm_client.classify_cooldown(plain_429) is None
    assert llm_client.classify_cooldown(outage) is None


def test_client_cooldown_bookkeeping(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: now[0])
    client = llm_client.LLMClient()
    conn = {"id": "conn_a", "provider": "gemini", "label": "Google"}

    assert client.cooldown_remaining(conn) == 0.0
    client.note_failure(conn, _Exc("503 unavailable", status_code=503))
    assert client.cooldown_remaining(conn) == 0.0  # transient: no cooldown

    client.note_failure(conn, _Exc("Your credit balance is too low", status_code=400))
    remaining = client.cooldown_remaining(conn)
    assert remaining > 0
    assert client.cooldown_reason(conn) == "billing"

    now[0] += remaining + 1
    assert client.cooldown_remaining(conn) == 0.0

    client.note_failure(conn, _Exc("429 RESOURCE_EXHAUSTED quota", status_code=429))
    assert client.cooldown_remaining(conn) > 0
    client.note_success(conn)
    assert client.cooldown_remaining(conn) == 0.0


def test_short_error_truncates_provider_bodies() -> None:
    body = "x" * 5000
    s = llm_client.short_error(_Exc(body))
    assert len(s) < 400
    assert s.startswith("_Exc: ")
