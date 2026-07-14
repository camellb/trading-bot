"""
Role-routing LLM client with primary/backup failover and Anthropic
prompt caching.

The bot makes two kinds of LLM calls, each its own "use case":

  1. forecaster — the per-market forecast (polymarket_evaluator). Huge
     system prompt (~1500 tokens, reusable verbatim across every market
     in a scan) plus a short per-market user message. Fired 50+ times
     per scan cycle.
  2. search — research keyword extraction + bundle curation
     (research/fetcher) and headline summarisation (feeds/news_feed).
     Short prompts, no reusable system block, cheap model preferred.

Connections + role wiring live in secrets.json and are resolved through
engine.user_config. The user adds an API key for ANY provider, picks the
model per entry, and assigns which connection serves which use case as
primary or backup. This client:

  • Resolves the ordered connection chain for a use case via
    resolve_llm_chain(use_case) — primary first, then backup. 'search'
    falls back to the forecaster chain when no search role is set, so a
    single key still powers research.
  • Dispatches each connection by provider "kind":
      anthropic -> anthropic SDK (messages API, prompt caching)
      gemini    -> google-genai SDK (generate_content)
      openai    -> openai SDK against the connection's base_url
                   (chat.completions). Covers OpenAI, xAI/Grok,
                   DeepSeek, Mistral, Groq, OpenRouter, and any custom
                   OpenAI-compatible endpoint.
  • On a connection failure (auth error, rate limit, 5xx, network
    error, unexpected exception), moves to the next connection in the
    chain. Returns the first successful text, or None when the whole
    chain is exhausted (callers treat None as "skip this market").
  • Uses each connection's own model id (resolved with the provider
    default as a fallback), so the forecaster can stay on Claude Sonnet
    4 while search runs on a cheaper model — or whatever the user picks.
  • Caches one SDK client per (kind, api_key, base_url) so a multi-key
    setup doesn't rebuild a client per call. reset() drops every cached
    client — call after a credential save so the next request builds
    fresh against the now-current connections.
  • When cache_system=True (forecaster only), sends the Anthropic system
    prompt with cache_control:ephemeral so subsequent calls within the
    5-minute TTL pay 0.1x input on the cached prefix. No-op for the
    other providers.

Module-level singleton: `get_llm()` returns a process-wide instance;
`reset_llm()` clears its cached SDK clients. The hot-reload path in
local_api (after writing connections) calls `reset_llm()` so changes
take effect without a daemon restart.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any, Optional

import anthropic

from engine import llm_providers as _providers
from engine.user_config import resolve_llm_chain


# ── Module-level singleton ──────────────────────────────────────────────────

_SINGLETON_LOCK = threading.Lock()
_singleton: Optional["LLMClient"] = None


def get_llm() -> "LLMClient":
    """Process-wide LLM client. Idempotent. Construction is cheap."""
    global _singleton
    if _singleton is None:
        with _SINGLETON_LOCK:
            if _singleton is None:
                _singleton = LLMClient()
    return _singleton


def reset_llm() -> None:
    """Drop cached provider SDK clients.

    Call after a connection save so the next request constructs fresh
    clients against the now-current keys. Safe to call even if no
    singleton exists yet — it's a no-op in that case.
    """
    if _singleton is not None:
        _singleton.reset()


# ── Client ──────────────────────────────────────────────────────────────────

class LLMClient:
    """
    Multi-provider client with role-based primary/backup failover.

    Construction is lazy: the underlying SDK clients are built only when
    the first request needs them and cached by (kind, api_key, base_url).
    `reset()` nulls the cache; the next call reconstructs against
    whatever the connection store says.
    """

    def __init__(self) -> None:
        self._clients: dict[tuple, Any] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._clients = {}

    async def call(
        self,
        *,
        system: Optional[str],
        user: str,
        max_tokens: int,
        temperature: float = 1.0,
        cache_system: bool = False,
        use_case: str = "forecaster",
    ) -> Optional[str]:
        """
        Walk the use case's connection chain, return the first response.

        Returns None only if every connection in the chain failed or no
        connection is wired for the use case. Callers should treat None
        as "skip" rather than retrying indefinitely.
        """
        chain = resolve_llm_chain(use_case)
        if not chain:
            print(f"[llm_client] no usable '{use_case}' connection "
                  f"configured (add one in Settings -> Connections)",
                  file=sys.stderr)
            if use_case == "forecaster":
                from engine.runtime_alerts import report_failure
                report_failure(
                    "forecast_provider",
                    "No forecast provider is configured.",
                )
            return None

        last_exc: Optional[Exception] = None
        for i, conn in enumerate(chain):
            kind = _providers.provider_kind(conn.get("provider"))
            label = "primary" if i == 0 else f"backup{i}"
            try:
                if kind == "anthropic":
                    response = await self._call_anthropic(
                        conn, system, user, max_tokens, temperature,
                        cache_system,
                    )
                elif kind == "gemini":
                    response = await self._call_gemini(
                        conn, system, user, max_tokens, temperature,
                    )
                elif kind == "openai":
                    response = await self._call_openai(
                        conn, system, user, max_tokens, temperature,
                    )
                else:
                    print(f"[llm_client] {use_case} {label}: unknown kind for "
                          f"provider {conn.get('provider')!r}; skipping",
                          file=sys.stderr)
                    continue
                if use_case == "forecaster":
                    from engine.runtime_alerts import report_recovery
                    report_recovery("forecast_provider")
                return response
            except Exception as exc:
                last_exc = exc
                print(f"[llm_client] {use_case} {label} "
                      f"{conn.get('provider')} failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                continue

        print(f"[llm_client] {use_case} chain exhausted; last exc: "
              f"{type(last_exc).__name__ if last_exc else 'None'}: "
              f"{last_exc}", file=sys.stderr)
        if use_case == "forecaster":
            from engine.runtime_alerts import report_failure
            error_name = type(last_exc).__name__ if last_exc else "UnknownError"
            report_failure(
                "forecast_provider",
                f"Every configured forecast provider failed ({error_name}).",
            )
        return None

    # ── provider call paths ─────────────────────────────────────────────────

    async def _call_anthropic(
        self,
        conn: dict,
        system: Optional[str],
        user: str,
        max_tokens: int,
        temperature: float,
        cache_system: bool,
    ) -> str:
        api_key = conn.get("api_key") or ""
        model = _providers.model_for(conn)
        cache_key = ("anthropic", api_key)
        with self._lock:
            client = self._clients.get(cache_key)
            if client is None:
                client = anthropic.Anthropic(api_key=api_key)
                self._clients[cache_key] = client

        # Build the `system` argument three ways:
        #   None     → don't send a system block (NOT_GIVEN omits it).
        #   cached   → list form with cache_control:ephemeral. The cache
        #              marker applies to that block + everything before
        #              it; since `system` is the first content the bot
        #              sends, marking it caches the whole system. Hits
        #              within the 5-min TTL cost 0.1x input; the first
        #              write costs 1.25x once. Minimum cacheable length
        #              is ~1024 tokens, met by the evaluator's prompt.
        #   plain    → ordinary string, no caching.
        if system is None:
            system_arg: Any = anthropic.NOT_GIVEN
        elif cache_system:
            system_arg = [{
                "type":          "text",
                "text":          system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_arg = system

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                model       = model,
                max_tokens  = max_tokens,
                temperature = temperature,
                system      = system_arg,
                messages    = [{"role": "user", "content": user}],
            ),
        )

        # Surface cache usage in stderr so the operator can confirm
        # savings. cache_creation_input_tokens = this call wrote the
        # cache; cache_read_input_tokens = this call read it.
        try:
            u = response.usage
            cw = getattr(u, "cache_creation_input_tokens", 0) or 0
            cr = getattr(u, "cache_read_input_tokens", 0) or 0
            if cw or cr:
                print(f"[llm_client] anthropic cache: write={cw} read={cr} "
                      f"input={u.input_tokens} output={u.output_tokens}",
                      file=sys.stderr)
        except Exception:
            pass

        return response.content[0].text

    async def _call_gemini(
        self,
        conn: dict,
        system: Optional[str],
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        api_key = conn.get("api_key") or ""
        model = _providers.model_for(conn)
        cache_key = ("gemini", api_key)
        with self._lock:
            client = self._clients.get(cache_key)
            if client is None:
                from google import genai
                client = genai.Client(api_key=api_key)
                self._clients[cache_key] = client

        # google-genai accepts the config as a dict. system goes in as
        # system_instruction so the model sees it the way Anthropic does.
        #
        # thinking_budget=0 disables Gemini Flash's chain-of-thought
        # phase. Without it the model spends most of max_output_tokens on
        # internal "thoughts" before emitting any output text; a request
        # with max_tokens=20 came back with text=None and 20 thinking
        # tokens. We want JSON, not deliberation.
        cfg: dict = {
            "max_output_tokens": max_tokens,
            "temperature":       temperature,
            "thinking_config":   {"thinking_budget": 0},
        }
        if system is not None:
            cfg["system_instruction"] = system

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model    = model,
                contents = user,
                config   = cfg,
            ),
        )
        return response.text

    async def _call_openai(
        self,
        conn: dict,
        system: Optional[str],
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """OpenAI-compatible chat.completions call. One code path covers
        OpenAI, xAI/Grok, DeepSeek, Mistral, Groq, OpenRouter and custom
        endpoints — only the base_url + model differ."""
        api_key = conn.get("api_key") or ""
        base_url = _providers.base_url_for(conn) or None
        model = _providers.model_for(conn)
        cache_key = ("openai", api_key, base_url or "")
        with self._lock:
            client = self._clients.get(cache_key)
            if client is None:
                from openai import OpenAI
                kwargs: dict = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                client = OpenAI(**kwargs)
                self._clients[cache_key] = client

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        def _create(use_completion_tokens: bool, include_temp: bool):
            kw: dict = {"model": model, "messages": messages}
            if use_completion_tokens:
                kw["max_completion_tokens"] = max_tokens
            else:
                kw["max_tokens"] = max_tokens
            if include_temp:
                kw["temperature"] = temperature
            return client.chat.completions.create(**kw)

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None, lambda: _create(False, True),
            )
        except Exception as exc:
            # Reasoning models (o1/o3/o4...) reject max_tokens and a
            # non-default temperature. Retry once with the newer param
            # and default temperature before giving up on this provider.
            msg = str(exc).lower()
            if ("max_tokens" in msg or "max_completion_tokens" in msg
                    or "temperature" in msg or "unsupported" in msg):
                response = await loop.run_in_executor(
                    None, lambda: _create(True, False),
                )
            else:
                raise

        return response.choices[0].message.content
