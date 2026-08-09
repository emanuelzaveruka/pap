"""Source adapters.

Importing this package is what populates the registry, so every adapter must be
imported here — the CLI looks sources up by name and an unimported module is
invisible to it.
"""

from . import fake, studeo  # noqa: F401  (import for their registration side effect)

__all__ = ["fake", "studeo"]
