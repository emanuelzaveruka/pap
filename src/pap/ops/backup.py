"""Database backup, off-machine copy, retention, and restore verification.

The server's disk has already reported *"likely to fail soon"*, and everything —
boot, LVM, root, Postgres — sits on that one device. So the local dump is only
the first half: what makes this a backup is the copy pushed to Google Drive,
because a backup on the disk you are afraid of losing is not a backup.

``verify`` exists for the same reason. A dump nobody has ever restored is a
hypothesis, not a safety net — and the moment you discover a corrupt dump should
not be the moment you need it. It restores the newest archive into a throwaway
database, counts rows, and drops it again.

Everything the *platform* generates (book PDFs, deliverables, resumes) is already
mirrored to Drive by design, so the database is the genuinely irreplaceable part.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from ..config import Settings
from ..db import SCHEMA, conninfo
from ..sinks.gdrive import DriveSink
from ..sinks.google_auth import GoogleAuthError

log = logging.getLogger(__name__)

FILENAME_RE = re.compile(r"^pap-(\d{8}T\d{6}Z)\.sql\.gz$")
FILENAME_FORMAT = "%Y%m%dT%H%M%SZ"

# A gzip member holding a real schema dump is comfortably over this. Anything
# smaller is an empty or truncated file, and treating it as a backup is how you
# discover the failure at restore time instead of at backup time.
MIN_ARCHIVE_BYTES = 200


class BackupError(RuntimeError):
    """The dump or restore could not be completed."""


@dataclass(frozen=True)
class Archive:
    name: str
    taken_at: datetime
    file_id: str | None = None


def parse_archive_name(name: str) -> datetime | None:
    """Timestamp encoded in a backup filename, or None if it isn't one of ours."""
    match = FILENAME_RE.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), FILENAME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_expired(archives: list[Archive], *, keep_daily: int, keep_weekly: int) -> list[Archive]:
    """Which archives to delete under a keep-N-daily plus one-per-week policy.

    Pure function so the retention rule can be tested without touching Drive or
    the filesystem — deleting the wrong backup is not a mistake you get to notice
    later.
    """
    ordered = sorted(archives, key=lambda a: a.taken_at, reverse=True)
    keep: set[str] = {a.name for a in ordered[:max(0, keep_daily)]}

    seen_weeks: set[tuple[int, int]] = set()
    for archive in ordered:
        year, week, _ = archive.taken_at.isocalendar()
        if (year, week) in seen_weeks:
            continue
        seen_weeks.add((year, week))
        if len(seen_weeks) <= max(0, keep_weekly):
            keep.add(archive.name)

    return [a for a in ordered if a.name not in keep]


