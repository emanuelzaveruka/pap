"""Central Notifications — the only path from "something happened" to "you were told".

Scrapers never send anything. They write ``pap.item`` rows and stop. This module
turns items the platform has *not yet reported* into queued notifications, then
delivers them. Two consequences worth stating, because they are the whole reason
for the split:

* Re-running a scraper is silent. The second run finds the same items, sees
  ``notified_at`` already set, and enqueues nothing.
* A channel outage loses nothing. Delivery failures leave the row ``pending``
  with the reason recorded, so the next dispatch retries it. Only after
  ``MAX_ATTEMPTS`` does a notification give up and go to ``failed``.

Deduplication is enforced by the ``UNIQUE`` constraint on ``dedupe_key``, not by
checking first — two jobs racing on the same event collide in the database
instead of both sending.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence
from urllib.parse import quote

from ..config import Settings
from ..core.models import Notification, SendResult
from ..db import Database
from .base import Sink
from .email_smtp import EmailSink
from .telegram import TelegramSink

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


def build_sinks(settings: Settings) -> dict[str, Sink]:
    """Every sink that is actually usable, keyed by channel name."""
    candidates: list[Sink] = [
        TelegramSink(settings.telegram),
        EmailSink(settings.email),
    ]
    return {sink.name: sink for sink in candidates if sink.configured}


def default_channels(settings: Settings) -> list[str]:
    return sorted(build_sinks(settings))


def item_dedupe_key(source: str, external_id: str, channel: str) -> str:
    """Stable identity for "we told you about this item on this channel".

    Includes the channel so enabling email later still delivers items Telegram
    already covered, rather than treating them as already handled.

    Each part is percent-escaped before joining. Without that, an ``external_id``
    containing a colon — which portal ids frequently do — could produce the same
    key as a different item, and the UNIQUE constraint would silently swallow the
    second notification. Escaping keeps the key both unambiguous and readable in
    the table.
    """
    return ":".join(("item", quote(source, safe=""), quote(external_id, safe=""),
                     quote(channel, safe="")))


def _summarise(item: dict[str, Any], *, limit: int = 600) -> str:
    """A short body from whatever the adapter put in the payload.

    Adapters are not required to provide a summary, so this degrades gracefully
    rather than assuming any particular payload shape.
    """
    payload = item.get("payload") or {}
    for key in ("summary", "description", "excerpt", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= limit else text[: limit - 1] + "…"
    if due := payload.get("due_at"):
        return f"Prazo: {due}"
    return ""


def enqueue_new_items(
    db: Database,
    settings: Settings,
    *,
    source: str | None = None,
    channels: Sequence[str] | None = None,
    limit: int = 200,
    dry_run: bool = False,
) -> int:
    """Queue a notification per un-reported item, per channel. Returns rows queued."""
    channels = list(channels) if channels is not None else default_channels(settings)
    if not channels:
        log.warning("no notification channel is configured — items will stay unreported")
        return 0

    items = db.items_awaiting_notification(source=source, limit=limit)
    if not items:
        return 0

    queued = 0
    for item in items:
        for channel in channels:
            key = item_dedupe_key(item["source"], item["external_id"], channel)
            title = f"[{item['source']}] {item['title']}"
            if dry_run:
                log.info("would queue %s -> %s", key, title)
                queued += 1
                continue
            if db.enqueue_notification(
                dedupe_key=key,
                channel=channel,
                title=title,
                body=_summarise(item),
                url=item.get("url"),
                payload={"item_id": item["id"], "kind": item["kind"]},
            ):
                queued += 1

    if not dry_run:
        # Marked here, not after delivery: the queue row is now the durable record
        # and it has its own retry. Waiting for delivery would re-enqueue the same
        # item on the next run while the first copy is still pending.
        db.mark_items_notified(item["id"] for item in items)
    return queued


def drain(
    db: Database,
    settings: Settings,
    *,
    limit: int = 50,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Deliver pending notifications. Returns ``(sent, failed)``."""
    sinks = build_sinks(settings)
    pending = db.pending_notifications(limit=limit, max_attempts=MAX_ATTEMPTS)
    if not pending:
        return 0, 0

    sent = failed = 0
    for row in pending:
        channel = row["channel"]
        sink = sinks.get(channel)
        if sink is None:
            # Do not burn an attempt on a channel that is merely switched off —
            # it would exhaust the retries and mark the row failed for a reason
            # that has nothing to do with the message.
            log.warning("notification %d targets channel %r which is not configured — skipping",
                        row["id"], channel)
            continue

        notification = Notification(
            dedupe_key=row["dedupe_key"],
            channel=channel,
            title=row["title"],
            body=row["body"] or "",
            url=row["url"],
            payload=row["payload"] or {},
        )

        if dry_run:
            log.info("would send via %s: %s", channel, notification.title)
            sent += 1
            continue

        try:
            result = sink.send(notification)
        except Exception as exc:  # noqa: BLE001 - a broken sink must not stop the queue
            result = SendResult(ok=False, detail=f"{type(exc).__name__}: {exc}")
            log.exception("sink %s raised while sending notification %d", channel, row["id"])

        if result.ok:
            db.mark_notification_sent(row["id"])
            sent += 1
            log.info("sent notification %d via %s (%s)", row["id"], channel, result.detail)
        else:
            db.mark_notification_failed(row["id"], result.detail, max_attempts=MAX_ATTEMPTS)
            failed += 1
            log.warning("notification %d via %s failed: %s", row["id"], channel, result.detail)

    return sent, failed


def send_now(settings: Settings, notification: Notification) -> SendResult:
    """Deliver one notification immediately, bypassing the queue.

    Only for interactive commands such as ``pap notify --test``, where the point
    is to find out right now whether a channel works. Scheduled work always goes
    through the queue so it inherits dedupe and retries.
    """
    sink = build_sinks(settings).get(notification.channel)
    if sink is None:
        return SendResult(ok=False, detail=f"channel {notification.channel!r} is not configured")
    return sink.send(notification)
