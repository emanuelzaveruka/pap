"""Google Calendar sink — Studeo deadlines become Calendar events.

The whole value of this depends on one property: **a deadline that moves must
patch its existing event, never create a second one.** A duplicated prazo is
worse than no sync at all, because you can no longer tell which date is real.
That is enforced by `pap.deadline.gcal_event_id`: present means "already in
Calendar", so the decision is insert-or-patch rather than insert-and-hope.

Two further rules follow from the same concern:

*Patch only what changed.* ``synced_due_at`` records the value last pushed. If it
still matches, the event is left alone — so a re-run costs one database read and
zero API calls, and manual edits you made in Calendar survive.

*An event deleted in Calendar is not resurrected.* If the stored id 404s, the
event is recreated (you presumably deleted it by accident); but if it was
explicitly cancelled, Google reports it as ``status='cancelled'`` and we leave it
alone rather than fighting the user.

Deadlines are written as short timed events ending at the prazo, not all-day
events — an all-day event gives no sense of *when* on the final day, which is
exactly what a 23:59 submission cutoff needs to convey.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import GoogleSettings
from .google_auth import GoogleAuthError, load_credentials

log = logging.getLogger(__name__)

# How long the event block appears in the calendar, ending at the deadline.
DEFAULT_DURATION = timedelta(minutes=30)

# Google rejects reminders beyond 40320 minutes (4 weeks) before an event.
MAX_REMINDER_MINUTES = 40320


@dataclass(frozen=True)
class SyncOutcome:
    created: int = 0
    patched: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (f"created={self.created} patched={self.patched} "
                f"unchanged={self.unchanged} skipped={self.skipped} failed={self.failed}")


def event_body(
    *,
    title: str,
    due_at: datetime,
    description: str = "",
    url: str | None = None,
    duration: timedelta = DEFAULT_DURATION,
    reminders_minutes: tuple[int, ...] = (24 * 60, 60),
) -> dict:
    """Build the Calendar event payload for a deadline.

    Pure function, so the shape is testable without touching Google. ``due_at``
    must be timezone-aware: Calendar interprets a naive datetime in the
    calendar's own zone, which silently shifts every deadline when the server
    runs in UTC and the college does not.
    """
    if due_at.tzinfo is None:
        raise ValueError("due_at must be timezone-aware — a naive value would be "
                         "interpreted in the calendar's zone and shift the deadline")

    body_lines = [description] if description else []
    if url:
        body_lines.append(url)
    body_lines.append("Criado por pap — não editar o título.")

    overrides = [
        {"method": "popup", "minutes": m}
        for m in sorted({m for m in reminders_minutes if 0 < m <= MAX_REMINDER_MINUTES}, reverse=True)
    ]

    return {
        "summary": title,
        "description": "\n\n".join(body_lines),
        "start": {"dateTime": (due_at - duration).isoformat()},
        "end": {"dateTime": due_at.isoformat()},
        "reminders": {"useDefault": False, "overrides": overrides},
        # Marks the event as ours without relying on the title, which the user is
        # free to rename in Calendar.
        "extendedProperties": {"private": {"pap": "deadline"}},
    }


class CalendarSink:
    name = "gcalendar"

    def __init__(self, settings: GoogleSettings, *, state_dir: str | None = None) -> None:
        self.settings = settings
        self.state_dir = state_dir
        self._service = None

    @property
    def configured(self) -> bool:
        return self.settings.configured

    @property
    def service(self):
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover
                raise GoogleAuthError("google-api-python-client is not installed") from exc
            credentials = load_credentials(self.settings, state_dir=self.state_dir)
            self._service = build("calendar", "v3", credentials=credentials,
                                  cache_discovery=False)
        return self._service

    # -- single event -------------------------------------------------------
    def create(self, body: dict) -> str:
        created = self.service.events().insert(
            calendarId=self.settings.calendar_id, body=body
        ).execute()
        return created["id"]

    def patch(self, event_id: str, body: dict) -> None:
        self.service.events().patch(
            calendarId=self.settings.calendar_id, eventId=event_id, body=body
        ).execute()

    def get_status(self, event_id: str) -> str | None:
        """``confirmed`` / ``cancelled``, or None when the event no longer exists."""
        try:
            from googleapiclient.errors import HttpError
        except ImportError as exc:  # pragma: no cover
            raise GoogleAuthError("google-api-python-client is not installed") from exc
        try:
            event = self.service.events().get(
                calendarId=self.settings.calendar_id, eventId=event_id
            ).execute()
        except HttpError as exc:
            if exc.resp.status in (404, 410):
                return None
            raise
        return event.get("status")


def sync_deadlines(db, settings, *, dry_run: bool = False, limit: int = 500) -> SyncOutcome:
    """Push every pending deadline to Calendar, patching what moved.

    Reads ``pap.deadline`` joined to ``pap.item`` for the title and URL. Rows whose
    ``synced_due_at`` still equals ``due_at`` are left untouched — that is what
    makes a re-run free.
    """
    if not settings.google.configured:
        log.warning("Google is not configured — no deadlines can be synced")
        return SyncOutcome()

    rows = db.deadlines_to_sync(limit=limit)
    if not rows:
        return SyncOutcome()

    sink = CalendarSink(settings.google, state_dir=settings.state_dir)
    created = patched = unchanged = skipped = failed = 0

    for row in rows:
        body = event_body(
            title=f"[{row['source']}] {row['title']}",
            due_at=row["due_at"],
            description=(row.get("payload") or {}).get("summary", ""),
            url=row.get("url"),
        )

        if dry_run:
            action = "create" if not row["gcal_event_id"] else "patch"
            log.info("would %s event for deadline %s (%s)", action, row["id"], row["due_at"])
            created += int(action == "create")
            patched += int(action == "patch")
            continue

        try:
            event_id = row["gcal_event_id"]
            if event_id:
                status = sink.get_status(event_id)
                if status == "cancelled":
                    # Explicitly deleted in Calendar. Recreating it would override a
                    # deliberate choice, so record the sync and move on.
                    db.mark_deadline_synced(row["id"], event_id, row["due_at"])
                    skipped += 1
                    continue
                if status is None:
                    event_id = sink.create(body)
                    created += 1
                else:
                    sink.patch(event_id, body)
                    patched += 1
            else:
                event_id = sink.create(body)
                created += 1

            db.mark_deadline_synced(row["id"], event_id, row["due_at"])
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            failed += 1
            log.exception("could not sync deadline %s: %s", row["id"], exc)

    return SyncOutcome(created=created, patched=patched, unchanged=unchanged,
                       skipped=skipped, failed=failed)
