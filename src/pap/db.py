"""PostgreSQL access layer and migration runner.

All application state lives in the ``pap`` schema. Queries are raw SQL through
psycopg v3 — no ORM, matching the rest of your ETL projects.

Migrations are numbered files in ``migrations/`` applied in filename order and
recorded in ``pap.schema_migration`` with a checksum. The checksum is not
bureaucracy: editing an already-applied migration is a silent way to make two
environments disagree about their own schema, so it is refused rather than
ignored. Add a new file instead.

The role and database are created by the postgres container from POSTGRES_USER /
POSTGRES_DB, so there is no create-role migration.
"""

from __future__ import annotations

import hashlib
import logging
import os  # noqa: F401  (also used by connection_hint for container detection)
from dataclasses import dataclass
from typing import Any, Iterable

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Settings

log = logging.getLogger(__name__)

SCHEMA = "pap"


class MigrationError(RuntimeError):
    """Raised when the migration history on disk and in the database disagree."""


def conninfo(settings: Settings, *, dbname: str | None = None) -> str:
    """Build the libpq connection string with proper escaping.

    ``make_conninfo`` rather than an f-string because libpq's key=value format
    requires quoting for values containing spaces, single quotes or backslashes.
    Hand-formatting works right up until someone picks a password with a space in
    it, and then fails as a confusing "missing = after ..." parse error rather
    than anything resembling a credential problem.
    """
    return make_conninfo(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=dbname or settings.pg_db,
        user=settings.pg_user,
        password=settings.pg_password,
    )


def connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(conninfo(settings), row_factory=dict_row)


def connection_hint(settings: Settings, exc: BaseException | None = None) -> str | None:
    """A specific next step for the connection failures that actually happen.

    The raw psycopg error is accurate but names none of the things you have to
    change — which file, which container. These two cover essentially every
    first-run failure, so they are worth spelling out rather than leaving to a
    search engine.
    """
    detail = str(exc or "").lower()
    in_container = os.path.exists("/.dockerenv")
    host = settings.pg_host
    looks_like_service_name = "." not in host and host not in ("localhost", "127.0.0.1", "::1")

    if "password authentication failed" in detail:
        return (
            "PG_PASSWORD does not match the password the server was created with. A "
            "postgres container fixes its password on FIRST START and ignores the "
            "environment variable afterwards, so editing .env alone changes nothing.\n"
            "\n"
            "Recreate it, reading .env with the SAME parser this app uses. Do not use "
            "`source .env` — .env is not a shell script: unquoted values containing "
            "spaces or parentheses are a syntax error, and on a CRLF file the shell "
            "appends a carriage return to every value while python-dotenv strips it, so "
            "the two disagree by one invisible character.\n"
            "\n"
            '    PW=$(python -c "from dotenv import dotenv_values as d; '
            "print(d('.env')['PG_PASSWORD'].strip())\")\n"
            "    docker rm -f pap-local-pg\n"
            f"    docker run -d --name pap-local-pg -e POSTGRES_DB={settings.pg_db} \\\n"
            f"      -e POSTGRES_USER={settings.pg_user} -e POSTGRES_PASSWORD=\"$PW\" \\\n"
            f"      -p {settings.pg_port}:5432 postgres:17-alpine\n"
            "    pap migrate"
        )
    if looks_like_service_name and not in_container:
        return (
            f"PG_HOST is {host!r}, which only resolves inside the Docker network. "
            f"Running on the host, set PG_HOST=127.0.0.1 in .env (and PG_PORT to the "
            f"published port), or override it for one command:\n"
            f"    PG_HOST=127.0.0.1 pap <command>"
        )
    if in_container and host in ("localhost", "127.0.0.1"):
        return (
            "PG_HOST is loopback inside a container, which points at the container "
            "itself. Use the compose service name (PG_HOST=postgres)."
        )
    return None


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunSummary:
    source: str
    runs: int
    failures: int
    last_status: str | None
    last_started_at: Any
    last_finished_at: Any
    items_found: int
    items_new: int


