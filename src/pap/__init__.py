"""Personal automation platform.

Sources are scraped into a local PostgreSQL base; a dispatcher turns genuinely
new items into notifications; domain services generate documents and study
resumes through a vendor-neutral LLM port. See CLAUDE.md for the layout and the
plan behind it.
"""

__version__ = "0.1.0"
