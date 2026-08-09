"""On-disk credential store: 0600 files, atomic writes, never fatal.

Ported from ``etl_metas/src/metas_sync/token_cache.py`` and generalised past
Salesforce. Two kinds of credential live here, both of which must survive
between runs because every job is a short-lived container:

``google_token.json``  the OAuth refresh token for Drive and Calendar. The server
                       is headless, so consent happens once on a machine with a
                       browser and the resulting token is copied here. If it were
                       lost on each run there would be no way to re-consent
                       without a display.
``studeo_session.json`` the Studeo session/JWT, reused until it is rejected. A
                       fresh login per run would be a login every few minutes
                       against the college's auth endpoint — the kind of pattern
                       that gets an account flagged.

Everything is best-effort by design: a missing, corrupt or foreign file is logged
and ignored, never raised. A broken cache must degrade to "authenticate again",
because losing a cache is not a reason to abandon the work.

Files are written atomically (temp file + ``os.replace``) so a reader never sees
a half-written credential, and chmod'd 0600 *before* the rename so there is no
window in which the file exists with looser permissions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def account_fingerprint(*parts: str) -> str:
    """Short digest identifying which account a stored credential belongs to.

    Guards against reusing the wrong credential after a config change — a token
    saved for one Google account or Studeo login is never handed to another.
    """
    material = "|".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def write_atomic(path: str, payload: dict) -> None:
    """Write JSON 0600 and atomically replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=_DIR_MODE, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class StoredSecret:
    data: dict[str, Any]
    issued_at: float
    fingerprint: str

    def age_seconds(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.issued_at)


class SecretStore:
    """One named credential file under the state directory.

    No method raises because of I/O: the worst outcome of a broken store is an
    extra authentication, which is always recoverable.
    """

    def __init__(
        self,
        state_dir: str,
        name: str,
        *,
        fingerprint: str = "",
        max_age_seconds: int | None = None,
        enabled: bool = True,
    ) -> None:
        self.state_dir = state_dir
        self.name = name
        self.fingerprint = fingerprint
        self.max_age_seconds = max_age_seconds
        self.enabled = enabled

    @property
    def path(self) -> str:
        return os.path.join(self.state_dir, f"{self.name}.json")

    @property
    def cooldown_path(self) -> str:
        return os.path.join(self.state_dir, f"{self.name}_cooldown.json")

    # -- secret -------------------------------------------------------------
    def load(self, now: float | None = None) -> StoredSecret | None:
        """The reusable credential, or None (absent, unreadable, foreign, too old)."""
        if not self.enabled:
            return None
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
            secret = StoredSecret(
                data=raw["data"],
                issued_at=float(raw["issued_at"]),
                fingerprint=raw.get("fingerprint", ""),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("%s is unreadable (%s) — will authenticate again", self.name, exc)
            return None

        if self.fingerprint and secret.fingerprint != self.fingerprint:
            log.info("%s belongs to another account — ignoring it", self.name)
            return None
        if self.max_age_seconds is not None:
            age = secret.age_seconds(now)
            if age >= self.max_age_seconds:
                log.info("%s expired (age %s) — will authenticate again",
                         self.name, human_duration(age))
                return None
        return secret

    def save(self, data: dict[str, Any], now: float | None = None) -> None:
        if not self.enabled:
            return
        try:
            write_atomic(self.path, {
                "data": data,
                "issued_at": now if now is not None else time.time(),
                "fingerprint": self.fingerprint,
            })
        except OSError as exc:
            log.warning("could not write %s (%s) — continuing without it", self.name, exc)

    def invalidate(self) -> None:
        """Forget the credential (the remote side rejected it)."""
        _unlink(self.path)

    # -- cooldown -----------------------------------------------------------
    def cooldown_until(self, now: float | None = None) -> float | None:
        """Epoch until which this service must not be called, or None.

        Set after a rate-limit response. Retrying a throttled endpoint is what
        escalates throttling into a block, so the correct response is to stop
        calling entirely until it expires.
        """
        try:
            with open(self.cooldown_path, encoding="utf-8") as fh:
                until = float(json.load(fh)["until"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("%s cooldown marker unreadable (%s) — ignoring it", self.name, exc)
            return None
        return None if until <= (now if now is not None else time.time()) else until

    def set_cooldown(self, seconds: float, reason: str, now: float | None = None) -> float:
        base = now if now is not None else time.time()
        until = base + max(0.0, seconds)
        try:
            write_atomic(self.cooldown_path, {"until": until, "reason": reason[:500]})
        except OSError as exc:
            log.warning("could not write the %s cooldown marker (%s)", self.name, exc)
        return until

    def clear_cooldown(self) -> None:
        _unlink(self.cooldown_path)

    # -- ops reporting ------------------------------------------------------
    def describe(self, now: float | None = None) -> str:
        """Human-readable state for ``pap auth --status``. Never prints the secret."""
        now = now if now is not None else time.time()
        lines = [
            f"name      : {self.name}",
            f"file      : {self.path}",
            f"cache     : {'enabled' if self.enabled else 'DISABLED'}",
        ]
        secret = self.load(now)
        if secret is None:
            exists = os.path.exists(self.path)
            lines.append(
                f"stored    : none reusable{' (file present but rejected)' if exists else ''}"
                " — the next run will authenticate"
            )
        else:
            lines.append(f"stored    : present, age {human_duration(secret.age_seconds(now))}")
        until = self.cooldown_until(now)
        if until is None:
            lines.append("cooldown  : none")
        else:
            lines.append(
                f"cooldown  : ACTIVE for another {human_duration(until - now)} "
                f"(until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until))})"
            )
        return "\n".join(lines)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("could not remove %s (%s)", path, exc)


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
