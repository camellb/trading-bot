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
import time
from typing import Any, Optional

import anthropic

from engine import llm_providers as _providers
from engine.user_config import resolve_llm_chain


# Per-request wall-clock ceiling for every provider SDK. The anthropic
# SDK's own default is 600 seconds; one wedged HTTPS call at that
# default pins a loop-executor thread for 10 minutes, which starves the
# scheduler (observed 2026-07-16: pm_resolve_fast "missed by 0:14").
# 90s is comfortably above a slow forecaster completion and far below
# the scan's 240s ceiling.
_REQUEST_TIMEOUT_S = 90.0

# Substrings that mark an error as transient (worth one retry on the
# same connection before failing over). Covers Gemini 503 UNAVAILABLE
# spikes, Anthropic 529 overloaded, and generic gateway/network blips.
_TRANSIENT_MARKERS = (
    "503", "502", "504", "529", "429",
    "overloaded", "unavailable", "rate limit", "rate_limit",
    "timed out", "timeout", "connection error", "connection reset",
    "temporarily",
)


def _exc_status(exc: Exception) -> Optional[int]:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return status if isinstance(status, int) else None


# Failure classes that will NOT clear by retrying the same connection
# a few seconds later. Each maps to a cooldown so the scan stops paying
# a round trip (and a 3 s backoff) per market for a key that is out of
# quota, out of credit, rejected, or pointed at a missing model.
# 2026-09-12 log: 13,203 identical daily-quota exhaustions in two days,
# each retried after a 3 s sleep, on every market of every scan.
_QUOTA_MARKERS = (
    "resource_exhausted", "quota", "daily limit", "exceeded your current",
    "insufficient_quota",
)
_BILLING_MARKERS = (
    "credit balance", "billing", "purchase credits", "no credit",
    "payment required", "insufficient funds",
)
_AUTH_MARKERS = (
    "invalid x-api-key", "authentication", "api key not valid",
    "invalid api key", "invalid_api_key", "incorrect api key",
    "unauthorized", "permission denied", "permission_denied",
)
_MODEL_MARKERS = (
    "not_found", "model not found", "does not exist",
    "not found for api version", "unknown model", "no longer supported",
    "is not supported", "decommissioned",
)
_COOLDOWN_SECONDS = {
    "quota":   900.0,
    "billing": 1800.0,
    "auth":    1800.0,
    "model":   1800.0,
}


def classify_cooldown(exc: Exception) -> Optional[tuple[float, str]]:
    """(cooldown_seconds, reason) for failures that retrying cannot fix
    in the short term, else None. Reasons: quota | billing | auth | model."""
    status = _exc_status(exc)
    msg = f"{type(exc).__name__}: {exc}".lower()
    if status == 429 or " 429" in msg or msg.startswith("429"):
        if any(m in msg for m in _QUOTA_MARKERS):
            return (_COOLDOWN_SECONDS["quota"], "quota")
        return None
    if status in (401, 403) or any(m in msg for m in _AUTH_MARKERS):
        return (_COOLDOWN_SECONDS["auth"], "auth")
    if any(m in msg for m in _BILLING_MARKERS):
        return (_COOLDOWN_SECONDS["billing"], "billing")
    if status == 404 or any(m in msg for m in _MODEL_MARKERS):
        return (_COOLDOWN_SECONDS["model"], "model")
    return None


def _is_transient_llm_error(exc: Exception) -> bool:
    if classify_cooldown(exc) is not None:
        # Quota / billing / auth / missing model: a second attempt three
        # seconds later is guaranteed to fail the same way.
        return False
    status = _exc_status(exc)
    if status is not None:
        return status in (429, 500, 502, 503, 504, 529)
    msg = f"{type(exc).__name__}: {exc}".lower()
    if "not_found" in msg or "404" in msg:
        return False
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def short_error(exc: BaseException, limit: int = 300) -> str:
    """`Type: message` with the message collapsed to one line and capped.
    Provider SDK errors embed the full JSON body (Gemini's quota error is
    ~1.5 KB with help links); printing it per attempt per market grew
    sidecar.log to 53 MB in two days."""
    text = " ".join(str(exc).split())
    if len(text) > limit:
        text = text[:limit] + "..."
    return f"{type(exc).__name__}: {text}"


