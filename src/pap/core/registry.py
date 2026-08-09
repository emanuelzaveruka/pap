"""Source adapter contract and registry.

Adding a source is meant to cost one file: implement ``collect()``, decorate the
class with ``@register``, import it in ``sources/__init__.py``. Nothing else in
the platform needs to learn the new name — the CLI, the dispatcher and the
monitor all work from this registry.

If a new source ever needs changes outside its own module, the abstraction is
wrong and that is worth knowing before more sources are added.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from .models import Item


@runtime_checkable
class SourceAdapter(Protocol):
    """What every source must provide.

    ``collect`` returns the *current* state of the source. It must not decide what
    is new, must not send anything, and must not write to the database — the
    caller diffs against stored state and the dispatcher does the telling. Keeping
    adapters side-effect free is what makes ``--dry-run`` honest.
    """

    name: str

    def collect(self) -> Iterable[Item]: ...


_ADAPTERS: dict[str, type] = {}


def register(cls: type) -> type:
    """Class decorator recording an adapter under its ``name`` attribute."""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls.__name__} must define a class-level `name` to be registered")
    if name in _ADAPTERS and _ADAPTERS[name] is not cls:
        raise ValueError(f"two adapters both claim the name {name!r}")
    _ADAPTERS[name] = cls
    return cls


def available() -> list[str]:
    return sorted(_ADAPTERS)


def get(name: str) -> type:
    try:
        return _ADAPTERS[name]
    except KeyError:
        known = ", ".join(available()) or "none registered"
        raise KeyError(f"unknown source {name!r} (known: {known})") from None
