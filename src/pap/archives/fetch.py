"""Downloads course books and files them into Drive.

This is the shared dependency both later phases were blocked on: the book is the
*source* for the resume feed (Phase 4) and the *context* for a MAPA deliverable
(Phase 3). Neither can start until the PDF is on disk and in Drive.

The pipeline is: discover → download → hash → upload → record. Four properties are
deliberate:

**Resumable transfers.** Books are large (the first real one was 9 MB) and
``conteudoava`` honours Range requests, so an interrupted download continues from
where it stopped instead of starting over. On a home connection that is the
difference between "retry later" and "finishes".

**Content-level dedupe by sha256.** The same PDF is frequently the livro for several
offerings of a discipline. A hash match means the bytes are already stored, so the
existing Drive file is reused rather than uploaded again.

**A partial file is never mistaken for a complete one.** Bytes land in ``*.part``
and are only renamed after the transfer finishes and the size checks out — the same
rule the backup dump follows, for the same reason: a truncated artifact that looks
finished is worse than an obvious failure.

**Drive layout mirrors Studeo.** Books go to
``Studeo/<ano>/<módulo>/<disciplina>/livros/`` so a file is findable from a module
code alone.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass

import requests

from ..config import Settings
from ..core.dedupe import sha256_file
from ..core.models import parse_module_code
from ..db import Database
from ..sinks.gdrive import DriveSink, studeo_folder_path

log = logging.getLogger(__name__)

CHUNK_BYTES = 1024 * 1024

# A course book is never this small; anything under it is an error page that
# answered with HTTP 200.
MIN_BOOK_BYTES = 50 * 1024

# Guard against a runaway download filling the server's already-failing disk.
DEFAULT_MAX_BOOK_MB = 250


@dataclass
class BookOutcome:
    downloaded: int = 0
    reused: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (f"downloaded={self.downloaded} reused={self.reused} "
                f"uploaded={self.uploaded} skipped={self.skipped} failed={self.failed}")


def download_to(url: str, target_path: str, *, timeout: int = 120,
                max_bytes: int | None = None) -> int:
    """Stream a URL to disk, resuming a previous partial attempt. Returns total bytes.

    Writes to ``target_path + '.part'`` and renames on success. If the server
    ignores the Range header (answering 200 rather than 206) the partial file is
    discarded and the transfer restarts — appending to it would corrupt the file
    with a duplicated prefix, which is exactly the kind of damage that only shows
    up much later when something tries to read the PDF.
    """
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    partial_path = f"{target_path}.part"
    already = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0

    headers = {"Range": f"bytes={already}-"} if already else {}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as response:
        if response.status_code not in (200, 206):
            raise RuntimeError(f"download returned {response.status_code}")

        mode = "ab"
        if already and response.status_code == 200:
            log.info("server ignored the range request — restarting the download")
            already, mode = 0, "wb"
        elif already:
            log.info("resuming from %.1f MiB", already / (1024 * 1024))

        expected = response.headers.get("Content-Length")
        if expected and max_bytes:
            projected = already + int(expected)
            if projected > max_bytes:
                raise RuntimeError(
                    f"refusing to download {projected / (1024 * 1024):.0f} MiB — "
                    f"over the {max_bytes / (1024 * 1024):.0f} MiB cap "
                    f"(raise ARCHIVE_MAX_BOOK_MB if this is legitimate)"
                )

        written = already
        with open(partial_path, mode) as out:
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                if not chunk:
                    continue
                out.write(chunk)
                written += len(chunk)
                if max_bytes and written > max_bytes:
                    raise RuntimeError(
                        f"download exceeded the {max_bytes / (1024 * 1024):.0f} MiB cap"
                    )

    if written < MIN_BOOK_BYTES:
        os.unlink(partial_path)
        raise RuntimeError(
            f"downloaded only {written} bytes — the server almost certainly returned "
            f"an error page with HTTP 200"
        )

    # Rename only now: until this line, nothing on disk looks like a finished book.
    shutil.move(partial_path, target_path)
    return written


def _drive_folder(settings: Settings, module_code: str | None, discipline: str) -> str:
    parsed = parse_module_code(module_code or "")
    return studeo_folder_path(
        settings.google.drive_root_folder,
        year=parsed.year,
        module_folder=parsed.folder_name,
        discipline=discipline,
        bucket="livros",
    )


def archive_books(
    db: Database,
    settings: Settings,
    *,
    dry_run: bool = False,
    only_discipline: str | None = None,
    limit: int | None = None,
) -> BookOutcome:
    """Discover, download and file every book for the enrolled disciplines."""
    from ..sources.studeo import StudeoSource

    outcome = BookOutcome()
    source = StudeoSource(settings)
    if not source.enabled:
        log.error("studeo is not configured. %s", source.disabled_reason)
        source.close()
        return outcome

    max_bytes = settings.archive.max_book_mb * 1024 * 1024
    drive: DriveSink | None = None
    if settings.google.configured and not dry_run:
        drive = DriveSink(settings.google, state_dir=settings.state_dir)
    elif not settings.google.configured:
        log.warning("Google is not configured — books will be stored locally only")

    try:
        # Reuses the agenda-derived enrolment: one call, no configured list.
        source.ensure_token()
        entries = source.session.get_json(
            f"{source.base_url}/objeto-ensino-api-controller/api/plano-estudo/disciplinas-usuario",
            headers=source._headers(),
        )
        from ..sources.studeo import disciplines_from_plano

        disciplines = disciplines_from_plano(
            entries if isinstance(entries, list) else (entries or {}).get("content") or []
        )
        if only_discipline:
            disciplines = {k: v for k, v in disciplines.items() if k == only_discipline}
            if not disciplines:
                log.error("discipline %r is not among the enrolled ones", only_discipline)
                return outcome

        log.info("checking %d discipline(s) for books", len(disciplines))
        processed = 0
        for shortname, nome in disciplines.items():
            # Discovery is wrapped too: a discipline whose material listing fails
            # must not abort the other three. The first version wrapped only the
            # download, so one bad book took the whole run down with it.
            try:
                catalogue = source.books(shortname)
            except Exception as exc:  # noqa: BLE001
                outcome.failed += 1
                db.rollback()
                log.error("could not list books for %s (%s): %s", nome, shortname, exc)
                continue

            for book in catalogue:
                if limit is not None and processed >= limit:
                    log.info("reached --limit %d", limit)
                    return outcome
                processed += 1
                try:
                    _handle_book(db, settings, drive, source, book, nome, outcome,
                                 dry_run=dry_run, max_bytes=max_bytes)
                except Exception as exc:  # noqa: BLE001 - one book must not stop the rest
                    outcome.failed += 1
                    # Without this, a database error leaves the connection in a
                    # failed transaction and every remaining book fails too.
                    db.rollback()
                    log.error("could not archive %r (%s): %s",
                              book.get("title"), nome, " ".join(str(exc).split())[:200])
    finally:
        source.close()

    return outcome


def _handle_book(db, settings, drive, source, book, discipline_name, outcome, *,
                 dry_run: bool, max_bytes: int) -> None:
    shortname = book["disciplina_id"]
    module_code = parse_module_code(_module_from(shortname)).code or None
    folder = _drive_folder(settings, _module_from(shortname), discipline_name)

    if dry_run:
        # Deliberately does not resolve the download link: a dry run should cost
        # one listing call per discipline and nothing else.
        log.info("would archive %r -> %s/%s", book["title"], folder, book["filename"])
        outcome.downloaded += 1
        return

    discipline_id = db.discipline_id_for(shortname)
    existing = db.find_book(discipline_id, book["external_id"])
    if existing and existing.get("uploaded_at") and existing.get("drive_file_id"):
        log.info("already archived: %s", book["title"])
        outcome.skipped += 1
        return

    target = os.path.join(settings.archive.books_dir, book["filename"])
    have_bytes = bool(existing and existing.get("local_path")
                      and os.path.exists(existing["local_path"]))

    # Resolved here, immediately before the transfer, so the signed link is fresh —
    # and so a book whose link cannot be resolved fails alone.
    download_url = None
    if not have_bytes:
        download_url = source.resolve_apostila_url(book["nome_arquivo_hash"])

    book_id = db.upsert_book(
        discipline_id=discipline_id,
        external_id=book["external_id"],
        title=book["title"],
        source_url=download_url,
        filename=book["filename"],
        module_code=module_code,
    )

    if have_bytes:
        target = existing["local_path"]
        size = os.path.getsize(target)
        digest = existing.get("sha256") or sha256_file(target)
        log.info("bytes already on disk for %s", book["title"])
        outcome.reused += 1
    else:
        log.info("downloading %s", book["title"])
        size = download_to(download_url, target, max_bytes=max_bytes)
        digest = sha256_file(target)
        outcome.downloaded += 1
        log.info("downloaded %.1f MiB", size / (1024 * 1024))

    # Content dedupe: if these exact bytes were already uploaded for another
    # offering, point at that Drive file instead of uploading a second copy.
    twin = db.find_book_by_sha256(digest, exclude_id=book_id)
    if twin and twin.get("drive_file_id"):
        db.mark_book_stored(book_id, local_path=target, sha256=digest, bytes_=size,
                            drive_file_id=twin["drive_file_id"])
        log.info("identical to an already-uploaded book — reusing its Drive file")
        outcome.reused += 1
        return

    drive_file_id = None
    if drive is not None:
        drive_file_id = drive.upload(
            target, folder_path=folder, name=book["filename"],
            mime_type="application/pdf" if book["filename"].endswith(".pdf") else None,
        )
        outcome.uploaded += 1

    db.mark_book_stored(book_id, local_path=target, sha256=digest, bytes_=size,
                        drive_file_id=drive_file_id)


def _module_from(shortname: str) -> str:
    from ..sources.studeo import module_code_from_discipline_id

    return module_code_from_discipline_id(shortname) or ""


def run_archive_books(db: Database, settings: Settings, *, dry_run: bool = False,
                      discipline: str | None = None, limit: int | None = None) -> int:
    outcome = archive_books(db, settings, dry_run=dry_run,
                            only_discipline=discipline, limit=limit)
    print(outcome)
    return 1 if outcome.failed else 0
