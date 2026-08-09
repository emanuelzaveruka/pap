"""Throttled, retrying HTTP session shared by every scraper.

Carried over from the Toyama scraper's approach, which encodes the rule this
platform follows: **prefer a JSON API over parsing HTML, and parse HTML over
driving a browser.** A site that publishes JSON is both easier to consume and far
less likely to break on a redesign.

Two behaviours are deliberate:

*Throttling is per session and unconditional.* Every request waits
``HTTP_REQUEST_DELAY_SECONDS`` after the previous one. This is a personal tool
polling sites that owe it nothing — being slow is the price of being welcome, and
the Akita backfill in particular walks years of archive in one run.

*Retries cover transport and 5xx only.* A 4xx means the request was wrong and
retrying just repeats it; 429 is honoured via ``Retry-After`` rather than
hammered, because retrying a rate limit is what turns throttling into a ban.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from ..config import HttpSettings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


class HttpError(RuntimeError):
    """A request failed and is not worth retrying."""


class RateLimited(HttpError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` in its delta-seconds form. HTTP-date form is ignored —
    servers that use it are rare, and guessing wrong is worse than falling back
    to the configured backoff."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


class ThrottledSession:
    """A ``requests.Session`` that paces itself and retries transient failures."""

    def __init__(self, settings: HttpSettings, *, headers: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers["User-Agent"] = settings.user_agent
        if headers:
            self.session.headers.update(headers)
        self._last_request_at = 0.0

    def _wait_turn(self) -> None:
        delay = self.settings.request_delay_seconds
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.settings.timeout_seconds)
        attempts = max(1, self.settings.retry_max_attempts)
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            self._wait_turn()
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s %s failed (%s) — attempt %d/%d", method, url, exc, attempt, attempts)
            else:
                self._last_request_at = time.monotonic()
                if response.status_code == 429:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    raise RateLimited(
                        f"{method} {url} was rate limited (429)", retry_after=retry_after
                    )
                if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                    log.warning("%s %s returned %d — attempt %d/%d", method, url,
                                response.status_code, attempt, attempts)
                else:
                    return response
            self._last_request_at = time.monotonic()
            if attempt < attempts:
                time.sleep(self.settings.retry_backoff_seconds * attempt)

        raise HttpError(f"{method} {url} failed after {attempts} attempts") from last_exc

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.get(url, **kwargs)
        # 206 is accepted alongside 200: paginated catalogue APIs answer partial
        # content for a range request and it is a perfectly good page of results.
        if response.status_code not in (200, 206):
            raise HttpError(f"GET {url} returned {response.status_code}")
        return response.json()

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "ThrottledSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
