"""Recording and announcing a deliverable. No network, no database, no Drive."""

from __future__ import annotations

import pytest

from pap.archives.publish import deliverable_dedupe_key, publish_deliverable


# -- dedupe key -------------------------------------------------------------
def test_the_same_document_twice_is_one_notification():
    a = deliverable_dedupe_key("2026_26_X", "mapa", 3, "telegram")
    b = deliverable_dedupe_key("2026_26_X", "mapa", 3, "telegram")
    assert a == b


def test_a_pattern_version_bump_is_a_different_document():
    """Regenerating because the spec changed is worth saying; regenerating with
    the same spec is not."""
    v3 = deliverable_dedupe_key("2026_26_X", "mapa", 3, "telegram")
    v4 = deliverable_dedupe_key("2026_26_X", "mapa", 4, "telegram")
    assert v3 != v4


def test_each_channel_is_notified_independently():
    """Enabling email later must deliver what Telegram already covered."""
    assert (deliverable_dedupe_key("X", "mapa", 3, "telegram")
            != deliverable_dedupe_key("X", "mapa", 3, "email"))


def test_a_colon_in_the_id_cannot_forge_another_key():
    """Portal ids contain colons constantly; unescaped they collide and the UNIQUE
    constraint silently swallows the second notification."""
    assert (deliverable_dedupe_key("a:b", "mapa", 3, "telegram")
            != deliverable_dedupe_key("a", "b:mapa", 3, "telegram"))


# -- publish ----------------------------------------------------------------
class _DB:
    def __init__(self):
        self.rows, self.queued = [], []

    def upsert_deliverable(self, **kw):
        self.rows.append(kw)
        return len(self.rows)

    def enqueue_notification(self, **kw):
        self.queued.append(kw)
        return True


class _Google:
    drive_root_folder = "Studeo"
    configured = True


class _Settings:
    google = _Google()
    state_dir = "/tmp"


class _Drive:
    def __init__(self, fail=False):
        self.fail = fail

    def upload(self, path, *, folder_path, name=None, mime_type=None):
        if self.fail:
            raise RuntimeError("drive exploded")
        return "drive-123"


ITEM = {"id": 7, "external_id": "2026_26_X", "title": "MAPA de Teste", "url": "https://studeo/x"}


def _publish(db, drive, **over):
    kw = dict(item=ITEM, local_path="/tmp/m.docx", pattern_name="mapa",
              pattern_version=3, discipline="TÓPICOS", module_code="53/2026",
              provider="gemini", model="gemini-3.7-flash", drive=drive,
              channels=["telegram"])
    kw.update(over)
    return publish_deliverable(db, _Settings(), **kw)


def test_a_successful_publish_records_the_drive_id_and_notifies():
    db = _DB()
    res = _publish(db, _Drive())
    assert res.status == "uploaded"
    assert res.drive_file_id == "drive-123"
    assert res.link.endswith("/drive-123/view")
    assert db.rows[0]["status"] == "uploaded"
    assert db.rows[0]["pattern_version"] == 3
    assert res.notified == ["telegram"]


def test_a_failed_upload_still_records_the_row():
    """A failure that leaves no trace is indistinguishable from never having
    generated the document, and the next run would treat it as a first attempt."""
    db = _DB()
    res = _publish(db, _Drive(fail=True))
    assert res.status == "failed"
    assert res.drive_file_id is None
    assert db.rows[0]["status"] == "failed"
    assert db.rows[0]["local_path"] == "/tmp/m.docx"


def test_a_failed_upload_announces_nothing():
    """A message whose link goes nowhere is worse than no message."""
    db = _DB()
    _publish(db, _Drive(fail=True))
    assert db.queued == []


def test_a_dry_run_touches_nothing():
    db = _DB()
    res = _publish(db, _Drive(), dry_run=True)
    assert (db.rows, db.queued, res.status) == ([], [], "draft")


def test_the_message_says_the_platform_does_not_submit():
    """The single most important line in it: a message that reads like the work is
    done would be actively misleading."""
    db = _DB()
    _publish(db, _Drive())
    body = db.queued[0]["body"]
    assert "NÃO envia" in body
    assert "Revise" in body


def test_the_message_carries_the_spec_version_that_produced_it():
    db = _DB()
    _publish(db, _Drive())
    assert "mapa v3" in db.queued[0]["body"]
    assert db.queued[0]["payload"]["pattern_version"] == "3"
    assert db.queued[0]["url"].endswith("/drive-123/view")
