"""Backup filename parsing and the retention policy.

Deleting the wrong backup is not the kind of mistake you get to notice later, so
the selection rule is tested as a pure function with no filesystem or Drive access.
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pap.ops import backup as backup_module
from pap.ops.backup import Archive, BackupError, parse_archive_name, select_expired

NOW = datetime(2026, 8, 8, 3, 20, tzinfo=timezone.utc)


def _daily(days: int) -> Archive:
    taken = NOW - timedelta(days=days)
    return Archive(name=f"pap-{taken.strftime('%Y%m%dT%H%M%SZ')}.sql.gz", taken_at=taken)


# -- filename parsing -------------------------------------------------------
def test_parses_our_own_filenames():
    assert parse_archive_name("pap-20260808T032000Z.sql.gz") == datetime(
        2026, 8, 8, 3, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize("name", [
    "notes.txt",
    "pap-backup.sql.gz",
    "pap-20260808.sql.gz",
    "pap-20261399T032000Z.sql.gz",   # month 13 — not a real date
    "other-20260808T032000Z.sql.gz",
])
def test_foreign_or_malformed_names_are_ignored(name):
    """Anything not recognisably ours must be invisible to retention — the backup
    directory is not guaranteed to contain only our files."""
    assert parse_archive_name(name) is None


# -- retention --------------------------------------------------------------
def test_nothing_expires_below_the_daily_threshold():
    archives = [_daily(d) for d in range(5)]
    assert select_expired(archives, keep_daily=14, keep_weekly=8) == []


def test_the_newest_daily_backups_are_kept():
    archives = [_daily(d) for d in range(40)]
    kept = {a.name for a in archives} - {a.name for a in select_expired(
        archives, keep_daily=14, keep_weekly=8)}
    newest_14 = {_daily(d).name for d in range(14)}
    assert newest_14 <= kept


def test_one_backup_per_week_survives_beyond_the_daily_window():
    """The point of weekly retention: a corruption noticed a month late still has
    something older than the daily window to restore from."""
    archives = [_daily(d) for d in range(120)]
    expired = {a.name for a in select_expired(archives, keep_daily=14, keep_weekly=8)}
    kept = [a for a in archives if a.name not in expired]
    weeks = {a.taken_at.isocalendar()[:2] for a in kept}
    assert len(weeks) >= 8
    assert len(kept) < len(archives)


def test_zero_retention_expires_everything():
    archives = [_daily(d) for d in range(5)]
    assert len(select_expired(archives, keep_daily=0, keep_weekly=0)) == 5


def test_an_empty_directory_is_not_an_error():
    assert select_expired([], keep_daily=14, keep_weekly=8) == []


def test_a_single_backup_is_never_deleted_under_a_normal_policy():
    """The pathological case worth pinning: never leave yourself with nothing."""
    archives = [_daily(400)]
    assert select_expired(archives, keep_daily=14, keep_weekly=8) == []


# -- partial-archive cleanup ------------------------------------------------
def _fake_settings(tmp_path):
    return SimpleNamespace(
        pg_host="localhost", pg_port=5432, pg_db="pap",
        pg_user="pap", pg_password="irrelevant-for-this-test",
    )


def test_a_missing_pg_dump_leaves_no_partial_archive(tmp_path, monkeypatch):
    """Regression: gzip.open() creates the file before pg_dump writes a byte, so a
    failure past that point used to leave a zero-length .sql.gz behind — which
    `backup verify` would then pick up as the newest valid backup. An empty file
    that looks like a backup is worse than no backup at all."""
    def _missing(*args, **kwargs):
        raise FileNotFoundError("pg_dump")

    monkeypatch.setattr(backup_module.subprocess, "Popen", _missing)
    target = str(tmp_path / "pap-20260808T032000Z.sql.gz")

    with pytest.raises(BackupError, match="pg_dump was not found"):
        backup_module._dump(_fake_settings(tmp_path), target)

    assert not os.path.exists(target)
    assert os.listdir(tmp_path) == []


def test_a_suspiciously_small_dump_is_rejected(tmp_path, monkeypatch):
    """pg_dump exiting 0 having written nothing is a silent-corruption path; the
    size floor is what turns it into a loud failure."""
    class _Empty:
        returncode = 0
        stdout = io.BytesIO(b"")

        def communicate(self):
            return b"", b""

    monkeypatch.setattr(backup_module.subprocess, "Popen",
                        lambda *a, **k: _Empty())
    target = str(tmp_path / "pap-20260808T032000Z.sql.gz")

    with pytest.raises(BackupError, match="suspiciously small"):
        backup_module._dump(_fake_settings(tmp_path), target)

    assert not os.path.exists(target)
