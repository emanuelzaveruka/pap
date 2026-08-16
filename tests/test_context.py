"""Selecting the part of a book worth sending. Pure logic — no PDF, no network."""

from __future__ import annotations

from pap.archives.context import (Page, score_page, select_context, term_weights,
                                  terms)


def _pages(*texts: str) -> list[Page]:
    return [Page(i, t) for i, t in enumerate(texts, start=1)]


def test_accents_do_not_split_a_term():
    assert "analise" in terms("Análise de requisitos")
    assert terms("gestão") == terms("gestao")


def test_stopwords_and_brief_scaffolding_are_ignored():
    """Without this the top-scoring page is simply the wordiest one."""
    found = terms("Responda a questão sobre o cenário da atividade e o prazo")
    assert not {"responda", "questao", "cenario", "atividade", "prazo"} & found


def test_a_term_on_every_page_carries_no_weight():
    """Rarity weighting exists because plain overlap ranked the author's CV top
    for a Scrum brief, on the strength of 'desenvolvimento' and 'gestao'."""
    pages = _pages("scrum sprint backlog processo", "kanban lean processo",
                   "testes unitarios processo")
    weights = term_weights(pages, {"processo", "scrum"})
    assert weights["processo"] == 0.0
    assert weights["scrum"] > 0


def test_the_rare_term_decides_the_ranking():
    pages = _pages("processo requisitos", "processo scrum sprint")
    wanted = {"processo", "scrum"}
    weights = term_weights(pages, wanted)
    assert score_page(pages[1], wanted, weights) > score_page(pages[0], wanted, weights)


def test_front_matter_is_always_included_even_though_it_scores_zero():
    """Regression: the ficha catalográfica scores near zero against any brief, so
    relevance ranking dropped exactly the page carrying author, publisher and
    year. Asked for references, the model then invented
    'UNICESUMAR … 2019' where page 2 says 'JOSÉ, Maria Isabel Jacob … 2018'."""
    pages = _pages("JOSE Maria Isabel Jacob Unicesumar 2018 ISBN ficha catalografica",
                   "prefacio institucional bem vindo academico",
                   "scrum sprint planning backlog impedimentos")
    extract, used = select_context(pages, "scrum sprint planning", front_matter_pages=2)
    assert 1 in used, "the citation page must survive selection"
    assert "2018" in extract
    assert "DADOS BIBLIOGRÁFICOS" in extract


def test_selected_pages_are_emitted_in_document_order():
    """A textbook's argument depends on order, so a high-scoring later page must
    not be moved ahead of an earlier one."""
    pages = _pages("intro", "meio scrum", "fim scrum sprint backlog")
    extract, used = select_context(pages, "scrum sprint backlog", front_matter_pages=1)
    assert used == sorted(used)
    assert extract.index("[página 2]") < extract.index("[página 3]")


def test_pages_are_labelled_so_a_citation_can_name_one():
    extract, _ = select_context(_pages("scrum backlog"), "scrum", front_matter_pages=0)
    assert "[página 1]" in extract


def test_the_budget_is_respected():
    pages = _pages(*["scrum " * 500 for _ in range(20)])
    extract, used = select_context(pages, "scrum", max_chars=4000, front_matter_pages=0)
    assert len(extract) <= 4000
    assert 0 < len(used) < 20


def test_no_overlap_falls_back_to_the_opening_pages_rather_than_nothing():
    """Sending no context at all is worse than sending the start of the book."""
    extract, used = select_context(_pages("alpha beta", "gamma delta"),
                                   "zzzz yyyy", front_matter_pages=0)
    assert used and extract


def test_an_empty_book_is_not_an_error():
    assert select_context([], "scrum") == ("", [])


def test_the_stopword_list_contains_only_words():
    """Guard: a comment written inside the literal gets split by `.split()` and
    silently becomes stopwords — 'content', 'token' and 'brief' all did once."""
    from pap.archives.context import _STOPWORDS

    assert all(w.isalpha() for w in _STOPWORDS), [w for w in _STOPWORDS if not w.isalpha()]
    assert "analise" not in _STOPWORDS, "folds together with the noun 'análise'"
    assert not {"content", "token", "brief", "would"} & _STOPWORDS