def _dump(settings: Settings, target_path: str) -> None:
    """Run pg_dump into a gzipped file. Password goes via env, never argv."""
    env = dict(os.environ, PGPASSWORD=settings.pg_password)
    command = [
        "pg_dump",
        "--host", settings.pg_host,
        "--port", str(settings.pg_port),
        "--username", settings.pg_user,
        "--dbname", settings.pg_db,
        "--no-owner",
        "--no-privileges",
    ]
    log.info("running pg_dump -> %s", target_path)

    # A partial or empty file must never survive a failure. gzip.open() creates the
    # file before pg_dump has written a byte, so *any* failure past that point —
    # including pg_dump not existing — would otherwise leave a zero-length archive
    # that looks exactly like a valid backup until the day you try to restore it.
    # The finally block, not the error branch, is what guarantees cleanup.
    succeeded = False
    try:
        with gzip.open(target_path, "wb") as out:
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, env=env)
            except FileNotFoundError as exc:
                raise BackupError(
                    "pg_dump was not found — install postgresql-client "
                    "(it ships in the pap image; this is expected outside Docker)"
                ) from exc
            assert process.stdout is not None
            shutil.copyfileobj(process.stdout, out)
            _, stderr = process.communicate()

        if process.returncode != 0:
            message = stderr.decode("utf-8", "replace")[:500]
            if "server version mismatch" in message:
                # pg_dump refuses to dump a server newer than itself. In the image
                # both are 17; on Ubuntu 24.04 `apt install postgresql-client`
                # gives 16, which cannot dump the 17 container.
                message += (
                    "\n\npg_dump must be the same major version as the server, or newer. "
                    "Install the matching client from the PostgreSQL apt repository:\n"
                    "    sudo install -d /usr/share/postgresql-common/pgdg\n"
                    "    sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \\\n"
                    "        --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc\n"
                    "    echo \"deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \"\\\n"
                    "        \"https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main\" \\\n"
                    "        | sudo tee /etc/apt/sources.list.d/pgdg.list\n"
                    "    sudo apt update && sudo apt install -y postgresql-client-17\n"
                    "Or simply run backups inside the container, where the versions "
                    "already match:\n"
                    "    docker compose --profile cli run --rm pap backup db"
                )
            raise BackupError(f"pg_dump failed ({process.returncode}): {message}")
        if os.path.getsize(target_path) < MIN_ARCHIVE_BYTES:
            raise BackupError(
                f"pg_dump produced a suspiciously small archive "
                f"({os.path.getsize(target_path)} bytes) — refusing to keep it"
            )
        succeeded = True
    finally:
        if not succeeded:
            try:
                os.unlink(target_path)
                log.warning("removed the incomplete archive %s", target_path)
            except OSError:
                pass


def run_backup(settings: Settings, *, dry_run: bool = False) -> int:
    if not settings.backup.enabled:
        log.info("BACKUP_ENABLED is false — nothing to do")
        return 0

    stamp = datetime.now(timezone.utc).strftime(FILENAME_FORMAT)
    filename = f"pap-{stamp}.sql.gz"
    os.makedirs(settings.backup.directory, exist_ok=True)
    local_path = os.path.join(settings.backup.directory, filename)

    if dry_run:
        print(f"would dump {settings.pg_db} to {local_path}")
        print(f"would upload it to Drive folder {settings.google.drive_backup_folder}")
        return 0

    _dump(settings, local_path)
    size_mb = os.path.getsize(local_path) / (1024 * 1024)
    log.info("dump complete: %s (%.1f MiB)", local_path, size_mb)

    uploaded = False
    if settings.google.configured:
        try:
            drive = DriveSink(settings.google, state_dir=settings.state_dir)
            drive.upload(
                local_path,
                folder_path=settings.google.drive_backup_folder,
                mime_type="application/gzip",
                replace_existing=False,
            )
            uploaded = True
            _prune_drive(drive, settings)
        except (GoogleAuthError, OSError) as exc:
            # Keep the local dump and report loudly. A failed upload must not
            # destroy the only copy that exists.
            log.error("could not upload the dump to Drive: %s", exc)
    else:
        log.warning("Google is not configured — the dump stays on the same disk as the "
                    "database, which defeats the purpose if that disk fails")

    _prune_local(settings)
    print(f"backup: {local_path} ({size_mb:.1f} MiB)"
          f"{' — uploaded to Drive' if uploaded else ' — LOCAL ONLY'}")
    return 0 if uploaded or not settings.google.configured else 1


def _prune_local(settings: Settings) -> None:
    archives = []
    for name in os.listdir(settings.backup.directory):
        taken_at = parse_archive_name(name)
        if taken_at:
            archives.append(Archive(name=name, taken_at=taken_at))
    for archive in select_expired(archives,
                                  keep_daily=settings.backup.keep_daily,
                                  keep_weekly=settings.backup.keep_weekly):
        path = os.path.join(settings.backup.directory, archive.name)
        try:
            os.unlink(path)
            log.info("pruned local backup %s", archive.name)
        except OSError as exc:
            log.warning("could not prune %s: %s", path, exc)


