"""The LLM port: provider resolution and request shaping. No network, no vendor keys.

These tests exist to protect the abstraction, not the vendors. The thing worth
guarding is that provider choice is data (config) rather than code, and that the
Claude adapter cannot emit a request shape the current API rejects.
"""

from __future__ import annotations

import pytest

from pap.config import LLMProviderSettings, LLMSettings
from pap.llm import registry
from pap.llm.base import Completion, LLMError, LLMRefusal


def _llm(**over) -> LLMSettings:
    base = {
        "default_provider": "claude",
        "purposes": {},
        "providers": {
            "claude": LLMProviderSettings("claude", "ANTHROPIC_API_KEY", "sk-a", "claude-opus-5"),
            "openai": LLMProviderSettings("openai", "OPENAI_API_KEY", "sk-o", "gpt-5"),
            "gemini": LLMProviderSettings("gemini", "GEMINI_API_KEY", "sk-g", "gemini-2.5-pro"),
        },
        "max_tokens": 8000,
        "thinking": "adaptive",
        "effort": "high",
    }
    return LLMSettings(**{**base, **over})


# -- resolution -------------------------------------------------------------
def test_a_purpose_without_an_override_uses_the_default():
    assert _llm().provider_for("book_resume") == "claude"


def test_a_purpose_override_beats_the_default():
    llm = _llm(purposes={"book_resume": "gemini"})
    assert llm.provider_for("book_resume") == "gemini"
    assert llm.provider_for("deliverable") == "claude"


def test_an_explicit_provider_beats_everything():
    """`--provider` is the escape hatch for A/B-ing two vendors on one task."""
    llm = _llm(purposes={"book_resume": "gemini"})
    assert llm.provider_for("book_resume", "openai") == "openai"


def test_an_unknown_provider_names_the_ones_that_exist():
    with pytest.raises(Exception) as exc:
        _llm().for_provider("llama")
    assert "claude" in str(exc.value)


def test_all_three_adapters_are_registered():
    """Three real adapters, not one plus two stubs — that is what proves the port."""
    assert registry.available() == ["claude", "gemini", "openai"]


def test_a_provider_without_a_key_is_not_configured():
    llm = _llm(providers={
        "claude": LLMProviderSettings("claude", "ANTHROPIC_API_KEY", "", "claude-opus-5"),
    })
    assert not llm.for_provider("claude").configured
    assert not llm.any_configured


# -- Claude request shaping -------------------------------------------------
def _claude(**over):
    from pap.llm.claude import ClaudeProvider

    llm = _llm(**over)
    return ClaudeProvider(llm.for_provider("claude"), llm)


def test_adaptive_thinking_by_default_and_no_token_budget():
    """`budget_tokens` is rejected outright on Opus 5; adaptive replaces it."""
    payload = _claude()._payload("hi", None, 1000)
    assert payload["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in str(payload)


def test_no_sampling_parameters_are_ever_sent():
    """temperature / top_p / top_k are 400s on Opus 5. Output is steered by prompting."""
    payload = _claude()._payload("hi", "sys", 1000)
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in payload


def test_disabled_thinking_is_clamped_below_xhigh_effort():
    """thinking=disabled with xhigh/max is a 400 — clamped rather than passed through
    to fail on a nightly job nobody is watching."""
    assert _claude(thinking="disabled", effort="xhigh")._effort() == "high"
    assert _claude(thinking="disabled", effort="max")._effort() == "high"


def test_disabled_thinking_keeps_a_legal_effort_untouched():
    assert _claude(thinking="disabled", effort="medium")._effort() == "medium"


def test_adaptive_thinking_never_clamps_effort():
    assert _claude(thinking="adaptive", effort="max")._effort() == "max"


def test_the_system_prompt_carries_a_cache_breakpoint():
    """The pattern spec is identical across every chunk of a book, so caching it turns
    a per-chunk cost into a one-off."""
    blocks = _claude()._payload("hi", "you are a tutor", 1000)["system"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[0]["text"] == "you are a tutor"


def test_no_system_key_when_there_is_no_system_prompt():
    assert "system" not in _claude()._payload("hi", None, 1000)


def test_refusal_fallbacks_are_enabled():
    """Opus 5 can decline a request; without this a decline is a failed job rather
    than a re-run on another model."""
    payload = _claude()._payload("hi", None, 1000)
    assert payload["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in payload["betas"]


def test_effort_lives_under_output_config():
    assert _claude(effort="low")._payload("hi", None, 10)["output_config"] == {"effort": "low"}


# -- error taxonomy ---------------------------------------------------------
def test_a_refusal_is_distinguishable_from_a_transport_failure():
    """They need different handling: a timeout should be retried, a refusal should
    not — the same prompt produces the same refusal and just spends tokens."""
    assert issubclass(LLMRefusal, LLMError)
    assert isinstance(LLMRefusal("claude", "declined"), LLMError)
    assert not isinstance(LLMError("claude", "timeout"), LLMRefusal)


def test_an_error_names_the_provider_that_failed():
    assert "gemini" in str(LLMError("gemini", "boom"))


# -- Completion -------------------------------------------------------------
def test_total_tokens_sums_what_is_present():
    assert Completion("x", "claude", "m", input_tokens=10, output_tokens=5).total_tokens == 15
    assert Completion("x", "claude", "m", input_tokens=10).total_tokens == 10


def test_total_tokens_is_none_when_the_vendor_reported_nothing():
    """A null count is honest; a fabricated zero corrupts every cost comparison."""
    assert Completion("x", "claude", "m").total_tokens is None