class Database:
    """Every read and write the platform performs. One instance per process."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    # -- migrations ---------------------------------------------------------
    def apply_migrations(self, migrations_dir: str, *, dry_run: bool = False) -> list[str]:
        """Apply every migration not yet recorded. Returns the filenames applied."""
        with self.conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migration (
                        filename   text PRIMARY KEY,
                        checksum   text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )"""
            )
        self.conn.commit()

        with self.conn.cursor() as cur:
            cur.execute(f"SELECT filename, checksum FROM {SCHEMA}.schema_migration")
            applied = {r["filename"]: r["checksum"] for r in cur.fetchall()}

        files = sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))
        if not files:
            raise MigrationError(f"no .sql files found in {migrations_dir}")

        pending: list[tuple[str, str, str]] = []
        for filename in files:
            with open(os.path.join(migrations_dir, filename), encoding="utf-8") as fh:
                body = fh.read()
            digest = _checksum(body)
            if filename in applied:
                if applied[filename] != digest:
                    raise MigrationError(
                        f"{filename} has changed since it was applied. Editing an applied "
                        f"migration makes environments silently disagree about their schema — "
                        f"add a new migration instead of modifying this one."
                    )
                continue
            pending.append((filename, body, digest))

        if dry_run:
            for filename, _, _ in pending:
                log.info("would apply migration %s", filename)
            return [f for f, _, _ in pending]

        for filename, body, digest in pending:
            log.info("applying migration %s", filename)
            with self.conn.cursor() as cur:
                cur.execute(body)
                cur.execute(
                    f"INSERT INTO {SCHEMA}.schema_migration (filename, checksum) VALUES (%s, %s)",
                    (filename, digest),
                )
            self.conn.commit()
        return [f for f, _, _ in pending]

    # -- run ledger ---------------------------------------------------------
    def start_run(self, source: str, *, dry_run: bool = False) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.source_run (source, status, dry_run) "
                f"VALUES (%s, 'running', %s) RETURNING id",
                (source, dry_run),
            )
            run_id = cur.fetchone()["id"]
        self.conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        items_found: int = 0,
        items_new: int = 0,
        error_text: str | None = None,
        sentry_event_id: str | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.source_run SET status = %s, finished_at = now(), "
                f"items_found = %s, items_new = %s, error_text = %s, sentry_event_id = %s "
                f"WHERE id = %s",
                (status, items_found, items_new, error_text, sentry_event_id, run_id),
            )
        self.conn.commit()

    def run_summary(self, *, days: int = 7) -> list[RunSummary]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT source,
                           count(*)                                        AS runs,
                           count(*) FILTER (WHERE status = 'failed')       AS failures,
                           max(started_at)                                 AS last_started_at,
                           sum(items_found)                                AS items_found,
                           sum(items_new)                                  AS items_new,
                           (array_agg(status ORDER BY started_at DESC))[1] AS last_status,
                           (array_agg(finished_at ORDER BY started_at DESC))[1] AS last_finished_at
                      FROM {SCHEMA}.source_run
                     WHERE started_at >= now() - make_interval(days => %s)
                     GROUP BY source ORDER BY source""",
                (days,),
            )
            return [
                RunSummary(
                    source=r["source"],
                    runs=r["runs"],
                    failures=r["failures"],
                    last_status=r["last_status"],
                    last_started_at=r["last_started_at"],
                    last_finished_at=r["last_finished_at"],
                    items_found=r["items_found"] or 0,
                    items_new=r["items_new"] or 0,
                )
                for r in cur.fetchall()
            ]

    # -- academic structure -------------------------------------------------
    def upsert_module(self, code: str, *, year: int | None, seq: int | None,
                      label: str | None) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.module (code, year, seq, label) VALUES (%s, %s, %s, %s) "
                f"ON CONFLICT (code) DO UPDATE SET "
                f"  year = COALESCE(EXCLUDED.year, {SCHEMA}.module.year), "
                f"  seq = COALESCE(EXCLUDED.seq, {SCHEMA}.module.seq), "
                f"  label = COALESCE(EXCLUDED.label, {SCHEMA}.module.label) "
                f"RETURNING id",
                (code, year, seq, label),
            )
            module_id = cur.fetchone()["id"]
        self.conn.commit()
        return module_id

    def upsert_discipline(self, module_id: int, external_id: str, name: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.discipline (module_id, external_id, name) "
                f"VALUES (%s, %s, %s) "
                f"ON CONFLICT (module_id, external_id) DO UPDATE SET name = EXCLUDED.name "
                f"RETURNING id",
                (module_id, external_id, name),
            )
            discipline_id = cur.fetchone()["id"]
        self.conn.commit()
        return discipline_id

    # -- items --------------------------------------------------------------
    def upsert_item(
        self,
        *,
        source: str,
        external_id: str,
        content_hash: str,
        kind: str,
        title: str,
        url: str | None,
        payload: dict,
        discipline_id: int | None = None,
    ) -> tuple[int, bool, bool]:
        """Insert or refresh one item. Returns ``(item_id, is_new, content_changed)``.

        The ``existing`` CTE is evaluated against the pre-insert snapshot, which is
        how the previous hash survives long enough to be compared. ``xmax = 0`` is
        the standard way to tell an INSERT from an ON CONFLICT UPDATE.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"""WITH existing AS (
                        SELECT content_hash FROM {SCHEMA}.item
                         WHERE source = %(source)s AND external_id = %(external_id)s
                    ),
                    upserted AS (
                        INSERT INTO {SCHEMA}.item
                            (source, external_id, content_hash, kind, title, url,
                             discipline_id, payload)
                        VALUES (%(source)s, %(external_id)s, %(content_hash)s, %(kind)s,
                                %(title)s, %(url)s, %(discipline_id)s, %(payload)s)
                        ON CONFLICT (source, external_id) DO UPDATE SET
                            last_seen_at  = now(),
                            content_hash  = EXCLUDED.content_hash,
                            kind          = EXCLUDED.kind,
                            title         = EXCLUDED.title,
                            url           = EXCLUDED.url,
                            payload       = EXCLUDED.payload,
                            discipline_id = COALESCE(EXCLUDED.discipline_id,
                                                     {SCHEMA}.item.discipline_id),
                            changed_at    = CASE
                                WHEN {SCHEMA}.item.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                                THEN now() ELSE {SCHEMA}.item.changed_at END
                        RETURNING id, (xmax = 0) AS inserted, content_hash
                    )
                    SELECT u.id, u.inserted, u.content_hash, e.content_hash AS previous_hash
                      FROM upserted u LEFT JOIN existing e ON true""",
                {
                    "source": source,
                    "external_id": external_id,
                    "content_hash": content_hash,
                    "kind": kind,
                    "title": title,
                    "url": url,
                    "discipline_id": discipline_id,
                    "payload": Jsonb(payload),
                },
            )
            row = cur.fetchone()
        self.conn.commit()
        is_new = bool(row["inserted"])
        changed = (not is_new) and row["previous_hash"] != row["content_hash"]
        return row["id"], is_new, changed

    def items_awaiting_notification(self, *, source: str | None = None,
                                    limit: int = 200) -> list[dict[str, Any]]:
        clause = "AND source = %(source)s" if source else ""
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT id, source, external_id, kind, title, url, payload, first_seen_at "
                f"FROM {SCHEMA}.item WHERE notified_at IS NULL {clause} "
                f"ORDER BY first_seen_at LIMIT %(limit)s",
                {"source": source, "limit": limit},
            )
            return cur.fetchall()

    def mark_items_notified(self, item_ids: Iterable[int]) -> None:
        item_ids = list(item_ids)
        if not item_ids:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.item SET notified_at = now() WHERE id = ANY(%s)",
                (item_ids,),
            )
        self.conn.commit()

    def count_items(self, source: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM {SCHEMA}.item WHERE source = %s", (source,))
            return cur.fetchone()["n"]

    # -- notifications ------------------------------------------------------
    def enqueue_notification(
        self,
        *,
        dedupe_key: str,
        channel: str,
        title: str,
        body: str = "",
        url: str | None = None,
        payload: dict | None = None,
    ) -> bool:
        """Queue one notification. Returns False when it was already queued.

        The UNIQUE constraint on ``dedupe_key`` does the deduplication, so two
        concurrent jobs racing on the same event cannot both enqueue it.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.notification (dedupe_key, channel, title, body, url, payload) "
                f"VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING "
                f"RETURNING id",
                (dedupe_key, channel, title, body, url, Jsonb(payload or {})),
            )
            row = cur.fetchone()
        self.conn.commit()
        return row is not None

    def pending_notifications(self, *, limit: int = 50,
                              max_attempts: int = 5) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT id, dedupe_key, channel, title, body, url, payload, attempts "
                f"FROM {SCHEMA}.notification "
                f"WHERE state = 'pending' AND attempts < %s ORDER BY created_at LIMIT %s",
                (max_attempts, limit),
            )
            return cur.fetchall()

    def mark_notification_sent(self, notification_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.notification SET state = 'sent', sent_at = now(), "
                f"attempts = attempts + 1, last_error = NULL WHERE id = %s",
                (notification_id,),
            )
        self.conn.commit()

    def mark_notification_failed(self, notification_id: int, error: str,
                                 *, max_attempts: int = 5) -> None:
        """Record a failure. The row stays 'pending' until attempts are exhausted,
        so a transient Telegram outage retries on the next dispatch run instead of
        losing the message."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.notification SET attempts = attempts + 1, last_error = %s, "
                f"state = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END "
                f"WHERE id = %s",
                (error[:2000], max_attempts, notification_id),
            )
        self.conn.commit()

    def notification_counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT state, count(*) AS n FROM {SCHEMA}.notification GROUP BY state"
            )
            return {r["state"]: r["n"] for r in cur.fetchall()}
