"""Content hashing — how the platform decides whether something actually changed.

The hash covers only the fields that carry meaning. Anything volatile (fetch
timestamps, session ids, tracking parameters on a URL) must stay out of it, or
every run would look like a change and re-notify you about work you already saw.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import Item


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no insignificant whitespace, non-ASCII preserved.

    Sorting matters — Python dict ordering follows insertion, so two adapters
    building the same payload in a different order would otherwise hash
    differently and look like a change.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def content_hash(item: Item) -> str:
    """The fingerprint stored on ``pap.item.content_hash``."""
    material = {
        "kind": item.kind,
        "title": item.title.strip(),
        "url": item.url or "",
        "due_at": item.due_at.isoformat() if item.due_at else "",
        "payload": item.payload,
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def sha256_file(path: str, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without reading it fully into memory — book PDFs are large and
    this runs on a 7 GiB box."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
