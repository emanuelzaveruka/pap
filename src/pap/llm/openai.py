"""OpenAI adapter — the only file in this project that imports ``openai``.

Deliberately contains no Anthropic concepts. Thinking configuration, effort levels
and refusal-fallback routing are Claude-specific and live in ``claude.py``; the
shared vocabulary between the two is ``Completion`` and nothing more. Keeping the
adapters ignorant of each other is what makes the port real — the moment one starts
translating another's parameters, the domain has effectively learned which vendor
it is talking to.

``LLM_MODEL_OPENAI`` defaults to a recent model name but **should be set explicitly**
to one your key can actually access; model availability varies per account and a
wrong name surfaces as a confusing 404 rather than an auth error.
"""

from __future__ import annotations

import logging
import time

from ..config import LLMProviderSettings, LLMSettings
from .base import (PING_MAX_TOKENS, PING_PROMPT, Completion, LLMError,
                   LLMRefusal, verified_ping)

log = logging.getLogger(__name__)


class OpenAIProvider:
    name = "openai"

    def __init__(self, provider: LLMProviderSettings, llm: LLMSettings) -> None:
        self.settings = provider
        self.llm = llm
        self._client = None

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def configured(self) -> bool:
        return self.settings.configured

    @property
    def client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - optional install
                raise LLMError(self.name, "the `openai` package is not installed") from exc
            if not self.settings.api_key:
                raise LLMError(self.name, "OPENAI_API_KEY is not set")
            self._client = OpenAI(api_key=self.settings.api_key)
        return self._client

    def complete(self, prompt: str, *, system: str | None = None,
                 max_tokens: int = 8000) -> Completion:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_completion_tokens=max_tokens,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - normalised for the ledger
            raise LLMError(self.name, f"{type(exc).__name__}: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        choice = response.choices[0]
        finish = getattr(choice, "finish_reason", None)
        message = choice.message
        # Some models expose a dedicated refusal field; treat it the same way as a
        # Claude refusal so the domain sees one behaviour across vendors.
        if getattr(message, "refusal", None):
            raise LLMRefusal(self.name, str(message.refusal)[:200])
        if finish == "content_filter":
            raise LLMRefusal(self.name, "the response was filtered")

        usage = getattr(response, "usage", None)
        return Completion(
            text=(message.content or "").strip(),
            provider=self.name,
            model=getattr(response, "model", self.model),
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=elapsed_ms,
            stop_reason=finish,
        )

    def count_tokens(self, text: str, *, system: str | None = None) -> int:
        """Approximate, and honest about it.

        OpenAI exposes no server-side counting endpoint, so this is a character
        heuristic rather than a real tokenizer. It is used only to size chunks
        before sending, and the caller should leave headroom accordingly. A local
        tokenizer library would be a guess about the model's tokenizer wearing the
        costume of a measurement.
        """
        total = len(text) + (len(system) if system else 0)
        return max(1, total // 4)

    def ping(self) -> Completion:
        return verified_ping(self.complete(PING_PROMPT, max_tokens=PING_MAX_TOKENS))
