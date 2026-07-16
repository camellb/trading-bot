"""
LLM provider registry.

Single source of truth for the set of LLM providers Delfi can talk to,
the call-path "kind" each one uses, its default model + API base URL,
and the validation/normalization of a stored connection entry.

Background. Delfi used to hard-code three LLM slots ("LLM API key",
"Backup LLM", "Search LLM") and detect the provider from the api-key
prefix (sk-ant- -> anthropic, AIzaSy -> gemini) in
engine.llm_client._provider_of. That only ever supported two providers
and gave the user no way to add a third or pick a model.

This module replaces that with an explicit provider catalogue. A stored
connection is a dict:

    {
        "id":       "conn_<random>",   # stable handle for role wiring
        "provider": "anthropic",        # a key in this registry
        "label":    "Claude",          # user-facing display name
        "model":    "claude-...",       # model id sent to the provider
        "base_url": "",                 # OpenAI-compatible base override
        "api_key":  "sk-ant-...",       # the secret (persisted by
                                         # user_config, never here)
    }

"kind" collapses every provider to one of three call paths:

    anthropic  -> anthropic SDK (messages API, prompt caching)
    gemini     -> google-genai SDK (generate_content)
    openai     -> openai SDK against base_url (chat.completions).
                  Covers OpenAI, xAI/Grok, DeepSeek, Mistral, Groq,
                  OpenRouter, and any custom OpenAI-compatible endpoint
                  - they all speak the same wire protocol; only the
                  base_url and model id differ.

This module holds NO secrets and touches NO files. It is pure data +
pure functions so llm_client (dispatch), user_config (migration +
persistence shape) and local_api (UI catalogue) can all import it
without a dependency cycle.
"""

from __future__ import annotations

from typing import Optional, TypedDict


class ProviderSpec(TypedDict):
    key: str                  # registry id, stored as connection["provider"]
    label: str                # default display name
    kind: str                 # "anthropic" | "gemini" | "openai"
    base_url: str             # default OpenAI-compatible base ("" for native SDKs)
    models: tuple[str, ...]   # suggested models (() = free-form only)
    default_model: str        # pre-selected model for a new connection
    key_hint: str             # placeholder shown in the api-key field
    custom_base_url: bool     # True => user must supply base_url (no default)


# Ordered so the UI dropdown lists the most common providers first.
_PROVIDERS: tuple[ProviderSpec, ...] = (
    {
        "key":             "anthropic",
        "label":           "Anthropic (Claude)",
        "kind":            "anthropic",
        "base_url":        "",
        "models":          (
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-haiku-4-5-20251001",
        ),
        "default_model":   "claude-sonnet-5",
        "key_hint":        "sk-ant-...",
        "custom_base_url": False,
    },
    {
        "key":             "openai",
        "label":           "OpenAI",
        "kind":            "openai",
        "base_url":        "https://api.openai.com/v1",
        "models":          (
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "o3",
            "o4-mini",
        ),
        "default_model":   "gpt-4o",
        "key_hint":        "sk-...",
        "custom_base_url": False,
    },
    {
        "key":             "gemini",
        "label":           "Google (Gemini)",
        "kind":            "gemini",
        "base_url":        "",
        "models":          (
            "gemini-flash-latest",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
        ),
        "default_model":   "gemini-flash-latest",
        "key_hint":        "AIza...",
        "custom_base_url": False,
    },
    {
        "key":             "xai",
        "label":           "xAI (Grok)",
        "kind":            "openai",
        "base_url":        "https://api.x.ai/v1",
        "models":          (
            "grok-4",
            "grok-3",
            "grok-3-mini",
        ),
        "default_model":   "grok-3",
        "key_hint":        "xai-...",
        "custom_base_url": False,
    },
    {
        "key":             "deepseek",
        "label":           "DeepSeek",
        "kind":            "openai",
        "base_url":        "https://api.deepseek.com",
        "models":          (
            "deepseek-chat",
            "deepseek-reasoner",
        ),
        "default_model":   "deepseek-chat",
        "key_hint":        "sk-...",
        "custom_base_url": False,
    },
    {
        "key":             "mistral",
        "label":           "Mistral",
        "kind":            "openai",
        "base_url":        "https://api.mistral.ai/v1",
        "models":          (
            "mistral-large-latest",
            "mistral-small-latest",
        ),
        "default_model":   "mistral-large-latest",
        "key_hint":        "...",
        "custom_base_url": False,
    },
    {
        "key":             "groq",
        "label":           "Groq",
        "kind":            "openai",
        "base_url":        "https://api.groq.com/openai/v1",
        "models":          (
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
        ),
        "default_model":   "llama-3.3-70b-versatile",
        "key_hint":        "gsk_...",
        "custom_base_url": False,
    },
    {
        "key":             "openrouter",
        "label":           "OpenRouter",
        "kind":            "openai",
        "base_url":        "https://openrouter.ai/api/v1",
        "models":          (),   # thousands of models; free-form entry
        "default_model":   "openai/gpt-4o",
        "key_hint":        "sk-or-...",
        "custom_base_url": False,
    },
    {
        "key":             "openai_compatible",
        "label":           "Custom (OpenAI-compatible)",
        "kind":            "openai",
        "base_url":        "",
        "models":          (),   # user supplies the model id
        "default_model":   "",
        "key_hint":        "API key",
        "custom_base_url": True,
    },
)

_BY_KEY: dict[str, ProviderSpec] = {p["key"]: p for p in _PROVIDERS}

# The four role slots: two use-cases x (primary, backup).
ROLE_KEYS: tuple[str, ...] = (
    "forecaster_primary",
    "forecaster_backup",
    "search_primary",
    "search_backup",
)

