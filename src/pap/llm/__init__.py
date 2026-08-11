"""The vendor-neutral LLM port.

``base.py`` is the interface; ``claude.py`` / ``openai.py`` / ``gemini.py`` are the
adapters, each importing only its own vendor's SDK; ``router.py`` picks one per task
and records the cost. Domain code imports ``router`` (or ``base`` for typing) and
nothing else from here.
"""

from .base import Completion, LLMError, LLMProvider, LLMRefusal

__all__ = ["Completion", "LLMError", "LLMProvider", "LLMRefusal"]
