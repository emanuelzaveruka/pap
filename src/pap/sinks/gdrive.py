"""Google Drive sink — generated deliverables, book PDFs, resumes, and DB backups.

Folder paths mirror Studeo's own structure so a file is findable from a module
code alone::

    Studeo/2026/52-2026/<disciplina>/{materiais,livros,entregas,resumos}/
    _backups/pap/

Resolved folder ids are cached per process. Drive has no "get folder by path"
call — each level costs a query — so without caching a run that uploads twenty
files into one folder would re-walk the whole path twenty times.

Uploads are resumable, which matters here: book PDFs are large and this runs on a
home connection where a mid-upload drop is a normal Tuesday.

Google client libraries are imported lazily so this module can be imported (and
the rest of the platform tested) without them installed.
"""

from __future__ import annotations

import logging
import os

from ..config import GoogleSettings
from .google_auth import GoogleAuthError, load_credentials

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"


def _escape(name: str) -> str:
    """Escape a name for a Drive query string literal.

    Disciplines routinely contain apostrophes ("Língua Portuguesa I - Prática"),
    and an unescaped one turns the query into a syntax error.
    """
    return name.replace("\\", "\\\\").replace("'", "\\'")


class DriveSink:
    name = "gdrive"

    def __init__(self, settings: GoogleSettings, *, state_dir: str | None = None) -> None:
        self.settings = settings
        # Where the short-lived access token is cached between runs. Optional:
        # without it every run performs one extra token refresh, which works fine.
        self.state_dir = state_dir
        self._service = None
        self._folder_cache: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return self.settings.configured

    # -- service ------------------------------------------------------------
    @property
    def service(self):
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover
                raise GoogleAuthError("google-api-python-client is not installed") from exc
            credentials = load_credentials(self.settings, state_dir=self.state_dir)
            # cache_discovery=False silences a noisy warning and avoids writing a
            # discovery cache into a read-only container filesystem.
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    # -- folders ------------------------------------------------------------
    def ensure_folder(self, path: str) -> str:
        """Resolve (creating as needed) a slash-separated path, returning its id."""
        path = path.strip("/")
        if not path:
            raise ValueError("folder path must not be empty")
        if path in self._folder_cache:
            return self._folder_cache[path]

        parent = "root"
        walked: list[str] = []
        for part in path.split("/"):
            walked.append(part)
            key = "/".join(walked)
            cached = self._folder_cache.get(key)
            if cached:
                parent = cached
                continue
            parent = self._find_or_create_folder(part, parent)
            self._folder_cache[key] = parent
        return parent

    def _find_or_create_folder(self, name: str, parent_id: str) -> str:
        query = (
            f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        response = self.service.files().list(
            q=query, spaces="drive", fields="files(id, name)", pageSize=1
        ).execute()
        files = response.get("files", [])
        if files:
            return files[0]["id"]

        created = self.service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id",
        ).execute()
        log.info("created Drive folder %s under %s", name, parent_id)
        return created["id"]

    # -- files --------------------------------------------------------------
    def upload(
        self,
        local_path: str,
        *,
        folder_path: str,
        name: str | None = None,
        mime_type: str | None = None,
        replace_existing: bool = True,
    ) -> str:
        """Upload a file and return its Drive id.

        With ``replace_existing`` a same-named file in the same folder is updated
        in place rather than duplicated — Drive happily keeps several files with
        identical names, which would silently turn a re-run into clutter.
        """
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:  # pragma: no cover
            raise GoogleAuthError("google-api-python-client is not installed") from exc

        if not os.path.exists(local_path):
            raise FileNotFoundError(local_path)

        name = name or os.path.basename(local_path)
        folder_id = self.ensure_folder(folder_path)
        media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)

        if replace_existing:
            existing = self._find_file(name, folder_id)
            if existing:
                updated = self.service.files().update(
                    fileId=existing, media_body=media, fields="id"
                ).execute()
                log.info("updated %s in Drive (%s)", name, updated["id"])
                return updated["id"]

        created = self.service.files().create(
            body={"name": name, "parents": [folder_id]}, media_body=media, fields="id"
        ).execute()
        log.info("uploaded %s to Drive (%s)", name, created["id"])
        return created["id"]

    def _find_file(self, name: str, parent_id: str) -> str | None:
        query = (
            f"name = '{_escape(name)}' and '{parent_id}' in parents and trashed = false"
        )
        response = self.service.files().list(
            q=query, spaces="drive", fields="files(id)", pageSize=1
        ).execute()
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def list_folder(self, folder_path: str, *, page_size: int = 200) -> list[dict]:
        """Files in a folder, newest first. Used by backup retention."""
        folder_id = self.ensure_folder(folder_path)
        response = self.service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="files(id, name, createdTime, size)",
            orderBy="createdTime desc",
            pageSize=page_size,
        ).execute()
        return response.get("files", [])

    def delete(self, file_id: str) -> None:
        self.service.files().delete(fileId=file_id).execute()


def studeo_folder_path(
    root: str, *, year: int | None, module_folder: str, discipline: str, bucket: str
) -> str:
    """Build the canonical Drive path for a piece of course material.

    ``bucket`` is one of materiais / livros / entregas / resumos. A module whose
    code could not be parsed lands under ``sem-ano`` rather than being dropped —
    misfiled is recoverable, missing is not.
    """
    safe_discipline = discipline.replace("/", "-").strip() or "sem-disciplina"
    return f"{root}/{year if year else 'sem-ano'}/{module_folder}/{safe_discipline}/{bucket}"