def _prune_drive(drive: DriveSink, settings: Settings) -> None:
    archives = []
    for entry in drive.list_folder(settings.google.drive_backup_folder):
        taken_at = parse_archive_name(entry["name"])
        if taken_at:
            archives.append(Archive(name=entry["name"], taken_at=taken_at, file_id=entry["id"]))
    for archive in select_expired(archives,
                                  keep_daily=settings.backup.keep_daily,
                                  keep_weekly=settings.backup.keep_weekly):
        try:
            drive.delete(archive.file_id)
            log.info("pruned Drive backup %s", archive.name)
        except Exception as exc:  # noqa: BLE001 - pruning must never fail a backup run
            log.warning("could not prune %s from Drive: %s", archive.name, exc)


def run_verify(settings: Settings) -> int:
    """Restore the newest local dump into a throwaway database and count rows."""
    directory = settings.backup.directory
    if not os.path.isdir(directory):
        print(f"no backup directory at {directory}")
        return 1

    archives = sorted(
        (Archive(name=n, taken_at=t) for n in os.listdir(directory)
         if (t := parse_archive_name(n))),
        key=lambda a: a.taken_at,
        reverse=True,
    )
    if not archives:
        print(f"no backups found in {directory}")
        return 1

    newest = archives[0]
    scratch_db = f"{settings.pg_db}_verify_{newest.taken_at.strftime('%Y%m%d%H%M%S')}"
    archive_path = os.path.join(directory, newest.name)
    print(f"verifying {newest.name} by restoring into {scratch_db}")

    admin = psycopg.connect(conninfo(settings), autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{scratch_db}"')
    except psycopg.Error as exc:
        admin.close()
        raise BackupError(f"could not create the scratch database: {exc}") from exc

    # Bound before the try so the finally can always run — otherwise a failure in
    # NamedTemporaryFile would raise NameError from the cleanup and mask the real
    # error, leaving the scratch database behind as well.
    plain_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
            plain_path = tmp.name
        with gzip.open(archive_path, "rb") as src, open(plain_path, "wb") as dst:
            shutil.copyfileobj(src, dst)

        if os.path.getsize(plain_path) < MIN_ARCHIVE_BYTES:
            raise BackupError(
                f"{newest.name} decompresses to {os.path.getsize(plain_path)} bytes — "
                f"it is empty or truncated, not a usable backup"
            )

        env = dict(os.environ, PGPASSWORD=settings.pg_password)
        try:
            result = subprocess.run(
                ["psql", "--host", settings.pg_host, "--port", str(settings.pg_port),
                 "--username", settings.pg_user, "--dbname", scratch_db,
                 "--quiet", "--file", plain_path, "--set", "ON_ERROR_STOP=on"],
                env=env, capture_output=True,
            )
        except FileNotFoundError as exc:
            raise BackupError(
                "psql was not found — install postgresql-client "
                "(it ships in the pap image; this is expected outside Docker)"
            ) from exc
        if result.returncode != 0:
            raise BackupError(
                f"restore failed: {result.stderr.decode('utf-8', 'replace')[:800]}"
            )

        counts = _count_rows(settings, scratch_db)
        total = sum(counts.values())
        for table, count in sorted(counts.items()):
            print(f"  {table:<20} {count:>8}")
        print(f"restore OK — {total} row(s) across {len(counts)} table(s)")
        # An empty restore is a green light that means nothing; treat it as failure.
        return 0 if total > 0 else 1
    finally:
        if plain_path:
            try:
                os.unlink(plain_path)
            except OSError:
                pass
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch_db}"')
        admin.close()


def _count_rows(settings: Settings, database: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with psycopg.connect(conninfo(settings, dbname=database)) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (SCHEMA,),
        )
        tables = [r[0] for r in cur.fetchall()]
        for table in tables:
            cur.execute(f'SELECT count(*) FROM {SCHEMA}."{table}"')
            counts[table] = cur.fetchone()[0]
    return counts
