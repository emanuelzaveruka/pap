"""The shapes every adapter and sink agrees on.

``Item`` is the canonical thing a source produces — a Studeo activity, an Akita
post, a job posting. Keeping one shape is what lets the dispatcher, the dedupe
logic and the monitor stay source-agnostic: adding a source means writing an
adapter, not touching anything downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Item:
    """One collected thing, in the shape the platform stores.

    ``external_id`` must be stable for the same logical thing across runs — it is
    half of the ``UNIQUE (source, external_id)`` constraint that makes re-running
    a scraper safe. Prefer the source's own id over a URL, and a URL over a title.

    ``payload`` carries whatever else the adapter wants to keep. It is stored as
    jsonb, so later phases can query it without a migration.
    """

    source: str
    external_id: str
    title: str
    kind: str = "generic"
    url: str | None = None
    payload: dict = field(default_factory=dict)
    due_at: datetime | None = None
    module_code: str | None = None
    discipline_external_id: str | None = None
    discipline_name: str | None = None


@dataclass(frozen=True)
class Notification:
    """One thing to tell the user, on one channel."""

    dedupe_key: str
    channel: str
    title: str
    body: str = ""
    url: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class ModuleCode:
    """A Studeo module code such as ``54/2025``.

    Stored verbatim as ``code``; ``year`` and ``seq`` are parsed out only for
    sorting and for building Drive folder names. If the numbering turns out to
    mean something other than it looks like, this parser is the only thing that
    changes — nothing downstream depends on the interpretation.
    """

    code: str
    year: int | None = None
    seq: int | None = None

    @property
    def folder_name(self) -> str:
        """Filesystem/Drive-safe name: ``54/2025`` becomes ``54-2025``.

        A slash would be read as a path separator, so it is always replaced —
        including for codes this parser did not understand.
        """
        if self.seq is not None and self.year is not None:
            return f"{self.seq:02d}-{self.year}"
        return re.sub(r"[^\w.-]+", "-", self.code).strip("-") or "unknown"


_MODULE_CODE_RE = re.compile(r"^\s*(\d{1,3})\s*/\s*(\d{4})\s*$")


def parse_module_code(code: str) -> ModuleCode:
    """Parse ``"54/2025"`` into its parts, keeping the original string intact.

    An unrecognised code is never an error: it is kept verbatim with no year or
    seq, so an unexpected format degrades to "unsorted but still filed" instead
    of failing a scrape.
    """
    match = _MODULE_CODE_RE.match(code or "")
    if not match:
        return ModuleCode(code=(code or "").strip())
    return ModuleCode(code=code.strip(), seq=int(match.group(1)), year=int(match.group(2)))
