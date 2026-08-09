"""Executes one source: collect, persist, enqueue, deliver.

This is the only place that knows how the pieces fit together, which is what
keeps adapters free of platform concerns. The stages are wrapped in
``observability.phase(...)`` so a reported exception says *where* it happened —
a collect failure means the site changed, a persist failure means the database,
an enqueue failure means our own logic. That distinction is usually the whole
diagnosis.
"""

from __future__ import annotations

import logging
from typing import Any

from . import observability
from .config import Settings
from .core import runs
from .core.dedupe import content_hash
from .core.models import Item, parse_module_code
from .core.registry import get as get_adapter
from .db import Database
from .sinks import dispatcher

log = logging.getLogger(__name__)


class _StructureCache:
    """Resolves module/discipline ids once per run instead of per item.

    A Studeo run touches a handful of modules across a few hundred activities, so
    without this the same two upserts would run for every single item.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._modules: dict[str, int] = {}
        self._disciplines: dict[tuple[int, str], int] = {}

    def discipline_id(self, item: Item) -> int | None:
        if not item.module_code or not item.discipline_external_id:
            return None

        module_id = self._modules.get(item.module_code)
        if module_id is None:
            parsed = parse_module_code(item.module_code)
            module_id = self.db.upsert_module(
                parsed.code, year=parsed.year, seq=parsed.seq, label=None
            )
            self._modules[item.module_code] = module_id

        key = (module_id, item.discipline_external_id)
        discipline_id = self._disciplines.get(key)
        if discipline_id is None:
            discipline_id = self.db.upsert_discipline(
                module_id,
                item.discipline_external_id,
                item.discipline_name or item.discipline_external_id,
            )
            self._disciplines[key] = discipline_id
        return discipline_id


def run_source(
    db: Database,
    settings: Settings,
    name: str,
    *,
    dry_run: bool = False,
    dispatch: bool = True,
    adapter_kwargs: dict[str, Any] | None = None,
) -> int:
    """Run one source end to end. Returns a process exit code."""
    adapter_cls = get_adapter(name)
    adapter = adapter_cls(settings, **(adapter_kwargs or {}))

    if not adapter.enabled:
        log.error("source %r is not configured — check its *_ENABLED and credentials "
                  "(run `pap doctor`)", name)
        return 2

    try:
        with runs.source_run(db, settings, name, dry_run=dry_run) as ctx:
            with observability.phase("collect"):
                items = list(adapter.collect())
            ctx.items_found = len(items)
            log.info("collected %d item(s) from %s", len(items), name)

            if dry_run:
                for item in items:
                    log.info("would store %s/%s :: %s", item.source, item.external_id, item.title)
                ctx.notes.append("dry-run: nothing written, nothing sent")
                return 0

            structure = _StructureCache(db)
            with observability.phase("persist"):
                for item in items:
                    item_id, is_new, changed = db.upsert_item(
                        source=item.source,
                        external_id=item.external_id,
                        content_hash=content_hash(item),
                        kind=item.kind,
                        title=item.title,
                        url=item.url,
                        payload=item.payload,
                        discipline_id=structure.discipline_id(item),
                    )
                    ctx.items_new += int(is_new)
                    ctx.items_changed += int(changed)
                    if item.due_at is not None:
                        db.upsert_deadline(item_id, item.due_at)

            with observability.phase("enqueue"):
                queued = dispatcher.enqueue_new_items(db, settings, source=name)
            if queued:
                ctx.notes.append(f"queued={queued}")

            if dispatch:
                with observability.phase("dispatch"):
                    sent, failed = dispatcher.drain(db, settings)
                if sent or failed:
                    ctx.notes.append(f"sent={sent} failed={failed}")
    finally:
        adapter.close()

    return 0
