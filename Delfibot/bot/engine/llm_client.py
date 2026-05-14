"""
Provider-routing LLM client with primary/backup failover and Anthropic
prompt caching.

The bot makes two kinds of LLM calls today:
  1. polymarket_evaluator — the per-market forecast call. Huge system
     prompt (~1500 tokens, reusable verbatim across every market in a
     scan) plus a short per-market user message. Fired 50+ times per
     scan cycle.
  2. research/fetcher — claude-driven keyword extraction for research
     queries. Short prompt, no reusable system block.

Both used to instantiate anthropic.Anthropic() directly with no
fallback. When the configured key 401'd, every call died. The user
also had a Gemini key stored under "Backup LLM" but nothing was
wired to use it.

This module:

  • Routes by api-key prefix: sk-ant-* → Anthropic, AIzaSy* → Gemini.
    Unknown prefixes are skipped in the failover chain rather than
    guessed at.
  • On primary failure (auth error, rate limit, 5xx, network error,
    unexpected exception), retries the same logical call against the
    backup provider if one is set. Returns the first successful text.
  • Caches a single Anthropic + a single Gemini SDK client per
    process. `reset()` drops both — call after a credential save so
    the next request constructs fresh clients against the current
    env/keychain.
  • When cache_system=True, sends the system prompt to Anthropic with
    cache_control:ephemeral so subsequent calls within the 5-minute
    TTL pay 0.1x the input price on the cached prefix instead of 1x.
    First call costs 1.25x once to write the cache; from call 2
    onward the savings dominate. For Gemini that flag is a no-op (no
    equivalent prefix-cache API at this size). The 1024-token minimum
    for Anthropic caching is comfortably met by the evaluator's
    system prompt.

Module-level singleton: `get_llm()` returns a process-wide instance;
`reset_llm()` clears its cached SDK clients. The hot-reload path in
`local_api._put_credentials` calls `reset_llm()` after writing keys
so credential changes take effect without a daemon restart.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Optional

import anthropic

import config
from engine.user_config import get_llm_backup_key


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

    Call after a credential save so the next request constructs fresh
    clients against the now-current keys. Safe to call even if no
    singleton exists yet — it's a no-op in that case.
    """
    if _singleton is not None:
        _singleton.reset()


# ── Provider detection ──────────────────────────────────────────────────────

def _provider_of(api_key: Optional[str]) -> Optional[str]:
    """Map api-key prefix → provider name. Unknown shapes return None
    so the caller skips that slot rather than constructing an SDK
    against a key that won't authenticate anyway."""
    if not api_key:
        return None
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("AIzaSy"):
        return "gemini"
    return None


# ── Client ──────────────────────────────────────────────────────────────────

class LLMClient:
    """
    Two-provider client with primary/backup failover.

    Construction is lazy: the underlying SDK clients are built only
    when the first request needs them. `reset()` nulls the caches;
    the next call reconstructs against whatever env + keychain says.
    """

    def __init__(self) -> None:
        self._anthropic: Optional[anthropic.Anthropic] = None
        self._gemini = None  # google.genai.Client once constructed
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._anthropic = None
            self._gemini = None

    async def call(
        self,
        *,
        system: Optional[str],
        user: str,
        max_tokens: int,
        temperature: float = 1.0,
        cache_system: bool = False,
    ) -> Optional[str]:
        """
        Try primary, fall back to backup, return response text.

        Returns None only if every configured provider failed or no
        provider key was usable. Callers should treat None as "skip
        this market" rather than retrying indefinitely.
        """
        primary = os.environ.get("ANTHROPIC_API_KEY") or ""
        backup = get_llm_backup_key() or ""
        attempts: list[tuple[str, str, str]] = []
        for key, label in [(primary, "primary"), (backup, "backup")]:
            provider = _provider_of(key)
            if provider is None:
                continue
            attempts.append((provider, key, label))

        if not attempts:
            print("[llm_client] no usable provider key configured "
                  "(neither ANTHROPIC_API_KEY env nor backup keychain "
                  "matches a known prefix)", file=sys.stderr)
            return None

        last_exc: Optional[Exception] = None
        for provider, key, label in attempts:
            try:
                if provider == "anthropic":
                    return await self._call_anthropic(
                        key, system, user, max_tokens, temperature,
                        cache_system,
                    )
                if provider == "gemini":
                    return await self._call_gemini(
                        key, system, user, max_tokens, temperature,
                    )
            except Exception as exc:
                last_exc = exc
                print(f"[llm_client] {label} {provider} failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                continue

        # Every configured provider raised. Surface a one-line summary
        # so the operator can find it in the err log without grepping.
        print(f"[llm_client] all providers exhausted; last exc: "
              f"{type(last_exc).__name__ if last_exc else 'None'}: "
              f"{last_exc}", file=sys.stderr)
        return None

    async def _call_anthropic(
        self,
        api_key: str,
        system: Optional[str],
        user: str,
        max_tokens: int,
        temperature: float,
        cache_system: bool,
    ) -> str:
        with self._lock:
            if self._anthropic is None:
                self._anthropic = anthropic.Anthropic(api_key=api_key)
            client = self._anthropic

        # Build the `system` argument three ways:
        #   None     → don't send a system block at all (use NOT_GIVEN
        #              so the SDK omits the field).
        #   cached   → list form with cache_control:ephemeral. The
        #              cache marker applies to that block + everything
        #              before it; since `system` is the first content
        #              the bot ever sends, marking it caches the whole
        #              system. Hits within the 5-min ephemeral TTL
        #              cost 0.1x input; the first write costs 1.25x
        #              once. Minimum cacheable length is ~1024 tokens,
        #              comfortably met by the evaluator's prompt.
        #   plain    → ordinary string, no caching.
        if system is None:
            system_arg = anthropic.NOT_GIVEN
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
                model       = config.CLAUDE_MODEL,
                max_tokens  = max_tokens,
                temperature = temperature,
                system      = system_arg,
                messages    = [{"role": "user", "content": user}],
            ),
        )

        # Surface cache usage in stderr at debug level so the operator
        # can confirm savings. Anthropic SDK exposes
        # response.usage.cache_creation_input_tokens (this call wrote
        # the cache) and cache_read_input_tokens (this call read it).
        # Total input tokens still includes both classes.
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
        api_key: str,
        system: Optional[str],
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        with self._lock:
            if self._gemini is None:
                from google import genai
                self._gemini = genai.Client(api_key=api_key)
            client = self._gemini

        # The google-genai SDK accepts the config either as a
        # GenerateContentConfig instance or as a dict. Dict is
        # smaller surface area; passing system as system_instruction
        # so the model sees it the same way Anthropic does.
        #
        # thinking_budget=0 disables Gemini Flash's chain-of-thought
        # phase. Without this, the model spends most of
        # max_output_tokens on internal "thoughts" before emitting
        # any output text; a request with max_tokens=20 came back
        # with text=None and 20 thinking tokens. We don't need
        # reasoning here (the bot wants JSON), and the caller's
        # max_tokens is sized for output, not deliberation.
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
                model    = config.GEMINI_MODEL,
                contents = user,
                config   = cfg,
            ),
        )
        return response.text
