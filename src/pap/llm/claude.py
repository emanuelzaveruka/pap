"""Claude adapter — the only file in this project that imports ``anthropic``.

Uses the official Anthropic SDK rather than raw HTTP. That matters more than it
looks: the SDK owns retry/backoff on 429 and 5xx, the streaming accumulator, and
the response typing, and it tracks API changes that a hand-rolled client would
silently drift away from.

Four things here are specific to current Claude models and are the difference
between working and mysteriously broken:

**Adaptive thinking, never a token budget.** ``thinking={"type": "adaptive"}`` lets
the model decide how much to think; depth is steered with ``output_config.effort``.
The old fixed ``budget_tokens`` form is rejected outright on Opus 5.

**No sampling parameters.** ``temperature``, ``top_p`` and ``top_k`` are rejected
on Opus 5. Output is steered by prompting, which is why the pattern documents in
``patterns/`` carry the formatting rules rather than a temperature setting.

**Disabling thinking is capped at ``high`` effort.** ``thinking=disabled`` paired
with ``xhigh``/``max`` is a 400, so the combination is clamped below rather than
passed through to fail at runtime.

**A refusal is an HTTP 200.** Safety classifiers can decline a request and the
response arrives successful, with ``stop_reason == "refusal"`` and an empty or
partial ``content``. Code that reads ``content[0].text`` unconditionally breaks on
it — so ``stop_reason`` is checked *before* the content is touched, and server-side
fallbacks are enabled so a decline is re-run on another model instead of surfacing
as a failed job.
"""

from __future__ import annotations

import logging
import time

from ..config import LLMProviderSettings, LLMSettings
from .base import Completion, LLMError, LLMRefusal

log = logging.getLogger(__name__)

# Above this, request streaming: the SDK refuses non-streaming requests it estimates
# will outlive the HTTP timeout, and book resumes routinely land in that territory.
STREAM_ABOVE_MAX_TOKENS = 16_000

# Effort levels that cannot be combined with thinking disabled (400 on Opus 5).
_EFFORT_REQUIRING_THINKING = frozenset({"xhigh", "max"})

# Routes a policy decline to Anthropic's recommended substitute automatically,
# rather than pinning a model here that would need migrating later.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    name = "claude"

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
                import anthropic
            except ImportError as exc:  # pragma: no cover - optional install
                raise LLMError(self.name, "the `anthropic` package is not installed") from exc
            if not self.settings.api_key:
                raise LLMError(self.name, "ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic(api_key=self.settings.api_key)
        return self._client

    # -- request shaping ----------------------------------------------------
    def _thinking(self) -> dict:
        return ({"type": "disabled"} if self.llm.thinking == "disabled"
                else {"type": "adaptive"})

    def _effort(self) -> str:
        effort = self.llm.effort
        if self.llm.thinking == "disabled" and effort in _EFFORT_REQUIRING_THINKING:
            # Pairing these is a 400. Clamping is better than letting a config
            # combination nobody tested take down a nightly job.
            log.warning("LLM_EFFORT=%s cannot be combined with thinking disabled — "
                        "using 'high' for this request", effort)
            return "high"
        return effort

    def _system(self, system: str | None) -> list | None:
        if not system:
            return None
        # A cache breakpoint on the system block: the pattern spec and persona are
        # identical across every chunk of a book, so caching turns a per-chunk cost
        # into a one-off. Volatile content stays in the user turn, after this point.
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _payload(self, prompt: str, system: str | None, max_tokens: int) -> dict:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "thinking": self._thinking(),
            "output_config": {"effort": self._effort()},
            "betas": [_FALLBACK_BETA],
            "fallbacks": "default",
            "messages": [{"role": "user", "content": prompt}],
        }
        if (system_blocks := self._system(system)) is not None:
            payload["system"] = system_blocks
        return payload

    # -- the port -----------------------------------------------------------
    def complete(self, prompt: str, *, system: str | None = None,
                 max_tokens: int = 8000) -> Completion:
        payload = self._payload(prompt, system, max_tokens)
        started = time.monotonic()
        try:
            if max_tokens > STREAM_ABOVE_MAX_TOKENS:
                with self.client.beta.messages.stream(**payload) as stream:
                    message = stream.get_final_message()
            else:
                message = self.client.beta.messages.create(**payload)
        except Exception as exc:  # noqa: BLE001 - normalised for the ledger
            raise LLMError(self.name, f"{type(exc).__name__}: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        # Checked before touching content: on a refusal, content is empty or partial.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise LLMRefusal(self.name, f"the request was declined ({category})")

        text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
        usage = message.usage
        return Completion(
            text=text.strip(),
            provider=self.name,
            model=getattr(message, "model", self.model),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            latency_ms=elapsed_ms,
            stop_reason=message.stop_reason,
            meta={
                # Proof the system-prompt cache is working. If this stays 0 across
                # runs with the same pattern spec, something volatile crept in ahead
                # of the breakpoint.
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(
                    usage, "cache_creation_input_tokens", None),
            },
        )

    def count_tokens(self, text: str, *, system: str | None = None) -> int:
        kwargs = {"model": self.model, "messages": [{"role": "user", "content": text}]}
        if system:
            kwargs["system"] = system
        try:
            return self.client.messages.count_tokens(**kwargs).input_tokens
        except Exception as exc:  # noqa: BLE001
            raise LLMError(self.name, f"count_tokens failed: {exc}") from exc

    def ping(self) -> Completion:
        return self.complete("Reply with the single word: ok", max_tokens=16)
