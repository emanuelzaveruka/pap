"""Calendar event construction. Pure logic — no Google, no DB.

The behaviour these pin down is the one that makes the sync trustworthy: a
deadline must never appear twice, and a timezone slip must never move it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pap.sinks.gcalendar import (
    DEFAULT_DURATION,
    MAX_REMINDER_MINUTES,
    SyncOutcome,
    event_body,
)

DUE = datetime(2026, 3, 15, 23, 59, tzinfo=timezone(timedelta(hours=-3)))


def test_event_ends_exactly_at_the_deadline():
    """The prazo is the END of the block — a block starting at the deadline would
    put the reminder after the submission window closed."""
    body = event_body(title="MAPA", due_at=DUE)
    assert body["end"]["dateTime"] == DUE.isoformat()
    assert body["start"]["dateTime"] == (DUE - DEFAULT_DURATION).isoformat()


def test_a_naive_due_date_is_refused():
    """Calendar reads a naive datetime in the calendar's own zone. With the server
    in UTC and the college in BRT, every deadline would silently shift 3 hours."""
    with pytest.raises(ValueError, match="timezone-aware"):
        event_body(title="MAPA", due_at=datetime(2026, 3, 15, 23, 59))


def test_the_offset_is_preserved_not_normalised():
    body = event_body(title="MAPA", due_at=DUE)
    assert body["end"]["dateTime"].endswith("-03:00")


def test_title_and_url_reach_the_event():
    body = event_body(title="Atividade 1", due_at=DUE, description="resumo",
                      url="https://example.invalid/1")
    assert body["summary"] == "Atividade 1"
    assert "resumo" in body["description"]
    assert "https://example.invalid/1" in body["description"]


def test_events_are_tagged_as_ours_independently_of_the_title():
    """The user is free to rename an event in Calendar, so identification cannot
    depend on the summary."""
    assert event_body(title="x", due_at=DUE)["extendedProperties"]["private"]["pap"] == "deadline"


def test_default_reminders_are_ordered_and_explicit():
    body = event_body(title="x", due_at=DUE)
    assert body["reminders"]["useDefault"] is False
    minutes = [o["minutes"] for o in body["reminders"]["overrides"]]
    assert minutes == sorted(minutes, reverse=True)
    assert minutes == [24 * 60, 60]


def test_reminders_beyond_googles_limit_are_dropped():
    """Google rejects the whole event if any reminder exceeds 4 weeks — dropping
    the offending one keeps the deadline syncable."""
    body = event_body(title="x", due_at=DUE,
                      reminders_minutes=(MAX_REMINDER_MINUTES + 1, 60))
    assert [o["minutes"] for o in body["reminders"]["overrides"]] == [60]


def test_nonpositive_and_duplicate_reminders_are_discarded():
    body = event_body(title="x", due_at=DUE, reminders_minutes=(0, -5, 60, 60))
    assert [o["minutes"] for o in body["reminders"]["overrides"]] == [60]


def test_no_reminders_is_valid():
    body = event_body(title="x", due_at=DUE, reminders_minutes=())
    assert body["reminders"] == {"useDefault": False, "overrides": []}


def test_duration_is_configurable():
    body = event_body(title="x", due_at=DUE, duration=timedelta(hours=2))
    assert body["start"]["dateTime"] == (DUE - timedelta(hours=2)).isoformat()


# -- outcome reporting ------------------------------------------------------
def test_outcome_renders_every_counter():
    text = str(SyncOutcome(created=1, patched=2, unchanged=3, skipped=4, failed=5))
    for fragment in ("created=1", "patched=2", "unchanged=3", "skipped=4", "failed=5"):
        assert fragment in text


def test_empty_outcome_is_all_zero():
    assert str(SyncOutcome()) == ("created=0 patched=0 unchanged=0 skipped=0 failed=0")
