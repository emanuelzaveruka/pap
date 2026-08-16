"""Renders a finished deliverable into the DOCX the college actually handed out.

Phase 3's output is a document a human submits in Studeo, so it has to look like
the template Unicesumar supplies — not like something a script produced. That
means **filling their file**, never building a new one from scratch: the template
carries the header logo, A4 page, 1-inch margins and Arial 11 defaults, and a
rebuilt document silently loses all four.

Three things here are less obvious than they look:

**Identity cells are found by label, not by coordinates.** The header block is a
3-column table with merged rows, so ``table.rows[3].cells[0]`` and ``[1]`` are the
*same* cell. Addressing by position would be ambiguous today and would silently
write the wrong field the first time the college adjusts a merge. Matching on the
``Nome:`` / ``R.A`` / ``Prazo:`` prefix survives that.

**Each ``w:tc`` is visited once by iterating the XML.** ``row.cells`` repeats a
merged cell once per column it spans, which is what makes a naive fill write the
same value three times and skip a real field.

**The first run is rewritten and the rest blanked, rather than replacing the
paragraph.** A run carries the template's font; a freshly added run inherits the
document default instead, which is how a filled form ends up in a different
typeface from the blank one it came from.

This module deliberately does **not** call the LLM. It takes finished text and
places it. Generation lives behind ``llm/base.py``, so the renderer never learns
which vendor wrote the body — and can be tested without a key or a network.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from docx import Document
from docx.oxml.ns import qn

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "patterns", "templates",
)
MAPA_TEMPLATE = os.path.join(TEMPLATE_DIR, "mapa-unicesumar.docx")
ATIVIDADE_TEMPLATE = os.path.join(TEMPLATE_DIR, "atividade-unicesumar.docx")

# Activity type -> (template, pattern spec). Adding a type is one line plus the
# two files; nothing else in the pipeline learns about it.
TEMPLATES = {
    "mapa": (MAPA_TEMPLATE, "patterns/mapa.md"),
    "atividade": (ATIVIDADE_TEMPLATE, "patterns/atividade.md"),
}

# Identity fields are matched by MEANING, not by literal text. Six real documents
# from Studeo spell the same two fields six different ways:
#
#     name : "Nome:"  "Acadêmico:"
#     ra   : "R.A"    "R.A."    "R.A:"    "R.A.:"
#
# Matching on a literal prefix worked on the one blank template it was written
# against and silently filled nothing on four of the other five — the label stayed
# bare and the submission went out without a name or an RA. `Curso:`,
# `Disciplina:`, `Valor da atividade:` and `Prazo:` are stable across all six, but
# they are matched the same way so the next variant costs nothing.
#
# The label is written back EXACTLY as the template spelled it, so a filled
# document is indistinguishable from the blank it came from.
IDENTITY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("nome", re.compile(r"^(?:Nome|Acad[êe]mico)\s*:?", re.IGNORECASE)),
    ("ra", re.compile(r"^R\.?\s*A\.?\s*:?", re.IGNORECASE)),
    ("curso", re.compile(r"^Curso\s*:?", re.IGNORECASE)),
    ("disciplina", re.compile(r"^Disciplina\s*:?", re.IGNORECASE)),
    ("valor", re.compile(r"^Valor\s+da\s+atividade\s*:?", re.IGNORECASE)),
    ("prazo", re.compile(r"^Prazo\s*:?", re.IGNORECASE)),
)


@dataclass(frozen=True)
class Identity:
    """The header block. Everything is optional — a missing field leaves the
    label bare rather than inventing a value, because a wrong RA on a submitted
    MAPA is worse than an obviously empty one."""

    nome: str = ""
    ra: str = ""
    curso: str = ""
    disciplina: str = ""
    valor: str = ""
    prazo: str = ""

    def as_fields(self) -> dict[str, str]:
        return {
            "nome": self.nome,
            "ra": self.ra,
            "curso": self.curso,
            "disciplina": self.disciplina,
            "valor": self.valor,
            "prazo": self.prazo,
        }


def _cell_text(tc) -> str:
    return "".join(node.text or "" for node in tc.iter(qn("w:t"))).strip()


def _set_cell(tc, text: str) -> None:
    """Write `text` into the cell, keeping the template's run formatting."""
    runs = list(tc.iter(qn("w:t")))
    if not runs:
        return
    runs[0].text = text
    for extra in runs[1:]:
        extra.text = ""


def fill_identity(document: Document, identity: Identity) -> list[str]:
    """Fill the header table. Returns the field names that were found and written.

    Each field is written at most once. A cell whose label matches an
    already-written field is left alone, which is what keeps a merged row from
    receiving the same value in every column it spans.
    """
    wanted = identity.as_fields()
    written: list[str] = []
    for table in document.tables:
        for tc in table._tbl.iter(qn("w:tc")):
            text = _cell_text(tc)
            for field, pattern in IDENTITY_PATTERNS:
                if field in written:
                    continue
                match = pattern.match(text)
                if not match:
                    continue
                # Keep the template's own spelling — "R.A.:" stays "R.A.:".
                label = text[:match.end()].strip()
                value = (wanted.get(field) or "").strip()
                _set_cell(tc, f"{label} {value}".rstrip())
                written.append(field)
                break
    return written


