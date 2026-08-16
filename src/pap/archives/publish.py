"""Puts a finished deliverable where you can find it, and tells you it is there.

Three steps, in an order chosen so a failure never lies about what happened:

1. **Upload to Drive.** Until the bytes are there, there is nothing to link to.
2. **Record the row**, carrying the Drive id and the `pattern_version` that
   produced the document.
3. **Queue the notification**, which is what makes the whole thing useful — a
   file quietly appearing in Drive is not something you would notice.

The row is written *after* the upload rather than before, so `status='uploaded'`
with a `drive_file_id` is always true rather than aspirational. If the upload
fails the row still gets written, with `status='failed'` and the local path, so
a regenerated document is not mistaken for a first attempt.

Notification goes through the dispatcher's queue rather than sending directly.
Scrapers and generators never send — they enqueue, and `pap dispatch` delivers
with retries. That is what makes a failed Telegram call cost nothing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import quote

from ..config import Settings
from ..db import Database
from ..sinks.gdrive import DriveSink, file_link
from .deliverable import deliverable_folder, upload_deliverable

log = logging.getLogger(__name__)


def deliverable_dedupe_key(item_external_id: str, pattern_name: str,
                           pattern_version: int | str, channel: str) -> str:
    """Stable identity for "we told you this deliverable was ready".

    The pattern version is part of the key on purpose: regenerating with the same
    spec should stay silent, but regenerating because the spec changed is a
    genuinely different document and worth saying so. Each part is percent-escaped
    for the same reason `item_dedupe_key` does it — an id containing a colon would
    otherwise collide with a different one and the UNIQUE constraint would swallow
    the second notification.
    """
    return ":".join((
        "deliverable",
        quote(str(item_external_id), safe=""),
        quote(pattern_name, safe=""),
        quote(str(pattern_version), safe=""),
        quote(channel, safe=""),
    ))


@dataclass
class Published:
    deliverable_id: int
    drive_file_id: str | None
    link: str | None
    notified: list[str]
    status: str


def publish_deliverable(
    db: Database,
    settings: Settings,
    *,
    item: dict,
    local_path: str,
    pattern_name: str,
    pattern_version: int | str,
    discipline: str,
    module_code: str | None,
    provider: str | None = None,
    model: str | None = None,
    fmt: str = "docx",
    drive: DriveSink | None = None,
    channels: list[str] | None = None,
    dry_run: bool = False,
) -> Published:
    """Upload, record and announce one deliverable."""
    folder = deliverable_folder(settings, module_code, discipline)
    name = os.path.basename(local_path)

    if dry_run:
        log.info("would upload %s -> %s and notify", name, folder)
        return Published(0, None, None, [], "draft")

    drive_file_id, status = None, "rendered"
    if drive is None and settings.google.configured:
        drive = DriveSink(settings.google, state_dir=settings.state_dir)
    if drive is not None:
        try:
            drive_file_id = upload_deliverable(drive, local_path, folder, name=name)
            status = "uploaded"
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            # The row is still written. A failed upload that leaves no trace looks
            # identical to never having generated the document at all.
            status = "failed"
            log.error("could not upload %s: %s", name, " ".join(str(exc).split())[:200])
    else:
        log.warning("Google is not configured — the deliverable stays local only")

    deliverable_id = db.upsert_deliverable(
        item_id=item["id"],
        pattern_name=pattern_name,
        pattern_version=pattern_version,
        fmt=fmt,
        local_path=local_path,
        drive_file_id=drive_file_id,
        provider=provider,
        model=model,
        status=status,
    )

    link = file_link(drive_file_id) if drive_file_id else None
    notified: list[str] = []
    if status == "uploaded":
        from ..sinks.dispatcher import default_channels

        for channel in (channels or default_channels(settings)):
            key = deliverable_dedupe_key(item["external_id"], pattern_name,
                                         pattern_version, channel)
            queued = db.enqueue_notification(
                dedupe_key=key,
                channel=channel,
                title=f"Entrega gerada: {item.get('title') or pattern_name}",
                body=_body(item, discipline, pattern_name, pattern_version, provider, model),
                url=link,
                payload={"deliverable_id": deliverable_id,
                         "drive_file_id": drive_file_id,
                         "pattern": pattern_name,
                         "pattern_version": str(pattern_version)},
            )
            if queued:
                notified.append(channel)

    return Published(deliverable_id, drive_file_id, link, notified, status)


def _body(item: dict, discipline: str, pattern_name: str,
          pattern_version: int | str, provider: str | None, model: str | None) -> str:
    """The message you actually read on your phone.

    It says *revise and submit* because the platform never submits: a message that
    reads like the work is finished would be actively misleading.
    """
    lines = [
        f"<b>{discipline}</b>",
        f"Atividade: {item.get('title') or '—'}",
        f"Padrão: {pattern_name} v{pattern_version}",
    ]
    if provider:
        lines.append(f"Gerado por: {provider}/{model or '?'}")
    if item.get("url"):
        lines.append(f"Atividade no Studeo: {item['url']}")
    lines.append("")
    lines.append("Revise o documento antes de entregar. "
                 "A plataforma NÃO envia nada ao Studeo.")
    return "\n".join(lines)
