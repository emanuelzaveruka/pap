"""Selects the part of a book that is worth putting in front of the model.

The disciplina's livro is the source a deliverable should be grounded in, but a
book is ~170 pages and ~57k tokens. Sending all of it is possible on a large
context window and still wrong: it costs tokens on every generation and dilutes
the few pages that actually answer the brief.

So this module extracts page text once and selects the pages that overlap the
brief. Selection is deliberately **lexical, not semantic** — term overlap, no
embeddings, no vector store, no extra service. That is enough when the brief and
the book share a vocabulary, which is exactly the case for a course book and its
own activity, and it keeps a nightly job free of another dependency to operate.
When it stops being enough, the replacement is a better `score_page`, not a
different pipeline.

Pages are returned **in document order with their page numbers**, for two
reasons: the argument of a textbook depends on order, and a cited page number is
what lets a generated `Referências` entry point at something real instead of
being invented.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Default ceiling for the selected extract. Comfortably inside every current
# model's context while leaving room for the pattern spec, the brief and the
# answer itself.
DEFAULT_MAX_CHARS = 60_000

# A page with less than this is a cover, a divider or a figure caption.
MIN_PAGE_CHARS = 200

# Portuguese function words plus the scaffolding that appears in every brief.
# Without this the top-scoring pages are simply the wordiest ones.
#
# Deliberately absent: `analise`. Accent-folding makes the imperative "analise"
# and the noun "análise" the same token, so listing it would drop a content word
# from every engineering brief.
#
# Keep this a bare word list — a comment inside the literal would be split into
# tokens and silently become stopwords itself.
_STOPWORDS = frozenset("""
a as o os um uma uns umas de do da dos das em no na nos nas por para com sem sob
sobre entre ate apos e ou mas que quais qual quando como onde porque se ja nao
sim tambem muito mais menos todo toda todos todas cada seu sua seus suas este
esta estes estas esse essa esses essas aquele aquela isso isto ser estar ter
haver fazer pode podem deve devem sao foi era pelo pela pelos pelas ao aos
atividade valor prazo pontos ponto questao questoes responda apresente descreva
explique considere cenario tarefa aluno academico disciplina curso mapa
""".split())


def _fold(text: str) -> str:
    """Lowercase and strip accents so `análise` matches `analise`."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def terms(text: str) -> set[str]:
    """Significant words: folded, 4+ characters, not a stopword."""
    return {
        word for word in re.findall(r"[a-z]{4,}", _fold(text))
        if word not in _STOPWORDS
    }


@dataclass(frozen=True)
class Page:
    number: int  # 1-based, as printed in a citation
    text: str


def extract_pages(pdf_path: str, *, min_chars: int = MIN_PAGE_CHARS) -> list[Page]:
    """Text per page. Pages with almost nothing on them are dropped."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - optional until Phase 3/4
        raise RuntimeError("the `pypdf` package is not installed") from exc

    reader = PdfReader(pdf_path)
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if len(text) >= min_chars:
            pages.append(Page(index, text))
    log.info("extracted %d usable pages from %s", len(pages), pdf_path)
    return pages


def term_weights(pages: list[Page], wanted: set[str]) -> dict[str, float]:
    """Weight each query term by how *rare* it is in this book.

    Plain overlap counts every match equally, which ranks the page that happens
    to use the most common words — measured against the real book, the top hit
    for a Scrum brief was the author's CV, on the strength of "desenvolvimento",
    "gestão" and "aplicação". A term appearing on nearly every page carries no
    information about which page to send; one appearing on six does.
    """
    total = len(pages) or 1
    frequency = {term: 0 for term in wanted}
    for page in pages:
        present = terms(page.text) & wanted
        for term in present:
            frequency[term] += 1
    # log(total / df), floored at zero so a term on every page contributes nothing.
    import math
    return {
        term: max(0.0, math.log(total / count)) if count else 0.0
        for term, count in frequency.items()
    }


def score_page(page: Page, wanted: set[str], weights: dict[str, float] | None = None) -> float:
    """Share of the brief's *information* that appears on this page.

    Normalised by the total available weight rather than by page length, so a
    long page is not rewarded merely for containing more words.
    """
    if not wanted:
        return 0.0
    matched = wanted & terms(page.text)
    if weights is None:
        return len(matched) / len(wanted)
    total = sum(weights.values())
    if total <= 0:
        return len(matched) / len(wanted)
    return sum(weights.get(term, 0.0) for term in matched) / total


# The ficha catalográfica — author, publisher, year, ISBN — lives on the first
# page or two and scores near zero against any real brief, so relevance ranking
# throws away exactly the data a reference list needs. Measured consequence: asked
# for references, the model produced "UNICESUMAR. Tópicos em Computação II. 2019"
# when the book's own page 2 says "JOSÉ, Maria Isabel Jacob … Unicesumar, 2018".
# The source was real and the citation was invented, which is the worst
# combination available — it looks checkable and is wrong.
FRONT_MATTER_PAGES = 2


def select_context(
    pages: list[Page],
    query: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    front_matter_pages: int = FRONT_MATTER_PAGES,
) -> tuple[str, list[int]]:
    """Pick the pages most relevant to `query`, plus the book's front matter.

    Returns the extract and the page numbers used. Pages are emitted in document
    order regardless of score, and each is labelled with its page number so a
    citation can point at a real page.
    """
    if not pages:
        return "", []

    front = pages[:front_matter_pages]
    front_numbers = {p.number for p in front}

    wanted = terms(query)
    weights = term_weights(pages, wanted)
    scored = [(score_page(p, wanted, weights), p) for p in pages]
    ranked = sorted(scored, key=lambda pair: (pair[0], -pair[1].number), reverse=True)

    chosen: list[Page] = []
    budget = max_chars - sum(len(p.text) + 40 for p in front)
    for score, page in ranked:
        if score <= 0:
            break
        if page.number in front_numbers:
            continue  # already carried as front matter
        cost = len(page.text) + 40  # the page marker
        if cost > budget:
            continue  # a later, shorter page may still fit
        chosen.append(page)
        budget -= cost

    if not chosen:  # nothing overlapped — fall back to the opening pages
        log.warning("no page overlapped the brief; falling back to the first pages")
        for page in pages:
            cost = len(page.text) + 40
            if cost > budget:
                break
            chosen.append(page)
            budget -= cost

    chosen.sort(key=lambda p: p.number)
    sections = []
    if front:
        sections.append(
            "## DADOS BIBLIOGRÁFICOS DA OBRA (use estes dados para as referências; "
            "não invente autor, editora, ano ou edição)\n\n"
            + "\n\n".join(f"[página {p.number}]\n{p.text}" for p in front)
        )
    if chosen:
        sections.append(
            "## TRECHOS SELECIONADOS\n\n"
            + "\n\n".join(f"[página {p.number}]\n{p.text}" for p in chosen)
        )
    return "\n\n".join(sections), [p.number for p in front] + [p.number for p in chosen]


def book_context(pdf_path: str, query: str, *, max_chars: int = DEFAULT_MAX_CHARS
                 ) -> tuple[str, list[int]]:
    """Convenience: extract and select in one call."""
    return select_context(extract_pages(pdf_path), query, max_chars=max_chars)
