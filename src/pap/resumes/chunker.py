"""Splits a book into the units the resume feed delivers one at a time.

A unit is what one message covers, and `book.total_units` is set from it once.
That number is what makes progress and completion knowable — without it there is
no "12 of 30" and no way to say the book is finished.

**The PDF outline is not to be trusted.** Measured across the four books already
downloaded:

- `tópicos-em-computação-ii` — no outline at all
- `fundamentos-de-redes` — 81 entries, every title a Google Docs anchor
  (`h.kcjsbc5tuzc5`) rather than a chapter name
- `empreendedorismo` — 12 entries, but a chapter's title is split across
  consecutive entries pointing at the same page (`INTRODUÇÃO AO` then
  `EMPREENDEDORISMO`, both page 9)
- `estudo-contemporâneo` — 2 entries, both page 5

So chapter splitting is the exception and page splitting is the normal path. The
code reflects that: an outline is *offered* and accepted only if it survives
validation, and anything else falls back to fixed page units rather than
producing one 170-page unit or thirty units named `h.qyspu25dbmf4`.

Splitting is deliberately deterministic and content-free — no model call. The
resume is generated per unit at delivery time; deciding the boundaries is
arithmetic, and arithmetic should not cost tokens or vary between runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..archives.context import Page, extract_pages

log = logging.getLogger(__name__)

DEFAULT_PAGES_PER_UNIT = 12

# A chapter shorter than this is a title page or a divider, not a unit worth a
# message of its own.
MIN_UNIT_CHARS = 1_500

# Google Docs and Word both export anchor names as outline titles when a document
# has no real headings. Seen in the real books: `h.kcjsbc5tuzc5` (Google Docs, 81
# of them in fundamentos-de-redes) and `_GoBack` (Word's return-to-last-edit
# bookmark, which became a 7-page "chapter" at the end of empreendedorismo).
_ANCHOR_TITLE = re.compile(
    r"^(h\.[a-z0-9]{6,}|_Toc\d+|_GoBack|bookmark\d+|Text\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Unit:
    """One delivery. `index` is 0-based and stable for the life of a pass."""

    index: int
    label: str
    start_page: int
    end_page: int
    text: str

    @property
    def pages(self) -> int:
        return self.end_page - self.start_page + 1

    @property
    def words(self) -> int:
        return len(self.text.split())


def _clean_title(title: str) -> str:
    """Collapse whitespace. A PDF outline keeps the line breaks from the printed
    page, so a title arrives as `DEFININDO UM\\nMODELO\\nDE NEGÓCIO` and would be
    sent to Telegram with them intact."""
    return " ".join((title or "").split())


def _usable_title(title: str) -> bool:
    title = _clean_title(title)
    if len(title) < 3 or _ANCHOR_TITLE.match(title):
        return False
    return any(ch.isalpha() for ch in title)


def outline_starts(pdf_path: str) -> list[tuple[int, str]]:
    """`(page_number, title)` for each chapter the outline claims, cleaned up.

    Consecutive entries pointing at the same page are one chapter whose title was
    split across outline entries — they are joined rather than becoming several
    zero-page units.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("the `pypdf` package is not installed") from exc

    reader = PdfReader(pdf_path)
    try:
        outline = reader.outline or []
    except Exception as exc:  # noqa: BLE001 - a broken outline is not an error
        log.info("no usable outline in %s (%s)", pdf_path, type(exc).__name__)
        return []

    found: list[tuple[int, str]] = []

    def walk(entries) -> None:
        for entry in entries:
            if isinstance(entry, list):
                walk(entry)
                continue
            title = _clean_title(str(getattr(entry, "title", "") or ""))
            try:
                page = reader.get_destination_page_number(entry) + 1  # 1-based
            except Exception:  # noqa: BLE001 - skip an unresolvable destination
                continue
            if _usable_title(title):
                found.append((page, title))

    walk(outline)

    merged: list[tuple[int, str]] = []
    for page, title in sorted(found, key=lambda pair: pair[0]):
        if merged and merged[-1][0] == page:
            merged[-1] = (page, f"{merged[-1][1]} {title}".strip())
        else:
            merged.append((page, title))
    return merged


def chapter_units(pages: list[Page], starts: list[tuple[int, str]]) -> list[Unit]:
    """Units bounded by outline entries. Empty when the outline is not usable."""
    if len(starts) < 2:
        return []  # one entry is not a table of contents

    units: list[Unit] = []
    for position, (start_page, title) in enumerate(starts):
        end_page = starts[position + 1][0] - 1 if position + 1 < len(starts) else 10**9
        body = [p for p in pages if start_page <= p.number <= end_page]
        if not body:
            continue
        text = "\n\n".join(p.text for p in body)
        if len(text) < MIN_UNIT_CHARS:
            continue  # a divider page, not a chapter
        units.append(Unit(len(units), title, body[0].number, body[-1].number, text))
    return units


def page_units(pages: list[Page], pages_per_unit: int = DEFAULT_PAGES_PER_UNIT) -> list[Unit]:
    """Fixed-size units. The fallback, and in practice the common case."""
    if pages_per_unit < 1:
        raise ValueError("pages_per_unit must be at least 1")

    units: list[Unit] = []
    for offset in range(0, len(pages), pages_per_unit):
        block = pages[offset:offset + pages_per_unit]
        text = "\n\n".join(p.text for p in block)
        if not text.strip():
            continue
        units.append(Unit(
            len(units),
            f"páginas {block[0].number}–{block[-1].number}",
            block[0].number, block[-1].number, text,
        ))
    return units


def plan_units(
    pdf_path: str,
    *,
    unit: str = "chapter",
    pages_per_unit: int = DEFAULT_PAGES_PER_UNIT,
) -> list[Unit]:
    """The units for this book, honouring `RESUME_UNIT` and degrading sensibly.

    `unit="chapter"` is a preference, not a promise: when the outline does not
    survive validation this returns page units instead of failing, because a book
    with a broken outline is still a book worth reading.
    """
    pages = extract_pages(pdf_path)
    if not pages:
        return []

    if unit == "chapter":
        units = chapter_units(pages, outline_starts(pdf_path))
        if len(units) >= 2:
            log.info("%s: %d chapter unit(s) from the outline", pdf_path, len(units))
            return units
        log.info("%s: outline unusable — falling back to %d-page units",
                 pdf_path, pages_per_unit)

    units = page_units(pages, pages_per_unit)
    log.info("%s: %d page unit(s)", pdf_path, len(units))
    return units