# use-case -> ordered role chain (primary tried first, then backup).
USE_CASE_CHAINS: dict[str, tuple[str, ...]] = {
    "forecaster": ("forecaster_primary", "forecaster_backup"),
    "search":     ("search_primary", "search_backup"),
}

VALID_KINDS: tuple[str, ...] = ("anthropic", "gemini", "openai")


def providers() -> list[dict]:
    """Public catalogue for the UI. Tuples become JSON-serialisable
    lists. Holds no secrets."""
    out: list[dict] = []
    for p in _PROVIDERS:
        out.append({
            "key":             p["key"],
            "label":           p["label"],
            "kind":            p["kind"],
            "base_url":        p["base_url"],
            "models":          list(p["models"]),
            "default_model":   p["default_model"],
            "key_hint":        p["key_hint"],
            "custom_base_url": p["custom_base_url"],
        })
    return out


def get_provider(key: Optional[str]) -> Optional[ProviderSpec]:
    if not key:
        return None
    return _BY_KEY.get(key)


def is_provider(key: Optional[str]) -> bool:
    return bool(key) and key in _BY_KEY


def provider_kind(key: Optional[str]) -> Optional[str]:
    """Call-path kind for a provider key, or None if unknown."""
    p = _BY_KEY.get(key or "")
    return p["kind"] if p else None


def default_model(key: Optional[str]) -> str:
    p = _BY_KEY.get(key or "")
    return p["default_model"] if p else ""


def default_base_url(key: Optional[str]) -> str:
    p = _BY_KEY.get(key or "")
    return p["base_url"] if p else ""


def needs_custom_base_url(key: Optional[str]) -> bool:
    p = _BY_KEY.get(key or "")
    return bool(p and p["custom_base_url"])


def detect_provider(api_key: Optional[str]) -> Optional[str]:
    """Best-effort provider key from an api-key prefix.

    Used only by the legacy->connections migration, where the stored
    keys are known to be anthropic / gemini / (occasionally) an
    OpenAI-style sk- key. Returns None for shapes we can't attribute;
    the caller then falls back to a sensible default rather than
    guessing wrong.
    """
    if not api_key:
        return None
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("AIzaSy") or api_key.startswith("AIza"):
        return "gemini"
    if api_key.startswith("xai-"):
        return "xai"
    if api_key.startswith("gsk_"):
        return "groq"
    if api_key.startswith("sk-or-"):
        return "openrouter"
    if api_key.startswith("sk-"):
        return "openai"
    return None


def base_url_for(entry: dict) -> str:
    """Effective base_url for an OpenAI-kind connection: the entry's
    explicit override if set, else the provider default."""
    override = (entry.get("base_url") or "").strip()
    if override:
        return override
    return default_base_url(entry.get("provider"))


# Model ids Anthropic has retired from the API (404 not_found) mapped to
# the closest same-tier current id. Applied at dispatch time in
# model_for() so connections saved before the retirement keep working
# without the user re-editing Settings. Incident 2026-07-16: every
# stored forecaster connection pointed at claude-sonnet-4-20250514,
# the API returned 404 on all of them, and trading blocked for days.
RETIRED_MODEL_MAP: dict[str, str] = {
    "claude-sonnet-4-20250514":   "claude-sonnet-5",
    "claude-sonnet-4-0":          "claude-sonnet-5",
    "claude-opus-4-20250514":     "claude-opus-4-8",
    "claude-opus-4-0":            "claude-opus-4-8",
    "claude-3-5-haiku-20241022":  "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-latest":    "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet-20241022": "claude-sonnet-5",
    "claude-3-7-sonnet-20250219": "claude-sonnet-5",
}


def model_for(entry: dict) -> str:
    """Effective model id: the entry's model if set, else the provider
    default. Retired Anthropic ids are remapped to their current
    same-tier replacement (see RETIRED_MODEL_MAP)."""
    m = (entry.get("model") or "").strip()
    if not m:
        m = default_model(entry.get("provider"))
    return RETIRED_MODEL_MAP.get(m, m)


def validate_connection(entry: dict) -> Optional[str]:
    """Return an error string if the connection is unusable, else None.

    Checks the provider is known, an api_key is present, a model can be
    resolved, and a base_url exists for custom OpenAI-compatible
    providers. Does NOT call the network - this is a shape check only.
    """
    if not isinstance(entry, dict):
        return "connection must be an object"
    provider = entry.get("provider")
    if not is_provider(provider):
        return f"unknown provider '{provider}'"
    if not (entry.get("api_key") or "").strip():
        return "api_key is required"
    if not model_for(entry):
        return "model is required for this provider"
    if needs_custom_base_url(provider) and not base_url_for(entry):
        return "base_url is required for a custom OpenAI-compatible provider"
    return None


def normalize_connection(entry: dict) -> dict:
    """Coerce a raw connection dict into the canonical persisted shape,
    filling provider defaults and trimming strings.

    Leaves `id` as-is (the storage layer assigns one when absent). Does
    not validate - call validate_connection separately.
    """
    provider = (entry.get("provider") or "").strip()
    spec = _BY_KEY.get(provider)
    label = (entry.get("label") or "").strip()
    if not label and spec:
        label = spec["label"]
    model = (entry.get("model") or "").strip()
    if not model and spec:
        model = spec["default_model"]
    base_url = (entry.get("base_url") or "").strip()
    if not base_url and spec and not spec["custom_base_url"]:
        base_url = spec["base_url"]
    return {
        "id":       (entry.get("id") or "").strip(),
        "provider": provider,
        "label":    label,
        "model":    model,
        "base_url": base_url,
        "api_key":  (entry.get("api_key") or "").strip(),
    }
