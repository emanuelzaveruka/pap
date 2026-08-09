"""Error tracking and job liveness — the ops domain, deliberately separate from notifications.

This module answers *"what broke, in which application, at which phase, and why"*.
It is not how the platform talks to you about study material: that is
``sinks/dispatcher.py``, a product feature with a different audience. The two
never share state, and an outage in one must not silence the other.

Two tools, covering two genuinely different failure modes:

**GlitchTip** (via the official ``sentry-sdk`` — GlitchTip implements the Sentry
ingest API) receives exceptions with a full stack trace, grouped and deduplicated,
tagged with ``app``/``source``/``phase``/``release``. Because it is protocol
compatible, moving to hosted Sentry later is a DSN change and nothing else.

**Healthchecks** receives a ping at the start and end of every job. This exists
because error tracking is structurally blind to the worst failure mode: a job
that never runs raises no exception, so nothing is ever reported. Only something
watching for *silence* can catch a timer that stopped firing.

Both are optional. With no DSN and no ping key the platform runs exactly as
before, reporting nothing — a missing observability backend must never be the
reason a scrape fails.

Secret scrubbing is enforced here rather than trusted to callers: every string
registered in ``Settings.secret_values`` is replaced with ``[redacted]``
throughout the event payload before it leaves the process.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

import requests

from .config import Settings

log = logging.getLogger(__name__)

REDACTED = "[redacted]"

_secret_values: tuple[str, ...] = ()
_enabled = False
_phase_stack: list[str] = []


# -- scrubbing --------------------------------------------------------------
def _scrub(value: Any) -> Any:
    """Recursively replace known secret values anywhere in an event payload.

    Walks dicts, lists and strings. A secret can surface in surprising places —
    a connection string inside an exception message, a bearer token in a logged
    request URL, an SMTP password in a traceback's local variables — so the whole
    structure is rewritten rather than a known list of fields.
    """
    if isinstance(value, str):
        for secret in _secret_values:
            if secret and secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _before_send(event: dict, hint: dict) -> dict | None:
    try:
        return _scrub(event)
    except Exception:  # noqa: BLE001 - scrubbing must never break error reporting
        # If scrubbing fails we drop the event rather than risk shipping a secret.
        log.warning("could not scrub an error event — dropping it instead of sending it")
        return None


# -- setup ------------------------------------------------------------------
def init_observability(settings: Settings, *, job: str) -> bool:
    """Initialise error reporting. Returns True when events will be sent.

    Safe to call when unconfigured or when ``sentry-sdk`` is not installed —
    it simply reports nothing.
    """
    global _secret_values, _enabled
    _secret_values = settings.secret_values
    _enabled = False

    if not settings.observability.sentry_configured:
        log.debug("SENTRY_DSN is empty — error reporting disabled")
        return False

    try:
        import sentry_sdk
    except ImportError:
        log.warning("sentry-sdk is not installed — error reporting disabled")
        return False

    sentry_sdk.init(
        dsn=settings.observability.sentry_dsn,
        environment=settings.observability.environment,
        release=settings.observability.release,
        # Never attach request/user data automatically: this app handles college
        # credentials and personal course material.
        send_default_pii=False,
        # GlitchTip supports error events fully and tracing only partially, so we
        # send errors only. Raise this if the DSN ever points at hosted Sentry.
        traces_sample_rate=0.0,
        before_send=_before_send,
    )
    sentry_sdk.set_tag("app", "pap")
    sentry_sdk.set_tag("job", job)
    _enabled = True
    log.debug("error reporting enabled (release=%s)", settings.observability.release)
    return True


def set_tag(key: str, value: str | None) -> None:
    if not _enabled or value is None:
        return
    import sentry_sdk

    sentry_sdk.set_tag(key, value)


def set_context(name: str, data: dict) -> None:
    if not _enabled:
        return
    import sentry_sdk

    sentry_sdk.set_context(name, _scrub(data))


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Mark the pipeline stage currently executing.

    Sets the ``phase`` tag for the duration and leaves a breadcrumb on exit, so a
    reported exception says not just *which source* failed but *where in its
    pipeline* — extract, llm, render, upload — which is usually the difference
    between a five-minute fix and an hour of reading logs.
    """
    if not _enabled:
        yield
        return

    import sentry_sdk

    # Track nesting ourselves rather than reading the SDK's scope internals, so a
    # sentry-sdk upgrade can't quietly break phase reporting.
    _phase_stack.append(name)
    sentry_sdk.set_tag("phase", name)
    sentry_sdk.add_breadcrumb(category="phase", message=f"enter {name}", level="info")
    try:
        yield
    finally:
        sentry_sdk.add_breadcrumb(category="phase", message=f"exit {name}", level="info")
        _phase_stack.pop()
        sentry_sdk.set_tag("phase", _phase_stack[-1] if _phase_stack else None)


def capture_exception(exc: BaseException) -> str | None:
    """Report an exception and return the event id, so it can be stored on the run row.

    That id is what turns ``source_run.status='failed'`` from a dead end into a
    direct link to the stack trace in GlitchTip.
    """
    if not _enabled:
        return None
    import sentry_sdk

    try:
        return sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 - reporting a failure must not cause one
        log.warning("could not report the exception to GlitchTip", exc_info=True)
        return None


def flush(timeout: float = 5.0) -> None:
    """Send anything still queued. Required for short-lived processes: the SDK
    reports in a background thread, and a container exiting immediately after an
    exception would otherwise discard the event."""
    if not _enabled:
        return
    import sentry_sdk

    try:
        sentry_sdk.flush(timeout=timeout)
    except Exception:  # noqa: BLE001
        log.debug("flush of pending error events failed", exc_info=True)


# -- Healthchecks -----------------------------------------------------------
class Healthchecks:
    """Dead-man's switch pings for a single job.

    Every call is best-effort with a short timeout: the monitoring system must
    never delay or fail the work it is monitoring. A job that cannot reach
    Healthchecks still does its job; you simply get an alert about silence,
    which is the correct outcome anyway.
    """

    def __init__(self, url: str | None, *, timeout: int = 5) -> None:
        self.url = url
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _ping(self, suffix: str = "", body: str = "") -> None:
        if not self.url:
            return
        try:
            requests.post(f"{self.url}{suffix}", data=body.encode("utf-8")[:10_000],
                          timeout=self.timeout)
        except requests.RequestException as exc:
            log.debug("healthchecks ping %r failed: %s", suffix or "success", exc)

    def start(self) -> None:
        self._ping("/start")

    def success(self, summary: str = "") -> None:
        self._ping("", summary)

    def fail(self, summary: str = "") -> None:
        self._ping("/fail", summary)


def healthchecks_for(settings: Settings, job: str) -> Healthchecks:
    return Healthchecks(settings.observability.ping_url(job))