class EmptyLLMResponse(RuntimeError):
    """The provider answered without any usable text (thinking-only
    output, safety block, refusal, or max_tokens hit before the first
    text block). Raised so call() fails over to the next connection
    instead of returning an empty string as success."""


def extract_anthropic_text(response) -> str:
    """Join the text blocks of a Messages API response.

    Current models run adaptive thinking by default, so `content` starts
    with a thinking block that has no `.text`; `response.content[0].text`
    raised AttributeError on every forecast after the tokens were billed
    (335 times in the 2026-09-12 log). Only `type == "text"` blocks carry
    the answer."""
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
    joined = "\n".join(parts).strip()
    if not joined:
        raise EmptyLLMResponse(
            "provider returned no text "
            f"(stop_reason={getattr(response, 'stop_reason', None)})"
        )
    return joined


# Model families that reject non-default sampling parameters (a
# non-default `temperature` returns 400). The default 1.0 is omitted for
# every model; a non-default value is forwarded only to families that
# still accept it.
_NO_SAMPLING_PREFIXES = (
    "claude-sonnet-5", "claude-opus-5", "claude-opus-4-7", "claude-opus-4-8",
    "claude-fable", "claude-mythos",
)


def _anthropic_accepts_sampling(model: str) -> bool:
    m = (model or "").lower()
    return not any(m.startswith(p) for p in _NO_SAMPLING_PREFIXES)


# User-facing detail for the "Forecast provider unavailable" alert.
# Plain copy, no vendor names, says what to do next.
_FAILURE_COPY = {
    "quota":   ("The forecast provider's usage quota is used up. Add billing to "
                "that key or add a backup connection in Settings > Connections. "
                "Delfi retries every 15 minutes."),
    "billing": ("The forecast provider declined the request because the account "
                "has no credit. Top up the provider account or switch keys in "
                "Settings > Connections. Delfi retries every 30 minutes."),
    "auth":    ("The forecast provider rejected the API key. Check the key in "
                "Settings > Connections. Delfi retries every 30 minutes."),
    "model":   ("The model set on the forecast connection is not available for "
                "that key. Pick another model in Settings > Connections. "
                "Delfi retries every 30 minutes."),
    "network": ("The forecast provider could not be reached. Delfi retries on "
                "the next scan."),
    "empty":   ("The forecast provider returned no usable answer. Delfi retries "
                "on the next scan."),
    "error":   ("The forecast provider returned an error. Delfi retries on the "
                "next scan."),
}


def failure_detail(reason: Optional[str]) -> str:
    return _FAILURE_COPY.get(reason or "error", _FAILURE_COPY["error"])


