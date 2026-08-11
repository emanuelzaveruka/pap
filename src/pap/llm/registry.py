"""Provider lookup by name.

Kept separate from ``router.py`` so ``doctor --live`` can enumerate and probe
providers without pulling in the database or the call ledger — a credential check
should not need a working Postgres.
"""

from __future__ import annotations

from ..config import Settings
from .base import LLMProvider
from .claude import ClaudeProvider
from .gemini import GeminiProvider
from .openai import OpenAIProvider

_PROVIDERS = {
    "claude": ClaudeProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def available() -> list[str]:
    return sorted(_PROVIDERS)


def build(name: str, settings: Settings) -> LLMProvider:
    try:
        provider_cls = _PROVIDERS[name]
    except KeyError:
        known = ", ".join(available())
        raise KeyError(f"unknown LLM provider {name!r} (known: {known})") from None
    return provider_cls(settings.llm.for_provider(name), settings.llm)
