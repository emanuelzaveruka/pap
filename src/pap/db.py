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

    # -- books --------------------------------------------------------------
    def discipline_id_for(self, external_id: str) -> int | None:
        """The stored discipline row for a Studeo shortname, if the scraper saw it."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT id FROM {SCHEMA}.discipline WHERE external_id = %s "
                f"ORDER BY id DESC LIMIT 1",
                (external_id,),
            )
            row = cur.fetchone()
        return row["id"] if row else None

    def find_book(self, discipline_id: int | None, external_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {SCHEMA}.book "
                f"WHERE external_id = %s AND discipline_id IS NOT DISTINCT FROM %s",
                (external_id, discipline_id),
            )
            return cur.fetchone()

    def find_book_by_sha256(self, sha256: str, *, exclude_id: int | None = None
                            ) -> dict[str, Any] | None:
        """A previously stored book with identical bytes.

        This is what makes the same PDF shared across discipline offerings download
        and upload once instead of once per offering.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {SCHEMA}.book WHERE sha256 = %s AND id <> %s "
                f"AND drive_file_id IS NOT NULL LIMIT 1",
                (sha256, exclude_id or -1),
            )
            return cur.fetchone()

    def upsert_book(
        self,
        *,
        discipline_id: int | None,
        external_id: str,
        title: str,
        source_url: str | None,
        filename: str | None,
        module_code: str | None = None,
    ) -> int:
        """Register a book, or refresh what we know about it. Returns its id.

        ``source_url`` is a short-lived signed link, so it is refreshed on every
        pass rather than treated as stable.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SCHEMA}.book
                        (discipline_id, external_id, title, source_url, filename, config)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (discipline_id, external_id) WHERE external_id IS NOT NULL
                    DO UPDATE SET title = EXCLUDED.title,
                                  source_url = EXCLUDED.source_url,
                                  filename = COALESCE(EXCLUDED.filename, {SCHEMA}.book.filename)
                    RETURNING id""",
                (discipline_id, external_id, title, source_url, filename,
                 Jsonb({"module_code": module_code} if module_code else {})),
            )
            book_id = cur.fetchone()["id"]
        self.conn.commit()
        return book_id

    def mark_book_stored(self, book_id: int, *, local_path: str, sha256: str,
                         bytes_: int, drive_file_id: str | None) -> None:
        """Record a completed download and (when Google is configured) upload.

        ``uploaded_at`` is only set when a Drive id exists, so a locally-downloaded
        book stays in the work queue until it actually reaches Drive.

        The upload decision is computed in Python rather than as a repeated
        ``CASE WHEN %s IS NULL`` over the same parameter: Postgres cannot infer a
        type for a bare NULL placeholder used only in a null test, and fails the
        statement with "could not determine data type of parameter".
        """
        uploaded = drive_file_id is not None
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.book SET local_path = %s, sha256 = %s, bytes = %s, "
                f"drive_file_id = %s, downloaded_at = now(), "
                f"uploaded_at = CASE WHEN %s THEN now() ELSE uploaded_at END, "
                f"status = %s WHERE id = %s",
                (local_path, sha256, bytes_, drive_file_id, uploaded,
                 "stored" if uploaded else "downloaded", book_id),
            )
        self.conn.commit()

    def rollback(self) -> None:
        """Abandon a failed transaction so the connection stays usable.

        Postgres puts a connection into a failed state after any error, and every
        later statement returns "current transaction is aborted" until it is rolled
        back. Loops that process many items must call this when one item fails, or a
        single bad row silently fails everything after it.
        """
        try:
            self.conn.rollback()
        except Exception:  # noqa: BLE001 - nothing useful to do if rollback itself fails
            log.warning("could not roll back the failed transaction", exc_info=True)

    def books(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT b.id, b.title, b.status, b.bytes, b.sha256, b.drive_file_id, "
                f"       b.total_units, d.name AS discipline "
                f"  FROM {SCHEMA}.book b "
                f"  LEFT JOIN {SCHEMA}.discipline d ON d.id = b.discipline_id "
                f" ORDER BY b.created_at DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()

    # -- llm ledger ---------------------------------------------------------
    def record_llm_call(
        self,
        *,
        purpose: str,
        provider: str,
        model: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        ok: bool = True,
        error_text: str | None = None,
        run_id: int | None = None,
    ) -> None:
        """Record one LLM call, whatever its outcome.

        Failures are recorded too — a table containing only successes would make
        every vendor look equally reliable.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.llm_call (purpose, provider, model, input_tokens, "
                f"output_tokens, latency_ms, ok, error_text, run_id) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (purpose, provider, model, input_tokens, output_tokens, latency_ms,
                 ok, (error_text or None) and error_text[:2000], run_id),
            )
        self.conn.commit()

    def llm_usage(self, *, days: int = 30) -> list[dict[str, Any]]:
        """Per-provider spend and reliability — the point of the ledger."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT provider, model, purpose,
                           count(*)                                  AS calls,
                           count(*) FILTER (WHERE NOT ok)            AS failures,
                           coalesce(sum(input_tokens), 0)            AS input_tokens,
                           coalesce(sum(output_tokens), 0)           AS output_tokens,
                           round(avg(latency_ms))                    AS avg_latency_ms
                      FROM {SCHEMA}.llm_call
                     WHERE created_at >= now() - make_interval(days => %s)
                     GROUP BY provider, model, purpose
                     ORDER BY provider, purpose""",
                (days,),
            )
            return cur.fetchall()

    # -- deadlines ----------------------------------------------------------
    def upsert_deadline(self, item_id: int, due_at: Any) -> int:
        """Record an activity's prazo. A changed date clears nothing — the sync
        compares ``due_at`` against ``synced_due_at`` to decide what to patch."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.deadline (item_id, due_at) VALUES (%s, %s) "
                f"ON CONFLICT (item_id) DO UPDATE SET due_at = EXCLUDED.due_at "
                f"RETURNING id",
                (item_id, due_at),
            )
            deadline_id = cur.fetchone()["id"]
        self.conn.commit()
        return deadline_id

    def deadlines_to_sync(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Deadlines never synced, or whose date changed since the last sync.

        ``synced_due_at IS DISTINCT FROM due_at`` is what makes a re-run free:
        unchanged rows are not returned at all, so no API call is made for them.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT d.id, d.item_id, d.due_at, d.gcal_event_id, d.synced_due_at, "
                f"       i.source, i.title, i.url, i.payload "
                f"  FROM {SCHEMA}.deadline d "
                f"  JOIN {SCHEMA}.item i ON i.id = d.item_id "
                f" WHERE d.synced_at IS NULL OR d.synced_due_at IS DISTINCT FROM d.due_at "
                f" ORDER BY d.due_at LIMIT %s",
                (limit,),
            )
            return cur.fetchall()

    def mark_deadline_synced(self, deadline_id: int, event_id: str, due_at: Any) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.deadline SET gcal_event_id = %s, synced_due_at = %s, "
                f"synced_at = now() WHERE id = %s",
                (event_id, due_at, deadline_id),
            )
        self.conn.commit()

    # -- notifications ------------------------------------------------------
    def upsert_deliverable(
        self,
        *,
        item_id: int,
        pattern_name: str,
        pattern_version: int | str,
        fmt: str = "docx",
        local_path: str | None = None,
        drive_file_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        status: str = "draft",
    ) -> int:
        """Record a generated deliverable, or update it in place. Returns its id.

        ``UNIQUE (item_id, pattern_name, fmt)`` is what makes regeneration safe:
        rewriting a MAPA updates the row rather than leaving two records pointing
        at the same activity, in the same way the Drive upload replaces the file
        rather than adding a second one.

        ``pattern_version`` is stored as text because the column is text — it is
        an identifier to trace a document back to its spec, not a number to do
        arithmetic on.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SCHEMA}.deliverable
                        (item_id, pattern_name, pattern_version, fmt, local_path,
                         drive_file_id, provider, model, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (item_id, pattern_name, fmt)
                    DO UPDATE SET pattern_version = EXCLUDED.pattern_version,
                                  local_path      = COALESCE(EXCLUDED.local_path,
                                                             {SCHEMA}.deliverable.local_path),
                                  drive_file_id   = COALESCE(EXCLUDED.drive_file_id,
                                                             {SCHEMA}.deliverable.drive_file_id),
                                  provider        = EXCLUDED.provider,
                                  model           = EXCLUDED.model,
                                  status          = EXCLUDED.status,
                                  generated_at    = now()
                    RETURNING id""",
                (item_id, pattern_name, str(pattern_version), fmt, local_path,
                 drive_file_id, provider, model, status),
            )
            row = cur.fetchone()
        self.conn.commit()
        return row["id"]

    def deliverables(self, *, item_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where = "WHERE d.item_id = %s" if item_id else ""
        params = (item_id, limit) if item_id else (limit,)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT d.*, i.title AS item_title, i.external_id AS item_external_id
                      FROM {SCHEMA}.deliverable d
                      JOIN {SCHEMA}.item i ON i.id = d.item_id
                      {where}
                     ORDER BY d.generated_at DESC LIMIT %s""",
                params,
            )
            return cur.fetchall()

    # -- book resume feed (Phase 4) -----------------------------------------
    def set_total_units(self, book_id: int, total: int) -> None:
        """Record how many units this book has. Set once per pass."""
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE {SCHEMA}.book SET total_units = %s WHERE id = %s",
                        (total, book_id))
        self.conn.commit()

    def resume_progress(self, book_id: int) -> dict[str, Any]:
        """Where the feed is for this book: pass, units done, total, status."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT b.id, b.title, b.local_path, b.status, b.total_units,
                           b.current_pass, b.config, b.completed_at,
                           d.name AS discipline,
                           (SELECT count(*) FROM {SCHEMA}.book_resume r
                             WHERE r.book_id = b.id AND r.pass_number = b.current_pass)
                             AS units_done,
                           (SELECT count(*) FROM {SCHEMA}.book_resume r
                             WHERE r.book_id = b.id AND r.pass_number = b.current_pass
                               AND r.sent_at IS NOT NULL) AS units_sent
                      FROM {SCHEMA}.book b
                      LEFT JOIN {SCHEMA}.discipline d ON d.id = b.discipline_id
                     WHERE b.id = %s""",
                (book_id,),
            )
            return cur.fetchone()

    def next_unit_index(self, book_id: int, pass_number: int) -> int:
        """The first unit of this pass that has no resume yet.

        Derived from what exists rather than from a stored cursor: a counter and
        the rows it points at can disagree after a crash, and the rows are the
        thing that actually got delivered.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT coalesce(max(unit_index) + 1, 0) AS next
                      FROM {SCHEMA}.book_resume
                     WHERE book_id = %s AND pass_number = %s""",
                (book_id, pass_number),
            )
            return cur.fetchone()["next"]

    def save_resume(
        self,
        *,
        book_id: int,
        pass_number: int,
        unit_index: int,
        unit_label: str | None,
        text: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> int | None:
        """Store one unit's resume. Returns None when it already existed.

        ``UNIQUE (book_id, pass_number, unit_index)`` is the guarantee that a unit
        is never generated twice within a pass — the constraint does it, not a
        check-then-insert, which would reintroduce the race it exists to prevent.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {SCHEMA}.book_resume
                        (book_id, pass_number, unit_index, unit_label, text,
                         provider, model, prompt_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (book_id, pass_number, unit_index) DO NOTHING
                    RETURNING id""",
                (book_id, pass_number, unit_index, unit_label, text,
                 provider, model, prompt_version),
            )
            row = cur.fetchone()
        self.conn.commit()
        return row["id"] if row else None

    def mark_resume_sent(self, resume_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE {SCHEMA}.book_resume SET sent_at = now() WHERE id = %s",
                        (resume_id,))
        self.conn.commit()

    def complete_book(self, book_id: int) -> None:
        """Mark the pass finished. The scheduler skips a completed book, which is
        what makes the feed stop by itself rather than looping forever."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.book SET status = 'completed', completed_at = now() "
                f"WHERE id = %s", (book_id,))
        self.conn.commit()

    def start_new_pass(self, book_id: int) -> int:
        """Begin a deeper pass over the same book. Returns the new pass number.

        Existing resumes are kept: the point of a second pass is to compare it
        with the first, and the UNIQUE key is scoped by pass so nothing collides.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {SCHEMA}.book
                       SET current_pass = current_pass + 1,
                           status = 'pending',
                           completed_at = NULL
                     WHERE id = %s
                 RETURNING current_pass""",
                (book_id,),
            )
            row = cur.fetchone()
        self.conn.commit()
        return row["current_pass"]

    def books_pending_resume(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Books the feed may still deliver from, oldest first."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT b.id, b.title, b.local_path, b.status, b.total_units,
                           b.current_pass, b.config, d.name AS discipline
                      FROM {SCHEMA}.book b
                      LEFT JOIN {SCHEMA}.discipline d ON d.id = b.discipline_id
                     WHERE b.status <> 'completed' AND b.local_path IS NOT NULL
                     ORDER BY b.id LIMIT %s""",
                (limit,),
            )
            return cur.fetchall()

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
