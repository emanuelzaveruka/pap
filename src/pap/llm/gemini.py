"""Gemini adapter — the only file in this project that imports ``google.genai``.

Uses the current ``google-genai`` SDK, not the legacy ``google-generativeai``
package. Like the OpenAI adapter, it carries no concepts from the other vendors.

Gemini is the natural choice for ``LLM_PROVIDER_BOOK_RESUME``: its context window
comfortably holds whole chapters, which means fewer, larger chunks and better
continuity across a resume than many small ones would give.

``LLM_MODEL_GEMINI`` defaults to a recent model but **should be set explicitly** to
one your key can access. ``models.list()`` is not proof of access: it still returns
``gemini-2.5-pro``, while ``generateContent`` answers 404 "no longer available to
new users" for any key created after it was closed off. The only reliable check is
a real call — which is what ``pap llm ping`` is for.
"""

from __future__ import annotations

import logging
import time

from ..config import LLMProviderSettings, LLMSettings
from .base import (PING_MAX_TOKENS, PING_PROMPT, Completion, LLMError,
                   LLMRefusal, verified_ping)

log = logging.getLogger(__name__)

# Gemini reports why generation stopped; these mean "declined", not "finished".
_REFUSAL_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}


class GeminiProvider:
    name = "gemini"

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
                from google import genai
            except ImportError as exc:  # pragma: no cover - optional install
                raise LLMError(self.name, "the `google-genai` package is not installed") from exc
            if not self.settings.api_key:
                raise LLMError(self.name, "GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=self.settings.api_key)
        return self._client

    def _config(self, system: str | None, max_tokens: int):
        from google.genai import types

        return types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            system_instruction=system or None,
        )

    def complete(self, prompt: str, *, system: str | None = None,
                 max_tokens: int = 8000) -> Completion:
        started = time.monotonic()
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=self._config(system, max_tokens),
            )
        except Exception as exc:  # noqa: BLE001 - normalised for the ledger
            raise LLMError(self.name, f"{type(exc).__name__}: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        # A blocked prompt yields no candidates at all, so this is checked before
        # anything tries to read text off the response.
        feedback = getattr(response, "prompt_feedback", None)
        if getattr(feedback, "block_reason", None):
            raise LLMRefusal(self.name, f"prompt blocked ({feedback.block_reason})")

        candidates = getattr(response, "candidates", None) or []
        finish = str(getattr(candidates[0], "finish_reason", "")) if candidates else ""
        if any(reason in finish.upper() for reason in _REFUSAL_REASONS):
            raise LLMRefusal(self.name, f"generation stopped: {finish}")

        usage = getattr(response, "usage_metadata", None)
        answer_tokens = getattr(usage, "candidates_token_count", None)
        thought_tokens = getattr(usage, "thoughts_token_count", None)

        # Gemini 3.x thinks by default and reports those tokens SEPARATELY from
        # the answer, but bills them as output. Reporting only the answer made a
        # 72-thought/1-answer response look like one output token, which would
        # have understated Gemini by two orders of magnitude in `pap llm usage` —
        # the measurement the LLM_PROVIDER_* choices are supposed to rest on.
        # None is preserved when the vendor reported nothing: a fabricated zero
        # corrupts the comparison just as badly in the other direction.
        output_tokens = None
        if answer_tokens is not None or thought_tokens is not None:
            output_tokens = (answer_tokens or 0) + (thought_tokens or 0)

        return Completion(
            text=(getattr(response, "text", None) or "").strip(),
            provider=self.name,
            model=self.model,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            stop_reason=finish or None,
            meta={"answer_tokens": answer_tokens, "thought_tokens": thought_tokens},
        )

    def count_tokens(self, text: str, *, system: str | None = None) -> int:
        """Server-side count — Gemini exposes a real counting endpoint."""
        payload = f"{system}\n\n{text}" if system else text
        try:
            result = self.client.models.count_tokens(model=self.model, contents=payload)
            return int(result.total_tokens)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(self.name, f"count_tokens failed: {exc}") from exc

    def ping(self) -> Completion:
        return verified_ping(self.complete(PING_PROMPT, max_tokens=PING_MAX_TOKENS))
