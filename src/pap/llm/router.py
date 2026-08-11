"""Resolves which provider answers a task, and records what it cost.

This is the single door between domain code and any vendor. ``archives/`` and
``resumes/`` call ``complete(db, settings, purpose="deliverable", prompt=...)`` and
never learn which model replied.

Two responsibilities:

**Resolution.** ``--provider`` beats ``LLM_PROVIDER_<PURPOSE>`` beats
``LLM_PROVIDER_DEFAULT``. A purpose with no explicit setting falls through to the
default rather than erroring, so adding a new purpose never requires touching
``.env`` first.

**Accounting.** Every call — successful, failed or refused — writes a ``pap.llm_call``
row with provider, model, token counts and latency. That is what makes "which
vendor is actually cheaper for book resumes" a SQL query instead of a guess, and it
is why the ledger write happens in a ``finally``: a failed call is exactly the one
you most want a record of.
"""

from __future__ import annotations

import logging
import time

from ..config import Settings
from ..db import Database
from . import registry
from .base import Completion, LLMError, LLMProvider

log = logging.getLogger(__name__)


def resolve(settings: Settings, purpose: str, override: str | None = None) -> LLMProvider:
    """The provider that should answer ``purpose``, built and ready."""
    name = settings.llm.provider_for(purpose, override)
    provider = registry.build(name, settings)
    if not provider.configured:
        key_var = settings.llm.for_provider(name).key_var
        raise LLMError(
            name,
            f"selected for purpose {purpose!r} but not configured — set {key_var} "
            f"(and LLM_MODEL_{name.upper()} if you need a specific model) in .env",
        )
    return provider


def complete(
    db: Database | None,
    settings: Settings,
    *,
    purpose: str,
    prompt: str,
    system: str | None = None,
    max_tokens: int | None = None,
    provider: str | None = None,
    run_id: int | None = None,
) -> Completion:
    """Run one completion for ``purpose`` and record it.

    ``db`` may be None so that `pap llm ping` works before `pap migrate` has run —
    a provider check shouldn't depend on the schema existing.
    """
    chosen = resolve(settings, purpose, provider)
    limit = max_tokens or settings.llm.max_tokens

    started = time.monotonic()
    error: Exception | None = None
    result: Completion | None = None
    try:
        result = chosen.complete(prompt, system=system, max_tokens=limit)
        return result
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        error = exc
        raise
    finally:
        if db is not None:
            elapsed_ms = (result.latency_ms if result and result.latency_ms is not None
                          else int((time.monotonic() - started) * 1000))
            try:
                db.record_llm_call(
                    purpose=purpose,
                    provider=chosen.name,
                    model=result.model if result else chosen.model,
                    input_tokens=result.input_tokens if result else None,
                    output_tokens=result.output_tokens if result else None,
                    latency_ms=elapsed_ms,
                    ok=error is None,
                    error_text=None if error is None else f"{type(error).__name__}: {error}",
                    run_id=run_id,
                )
            except Exception:  # noqa: BLE001 - accounting must never fail the work
                log.warning("could not record the llm_call row", exc_info=True)


def ping_all(settings: Settings) -> list[tuple[str, Completion | Exception]]:
    """One trivial call per configured provider, for side-by-side comparison.

    Unconfigured providers are skipped rather than reported as failures — the point
    is to compare the ones you actually have keys for.
    """
    results: list[tuple[str, Completion | Exception]] = []
    for name in registry.available():
        provider = registry.build(name, settings)
        if not provider.configured:
            continue
        try:
            results.append((name, provider.ping()))
        except Exception as exc:  # noqa: BLE001 - one dead provider must not hide the rest
            results.append((name, exc))
    return results
