"""Base class for source adapters.

Subclasses implement ``collect()`` and nothing else. They receive settings and a
throttled HTTP session, and return ``Item``s describing the *current* state of
the source. They do not diff, do not write to the database, and do not notify —
``run_source`` does the first two and the dispatcher does the third.

That constraint is what makes ``--dry-run`` meaningful: a collect-only adapter
can always be run safely to see what it would find.
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..config import Settings
from ..core.http import ThrottledSession
from ..core.models import Item

log = logging.getLogger(__name__)


class BaseSource:
    name: str = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: ThrottledSession | None = None

    @property
    def session(self) -> ThrottledSession:
        """Lazily created so adapters that need no HTTP (or are dry-run) open nothing."""
        if self._session is None:
            self._session = ThrottledSession(self.settings.http)
        return self._session

    @property
    def enabled(self) -> bool:
        """Whether this source has the configuration it needs to run."""
        return self.disabled_reason is None

    @property
    def disabled_reason(self) -> str | None:
        """Why this source cannot run, phrased as the fix — or None when it can.

        The adapter knows exactly which variable is missing; the runner does not.
        Returning the reason from here is what keeps "not configured" from being a
        dead end the caller has to go investigate.
        """
        return None

    def collect(self) -> Iterable[Item]:
        raise NotImplementedError

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