def _reason_for_exception(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "error"
    cls = classify_cooldown(exc) if isinstance(exc, Exception) else None
    if cls is not None:
        return cls[1]
    if isinstance(exc, EmptyLLMResponse):
        return "empty"
    if isinstance(exc, Exception) and _is_transient_llm_error(exc):
        return "network"
    return "error"


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
        # Per-connection cooldowns: connection key -> (until, reason).
        # Set by note_failure() for quota / billing / auth / model
        # failures; cleared by note_success() or when `until` passes.
        self._cooldowns: dict[str, tuple[float, str]] = {}
        self._last_exhausted_log: dict[str, float] = {}

    def reset(self) -> None:
        with self._lock:
            self._clients = {}
            self._cooldowns = {}

    # ── cooldown bookkeeping ────────────────────────────────────────────

    @staticmethod
    def _conn_key(conn: dict) -> str:
        return str(conn.get("id") or f"{conn.get('provider')}:{conn.get('label')}")

    def cooldown_remaining(self, conn: dict) -> float:
        with self._lock:
            entry = self._cooldowns.get(self._conn_key(conn))
        if not entry:
            return 0.0
        remaining = entry[0] - time.monotonic()
        return remaining if remaining > 0 else 0.0

    def cooldown_reason(self, conn: dict) -> Optional[str]:
        if self.cooldown_remaining(conn) <= 0:
            return None
        with self._lock:
            entry = self._cooldowns.get(self._conn_key(conn))
        return entry[1] if entry else None

    def note_failure(self, conn: dict, exc: Exception, label: str = "") -> Optional[str]:
        """Record a failed call. Returns the cooldown reason when the
        failure class pauses the connection, else None."""
        cls = classify_cooldown(exc)
        if cls is None:
            return None
        seconds, reason = cls
        with self._lock:
            self._cooldowns[self._conn_key(conn)] = (time.monotonic() + seconds, reason)
        who = f"{label} " if label else ""
        print(f"[llm_client] {who}{conn.get('provider')} connection paused for "
              f"{int(seconds)}s ({reason}): {short_error(exc)}",
              file=sys.stderr)
        return reason

    def note_success(self, conn: dict) -> None:
        with self._lock:
            self._cooldowns.pop(self._conn_key(conn), None)

    def cooldown_snapshot(self) -> dict[str, dict]:
        """{connection key: {reason, remaining_s}} for /api/health and tests."""
        now = time.monotonic()
        with self._lock:
            items = list(self._cooldowns.items())
        return {
            key: {"reason": reason, "remaining_s": int(until - now)}
            for key, (until, reason) in items if until > now
        }

    def _log_exhausted(self, use_case: str, last_exc: Optional[BaseException],
                       skipped: list) -> None:
        """One stderr line per use case per minute, not one per market."""
        now = time.monotonic()
        if now - self._last_exhausted_log.get(use_case, 0.0) < 60.0:
            return
        self._last_exhausted_log[use_case] = now
        if last_exc is None and skipped:
            paused = ", ".join(
                f"{label} {conn.get('provider')} ({reason}, {int(rem)}s left)"
                for label, conn, reason, rem in skipped
            )
            print(f"[llm_client] {use_case}: every connection is paused: {paused}",
                  file=sys.stderr)
        else:
            print(f"[llm_client] {use_case} chain exhausted; last: "
                  f"{short_error(last_exc) if last_exc else 'None'}",
                  file=sys.stderr)

    async def test_connection(self, conn: dict, timeout_s: float = 25.0) -> dict:
        """One tiny round trip on a single connection, for the Settings
        page. Never raises; returns {ok, latency_ms, model, error_kind,
        error}. Does not touch or clear cooldowns."""
        kind = _providers.provider_kind(conn.get("provider"))
        model = _providers.model_for(conn)
        prompt = "Reply with the single word OK."
        t0 = time.monotonic()
        try:
            if kind == "anthropic":
                coro = self._call_anthropic(conn, None, prompt, 256, 1.0, False)
            elif kind == "gemini":
                coro = self._call_gemini(conn, None, prompt, 256, 1.0)
            elif kind == "openai":
                coro = self._call_openai(conn, None, prompt, 256, 1.0)
            else:
                return {"ok": False, "latency_ms": 0, "model": model,
                        "error_kind": "provider",
                        "error": f"unknown provider {conn.get('provider')!r}"}
            reply = await asyncio.wait_for(coro, timeout=timeout_s)
            return {
                "ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "model": model,
                "reply": (reply or "").strip()[:80],
            }
        except asyncio.TimeoutError:
            return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000),
                    "model": model, "error_kind": "timeout",
                    "error": f"no reply within {int(timeout_s)}s"}
        except Exception as exc:
            return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000),
                    "model": model, "error_kind": _reason_for_exception(exc),
                    "error": short_error(exc, 300)}

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
        last_reason: Optional[str] = None
        skipped: list[tuple[str, dict, str, float]] = []
        for i, conn in enumerate(chain):
            kind = _providers.provider_kind(conn.get("provider"))
            label = "primary" if i == 0 else f"backup{i}"
            if kind not in ("anthropic", "gemini", "openai"):
                print(f"[llm_client] {use_case} {label}: unknown kind for "
                      f"provider {conn.get('provider')!r}; skipping",
                      file=sys.stderr)
                continue
            remaining = self.cooldown_remaining(conn)
            if remaining > 0:
                skipped.append((label, conn, self.cooldown_reason(conn) or "error",
                                remaining))
                continue
            # Two attempts per connection: transient failures (5xx,
            # overloaded, rate limit, network blip) get one short-backoff
            # retry before failing over. A Gemini 503 spike used to kill
            # the whole chain instantly and block trading. Permanent
            # errors (auth, 404 model) fail over immediately.
            for attempt in (0, 1):
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
                    else:
                        response = await self._call_openai(
                            conn, system, user, max_tokens, temperature,
                        )
                    if not (response or "").strip():
                        # Treat an empty answer as a failure so the
                        # backup connection gets a chance; returning
                        # "" here used to count as success and skip
                        # the failover entirely.
                        raise EmptyLLMResponse("provider returned an empty response")
                    self.note_success(conn)
                    if use_case == "forecaster":
                        from engine.runtime_alerts import report_recovery
                        report_recovery("forecast_provider")
                    return response
                except Exception as exc:
                    last_exc = exc
                    reason = self.note_failure(conn, exc, label=label)
                    if reason:
                        last_reason = reason
                    transient = _is_transient_llm_error(exc)
                    print(f"[llm_client] {use_case} {label} "
                          f"{conn.get('provider')} failed "
                          f"(attempt {attempt + 1}, "
                          f"{'transient' if transient else 'permanent'}): "
                          f"{short_error(exc)}", file=sys.stderr)
                    if transient and attempt == 0:
                        await asyncio.sleep(3.0)
                        continue
                    break

        if last_reason is None and skipped:
            last_reason = skipped[0][2]
        if last_reason is None:
            last_reason = _reason_for_exception(last_exc)
        self._log_exhausted(use_case, last_exc, skipped)
        if use_case == "forecaster":
            from engine.runtime_alerts import report_failure
            report_failure("forecast_provider", failure_detail(last_reason))
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
                # max_retries=0: retry policy lives in call(), where it
                # is transient-aware and shared across providers. The
                # SDK default (2 retries x 600s timeout) could pin an
                # executor thread for tens of minutes.
                client = anthropic.Anthropic(
                    api_key=api_key,
                    timeout=_REQUEST_TIMEOUT_S,
                    max_retries=0,
                )
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

        create_kwargs: dict = {
            "model":      model,
            "max_tokens": max_tokens,
            "system":     system_arg,
            "messages":   [{"role": "user", "content": user}],
        }
        # The API default temperature is 1.0; only forward a different
        # value, and only to model families that still accept sampling
        # parameters (current models return 400 on a non-default one).
        if temperature != 1.0 and _anthropic_accepts_sampling(model):
            create_kwargs["temperature"] = temperature
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.messages.create(**create_kwargs),
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

        return extract_anthropic_text(response)

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
                # http_options timeout is in milliseconds.
                client = genai.Client(
                    api_key=api_key,
                    http_options={"timeout": int(_REQUEST_TIMEOUT_S * 1000)},
                )
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
        try:
            text = response.text
        except Exception:
            text = None
        if not text or not str(text).strip():
            finish = None
            try:
                cands = getattr(response, "candidates", None) or []
                finish = getattr(cands[0], "finish_reason", None) if cands else None
            except Exception:
                pass
            raise EmptyLLMResponse(
                f"provider returned no text (finish_reason={finish}, "
                f"prompt_feedback={getattr(response, 'prompt_feedback', None)})"
            )
        return str(text)

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
                kwargs: dict = {
                    "api_key":     api_key,
                    "timeout":     _REQUEST_TIMEOUT_S,
                    "max_retries": 0,
                }
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

        choices = getattr(response, "choices", None) or []
        content = choices[0].message.content if choices else None
        if not content or not str(content).strip():
            finish = getattr(choices[0], "finish_reason", None) if choices else None
            refusal = getattr(choices[0].message, "refusal", None) if choices else None
            raise EmptyLLMResponse(
                f"provider returned no text (finish_reason={finish}, "
                f"refusal={refusal})"
            )
        return str(content)
