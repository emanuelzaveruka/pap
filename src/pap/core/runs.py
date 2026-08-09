"""The run ledger — one row per job execution, and the place ops wiring lives.

Every job runs inside ``source_run(...)``. That single context manager is what
guarantees the three observability facts stay true without each job remembering
to maintain them:

1. ``pap.source_run`` always gets a terminal row, success or failure. The monitor
   reads only this table, so it can never disagree with what actually happened.
2. A failure is reported to GlitchTip with ``source`` tagged, and the returned
   event id is written onto the run row — turning ``status='failed'`` from a dead
   end into a direct link to the stack trace.
3. Healthchecks is pinged at start and at the end. This is the only signal that
   catches a job which never ran at all: no exception is raised by a timer that
   stopped firing, so nothing else can notice.

The exception is always re-raised. Swallowing it here would let a broken run exit
0 and look healthy to systemd, which is exactly the silent failure the whole
design is trying to make impossible.
"""

from __future__ import annotations

import logging
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .. import observability
from ..config import Settings
from ..db import Database

log = logging.getLogger(__name__)


@dataclass
class RunContext:
    """Mutable counters for the job in progress, persisted when it finishes."""

    run_id: int
    source: str
    dry_run: bool = False
    items_found: int = 0
    items_new: int = 0
    items_changed: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"source={self.source}",
            f"found={self.items_found}",
            f"new={self.items_new}",
            f"changed={self.items_changed}",
        ]
        if self.dry_run:
            parts.append("dry-run")
        if self.notes:
            parts.append("; ".join(self.notes))
        return " ".join(parts)


@contextmanager
def source_run(
    db: Database,
    settings: Settings,
    source: str,
    *,
    dry_run: bool = False,
    job: str | None = None,
) -> Iterator[RunContext]:
    """Wrap one job execution in the ledger, error reporting and liveness pings."""
    healthchecks = observability.healthchecks_for(settings, job or source)
    healthchecks.start()

    observability.set_tag("source", source)
    observability.set_tag("dry_run", "true" if dry_run else "false")

    run_id = db.start_run(source, dry_run=dry_run)
    ctx = RunContext(run_id=run_id, source=source, dry_run=dry_run)
    observability.set_context("run", {"run_id": run_id, "source": source, "dry_run": dry_run})
    log.info("run %d started (source=%s%s)", run_id, source, ", dry-run" if dry_run else "")

    try:
        yield ctx
    except BaseException as exc:  # noqa: BLE001 - recorded, reported, then re-raised
        event_id = observability.capture_exception(exc)
        error_text = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        db.finish_run(
            run_id,
            status="failed",
            items_found=ctx.items_found,
            items_new=ctx.items_new,
            error_text=error_text[:8000],
            sentry_event_id=event_id,
        )
        healthchecks.fail(f"{ctx.summary()} :: {type(exc).__name__}: {exc}")
        log.error("run %d failed: %s", run_id, exc)
        if event_id:
            log.error("reported as GlitchTip event %s", event_id)
        observability.flush()
        raise
    else:
        db.finish_run(
            run_id,
            status="success",
            items_found=ctx.items_found,
            items_new=ctx.items_new,
        )
        healthchecks.success(ctx.summary())
        log.info("run %d finished — %s", run_id, ctx.summary())
        observability.flush()
