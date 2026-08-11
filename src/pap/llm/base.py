"""The LLM port: the only thing `archives/` and `resumes/` are allowed to know.

This is the boundary you asked for. Domain code — the MAPA deliverable builder, the
book resume feed — depends on ``LLMProvider`` and nothing else. It never imports
``anthropic``, ``openai`` or ``google.genai``, never branches on which vendor is in
play, and never sees a vendor-shaped response. Swapping Claude for Gemini on the
book feed is a line in ``.env``.

Each **adapter** imports exactly one vendor's official SDK — `claude.py` imports
`anthropic`, `openai.py` imports `openai`, `gemini.py` imports `google.genai`. No
adapter imports another's, and no adapter is a generic HTTP client pointed at a
compatibility shim: vendors differ in ways that matter (thinking, token accounting,
refusals), and hiding that behind one hand-rolled client is how you end up with a
port that only really works for one of them.

Two design choices worth stating, because they are what make the port useful rather
than decorative:

**Three real adapters, not one plus two stubs.** A single implementation cannot
show you where the abstraction leaks. Building all three is what forced
``Completion`` to carry token counts every vendor reports differently, and
``count_tokens`` to exist at all.

**``count_tokens`` is part of the port.** The book chunker has to size chunks before
sending them, and each vendor tokenizes differently — so the question "how big is
this?" has to be answered by the provider that will receive it, not by a local
approximation. This is the field that stops Phase 4 from reaching for ``tiktoken``
and silently mis-sizing every Claude request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    """A provider call failed. Carries which provider, for the ledger and logs."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider


class LLMRefusal(LLMError):
    """The model declined to answer.

    Kept distinct from a transport failure because the correct response differs:
    a timeout should be retried, a refusal should not — retrying the same prompt
    produces the same refusal and just spends tokens. Current frontier models
    return this as a *successful* HTTP response, so it has to be checked for
    rather than caught.
    """


@dataclass(frozen=True)
class Completion:
    """One provider response, in the shape the domain works with.

    Token counts are optional because vendors report usage differently and some
    report nothing at all on some paths. ``llm_call`` stores whatever arrived —
    a null token count is honest; a fabricated one corrupts every cost comparison
    built on top of it.
    """

    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    stop_reason: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


@runtime_checkable
class LLMProvider(Protocol):
    """What every vendor adapter provides.

    ``complete`` is deliberately narrow — a prompt, an optional system prompt, a
    token ceiling. Vendor-specific knobs (thinking depth, effort, safety
    fallbacks) are the adapter's business, configured from settings, and must not
    leak into this signature. The moment a caller has to pass a vendor's parameter
    through the port, the port has stopped being one.
    """

    name: str

    @property
    def model(self) -> str: ...

    @property
    def configured(self) -> bool: ...

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 8000,
    ) -> Completion: ...

    def count_tokens(self, text: str, *, system: str | None = None) -> int:
        """Tokens this provider will bill for this text.

        Called by the chunker before sending anything, so chunks are sized against
        the tokenizer that will actually process them.
        """
        ...

    def ping(self) -> Completion:
        """The cheapest possible round trip, for `pap llm ping` and `doctor --live`."""
        ...