# -- body ------------------------------------------------------------------
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BULLET = re.compile(r"^[-*]\s+")
_NUMBERED = re.compile(r"^(\d+)([.)])\s+")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|[\s|:\-]+\|\s*$")


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _set_borders(table) -> None:
    """Draw the grid explicitly.

    This template defines no `Table Grid` style — the same gap that silently
    flattened the bullet lists. Without borders a risk table renders as unaligned
    columns of text, so they are written into the XML rather than left to a style
    that may not exist.
    """
    from docx.oxml import OxmlElement

    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:color"), "auto")
        borders.append(element)
    table._tbl.tblPr.append(borders)


def add_table(document: Document, lines: list[str]):
    """Render a markdown pipe table as a real Word table.

    Regression: without this the model's table arrived as seven paragraphs of raw
    pipes — separator row `|---|---|` and all — in a graded document. Tables are
    not incidental here: `patterns/mapa.md` §6 records that content tables carry
    real weight, one sample having five of them.
    """
    rows = [_split_row(line) for line in lines if not _TABLE_RULE.match(line)]
    rows = [r for r in rows if any(cell for cell in r)]
    if not rows:
        return None
    width = max(len(r) for r in rows)

    table = document.add_table(rows=len(rows), cols=width)
    _set_borders(table)
    for row_index, cells in enumerate(rows):
        for col_index in range(width):
            text = cells[col_index] if col_index < len(cells) else ""
            paragraph = table.cell(row_index, col_index).paragraphs[0]
            _add_runs(paragraph, text)
            if row_index == 0:  # header
                for run in paragraph.runs:
                    run.bold = True
    return table
_HEADING = re.compile(r"^(#{1,6})\s+")


def _add_runs(paragraph, text: str) -> None:
    """Render `**bold**` inline. Everything else is written literally — a MAPA is
    prose, and a half-supported markdown dialect would put stray asterisks into a
    graded document."""
    for index, part in enumerate(_BOLD.split(text)):
        if not part:
            continue
        run = paragraph.add_run(part)
        run.bold = bool(index % 2)


def add_body(document: Document, markdown: str) -> int:
    """Append the deliverable body. Returns the number of paragraphs written.

    Supports headings, paragraphs, bullet and numbered lists — the shape a MAPA
    answer actually takes. Styles come from the template where it defines them,
    falling back to a bold run so a template without list styles still renders
    something readable rather than raising.

    **A list without its markers is not a list.** This template defines no
    ``List Bullet`` / ``List Number`` style, so relying on styles alone turned
    every bullet into an ordinary paragraph — the enumeration vanished and the
    answer read as run-on prose. When the style is missing the marker is written
    as literal text instead, which is visibly a list in any reader.
    """
    written = 0
    for block in (b.strip() for b in re.split(r"\n\s*\n", markdown.strip())):
        if not block:
            continue
        lines = [line.strip() for line in block.splitlines() if line.strip()]

        # A pipe table is a block-level construct — handled before the per-line
        # walk, which would otherwise emit each row as a paragraph of raw pipes.
        if len(lines) >= 2 and all(_TABLE_ROW.match(line) for line in lines):
            if add_table(document, lines) is not None:
                written += 1
                continue

        for line in lines:
            heading = _HEADING.match(line)
            if heading:
                level = len(heading.group(1))
                text = line[heading.end():].strip()
                try:
                    paragraph = document.add_paragraph(style=f"Heading {level}")
                    _add_runs(paragraph, text)
                except KeyError:
                    paragraph = document.add_paragraph()
                    paragraph.add_run(text).bold = True
            elif _BULLET.match(line):
                paragraph, styled = _styled(document, "List Bullet")
                _add_runs(paragraph, ("" if styled else "• ") + _BULLET.sub("", line))
            elif (numbered := _NUMBERED.match(line)):
                # Echo the author's own number. Generating one silently renumbered
                # a real deliverable: the model wrote sections 1/2/3, each in its
                # own block, and a per-block counter reset every one of them to
                # "1." — three questions all numbered 1 in a graded document.
                # There is no case where inventing a number beats keeping the one
                # that was written.
                paragraph, styled = _styled(document, "List Number")
                marker = "" if styled else f"{numbered.group(1)}{numbered.group(2)} "
                _add_runs(paragraph, marker + line[numbered.end():])
            else:
                paragraph = document.add_paragraph()
                _add_runs(paragraph, line)
            written += 1
    return written


def _styled(document: Document, style: str) -> tuple:
    """(paragraph, whether the requested style existed)."""
    try:
        return document.add_paragraph(style=style), True
    except KeyError:  # template without that list style
        return document.add_paragraph(), False


def render_deliverable(
    body_markdown: str,
    identity: Identity,
    target_path: str,
    *,
    template_path: str = MAPA_TEMPLATE,
) -> str:
    """Fill the template and save to `target_path`. Returns the path written."""
    if not os.path.exists(template_path):
        raise FileNotFoundError(
            f"deliverable template not found: {template_path}. It ships in "
            f"patterns/templates/ — see patterns/mapa.md."
        )
    document = Document(template_path)
    fill_identity(document, identity)
    add_body(document, body_markdown)
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    document.save(target_path)
    return target_path
