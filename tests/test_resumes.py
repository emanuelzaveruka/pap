"""The book resume feed: unit splitting and pass behaviour. No PDF, no network."""

from __future__ import annotations

import pytest

from pap.archives.context import Page
from pap.config import ResumeSettings
from pap.resumes.chunker import (MIN_UNIT_CHARS, _clean_title, _usable_title,
                                 chapter_units, page_units)
from pap.resumes.feed import (build_prompt, pass_instruction, resume_dedupe_key)


def _pages(count: int, chars: int = 400, start: int = 1) -> list[Page]:
    return [Page(start + i, f"conteudo {i} " + "x" * chars) for i in range(count)]


# -- outline titles ---------------------------------------------------------
@pytest.mark.parametrize("title", ["h.kcjsbc5tuzc5", "_Toc123456789", "_GoBack",
                                   "bookmark12", "", "  ", "12"])
def test_anchor_titles_are_not_chapters(title):
    """Measured in the real books: 81 Google Docs anchors in fundamentos-de-redes,
    and `_GoBack` — Word's return-to-last-edit bookmark — became a 7-page
    'chapter' at the end of empreendedorismo."""
    assert not _usable_title(title)


@pytest.mark.parametrize("title", ["Introdução", "PLANO DE NEGÓCIO",
                                   "Aplicações das Redes de Computadores"])
def test_real_chapter_titles_survive(title):
    assert _usable_title(title)


def test_a_title_broken_across_lines_is_collapsed():
    """A PDF outline keeps the printed page's line breaks, so a title arrives as
    `DEFININDO UM\\nMODELO\\nDE NEGÓCIO` and would reach Telegram that way."""
    assert _clean_title("DEFININDO UM\nMODELO\nDE NEGÓCIO") == "DEFININDO UM MODELO DE NEGÓCIO"


# -- chapter units ----------------------------------------------------------
def test_one_outline_entry_is_not_a_table_of_contents():
    """estudo-contemporâneo has exactly one usable entry; treating it as a chapter
    list would produce a single unit covering the whole book."""
    assert chapter_units(_pages(20), [(1, "COMUNICAÇÃO ASSERTIVA")]) == []


def test_chapters_are_bounded_by_the_next_entry():
    units = chapter_units(_pages(30), [(1, "Um"), (11, "Dois"), (21, "Três")])
    assert [u.label for u in units] == ["Um", "Dois", "Três"]
    assert (units[0].start_page, units[0].end_page) == (1, 10)
    assert (units[1].start_page, units[1].end_page) == (11, 20)
    assert units[2].end_page == 30, "the last chapter runs to the end of the book"


def test_a_divider_page_is_not_a_unit():
    """A one-page entry with almost no text is a title page, not a chapter worth
    its own message."""
    pages = [Page(1, "x" * (MIN_UNIT_CHARS + 100)), Page(2, "tiny"), Page(3, "y" * 3000)]
    units = chapter_units(pages, [(1, "Real"), (2, "Divisória"), (3, "Outro")])
    assert [u.label for u in units] == ["Real", "Outro"]


def test_unit_indexes_are_contiguous_after_a_unit_is_dropped():
    """Index is the delivery cursor — a gap would skip a unit forever."""
    pages = [Page(1, "x" * 3000), Page(2, "tiny"), Page(3, "y" * 3000)]
    units = chapter_units(pages, [(1, "A"), (2, "B"), (3, "C")])
    assert [u.index for u in units] == list(range(len(units)))


# -- page units -------------------------------------------------------------
def test_page_units_split_evenly_and_label_their_range():
    units = page_units(_pages(25), pages_per_unit=10)
    assert len(units) == 3
    assert units[0].label == "páginas 1–10"
    assert units[2].pages == 5, "the last unit is short, not padded"


def test_page_units_reject_a_nonsense_size():
    with pytest.raises(ValueError):
        page_units(_pages(3), pages_per_unit=0)


# -- passes -----------------------------------------------------------------
def test_the_first_pass_adds_no_extra_instruction():
    assert pass_instruction(1) == ""


def test_the_second_pass_asks_for_more_than_the_first():
    """Re-summoning must produce a deeper pass, not the same notes again —
    otherwise the feature is pointless."""
    text = pass_instruction(2)
    assert "SEGUNDA" in text
    assert "perguntas" in text


def test_a_third_pass_still_asks_for_depth_rather_than_falling_back_to_nothing():
    assert pass_instruction(3)
    assert "3" in pass_instruction(3)


def test_the_prompt_carries_the_unit_text_and_its_pages():
    from pap.resumes.chunker import Unit

    unit = Unit(0, "PLANO DE NEGÓCIO", 90, 128, "conteudo do capitulo")
    prompt = build_prompt(unit, ResumeSettings(), 1, "EMPREENDEDORISMO")
    assert "conteudo do capitulo" in prompt
    assert "PLANO DE NEGÓCIO" in prompt
    assert "90" in prompt and "128" in prompt
    assert "EMPREENDEDORISMO" in prompt


def test_the_style_setting_changes_the_prompt():
    from pap.resumes.chunker import Unit

    unit = Unit(0, "X", 1, 2, "texto")
    notes = build_prompt(unit, ResumeSettings(style="study-notes"), 1, "L")
    outline = build_prompt(unit, ResumeSettings(style="outline"), 1, "L")
    assert notes != outline


# -- dedupe -----------------------------------------------------------------
def test_each_unit_of_each_pass_notifies_once():
    a = resume_dedupe_key(4, 1, 0, "telegram")
    assert a == resume_dedupe_key(4, 1, 0, "telegram")
    assert a != resume_dedupe_key(4, 2, 0, "telegram")   # second pass
    assert a != resume_dedupe_key(4, 1, 1, "telegram")   # next unit
    assert a != resume_dedupe_key(5, 1, 0, "telegram")   # other book
    assert a != resume_dedupe_key(4, 1, 0, "email")      # other channel


# -- per-book config --------------------------------------------------------
def test_a_book_can_override_the_defaults():
    merged = ResumeSettings().merged({"resume": {"target_words": 300, "unit": "page"}})
    assert (merged.target_words, merged.unit) == (300, "page")
    assert merged.style == ResumeSettings().style, "untouched fields keep the default"


def test_an_empty_config_changes_nothing():
    assert ResumeSettings().merged(None) == ResumeSettings()
    assert ResumeSettings().merged({}) == ResumeSettings()


def test_an_unknown_key_is_ignored_rather_than_crashing():
    assert ResumeSettings().merged({"resume": {"nonsense": 1}}) == ResumeSettings()
