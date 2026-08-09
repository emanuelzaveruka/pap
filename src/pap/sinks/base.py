"""The outbound port: everything that leaves the platform goes through a Sink."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..core.models import Notification, SendResult


@runtime_checkable
class Sink(Protocol):
    """One delivery channel.

    ``send`` reports failure by returning ``SendResult(ok=False, ...)`` rather than
    raising, because a channel being down is an expected operational condition,
    not a bug. The dispatcher records the reason and retries on the next run; an
    exception would abort the whole queue and hold up every other notification.
    """

    name: str

    @property
    def configured(self) -> bool: ...

    def send(self, notification: Notification) -> SendResult: ...
