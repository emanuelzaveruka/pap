"""Telegram message splitting and rendering.

The 4096-character limit is a hard API constraint, and book resumes will exceed
it routinely, so the splitting boundaries are worth pinning down precisely.
No network: only the pure functions are exercised.
"""

from __future__ import annotations

import pytest

from pap.core.models import Notification
from pap.sinks.telegram import TELEGRAM_MAX_CHARS, render, split_message


def test_short_text_is_a_single_chunk():
    assert split_message("hello", limit=100) == ["hello"]


def test_empty_text_produces_no_chunks():
    assert split_message("", limit=100) == []


def test_every_chunk_respects_the_limit():
    text = "palavra " * 5000
    chunks = split_message(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


def test_chunks_stay_under_the_real_telegram_limit():
    """The default limit must leave room for the (n/m) suffix appended per part."""
    chunks = split_message("x " * 20000)
    assert all(len(c) + 32 < TELEGRAM_MAX_CHARS for c in chunks)


def test_no_content_is_lost_when_splitting():
    text = "\n\n".join(f"paragrafo {i} " + "conteudo " * 40 for i in range(30))
    rejoined = " ".join(split_message(text, limit=400)).split()
    assert rejoined == text.split()


def test_it_prefers_a_paragraph_break():
    first = "a" * 300
    second = "b" * 300
    chunks = split_message(f"{first}\n\n{second}", limit=400)
    assert chunks[0] == first
    assert chunks[1] == second


def test_it_falls_back_to_a_line_break():
    chunks = split_message("a" * 300 + "\n" + "b" * 300, limit=400)
    assert chunks[0] == "a" * 300


def test_an_unbreakable_run_is_hard_cut():
    """A 900-character URL has no break to prefer; it must still be split rather
    than sent whole and rejected by the API."""
    chunks = split_message("z" * 900, limit=400)
    assert len(chunks) == 3
    assert all(len(c) <= 400 for c in chunks)


def test_a_break_too_early_in_the_window_is_ignored():
    """Breaking at position 5 of a 400-char window would emit a 5-character chunk
    and push everything else forward, which is worse than a clean hard cut."""
    chunks = split_message("ab\n" + "c" * 900, limit=400)
    assert len(chunks[0]) > 100


def test_limit_must_be_positive():
    with pytest.raises(ValueError):
        split_message("x", limit=0)


# -- rendering --------------------------------------------------------------
def _notification(**over) -> Notification:
    base = {"dedupe_key": "k", "channel": "telegram", "title": "Titulo", "body": "Corpo"}
    return Notification(**{**base, **over})


def test_render_escapes_html_so_a_title_cannot_break_parsing():
    """Course material contains < and & constantly; unescaped, Telegram rejects
    the whole message as malformed HTML."""
    rendered = render(_notification(title="Matematica <avancada> & aplicada"))
    assert "&lt;avancada&gt;" in rendered
    assert "&amp;" in rendered


def test_render_includes_a_link_when_there_is_a_url():
    assert 'href="https://example.invalid/1"' in render(
        _notification(url="https://example.invalid/1"))


def test_render_omits_the_link_section_without_a_url():
    assert "<a href" not in render(_notification())
