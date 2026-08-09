"""Logging configuration: console (captured by Docker/journald) + rotating file.

The console handler is the one that matters in production — `docker compose run`
sends stdout to the Docker log driver and, through the systemd unit, on to
journald. The file handler is a convenience for local runs and is best-effort:
a read-only or missing LOG_DIR degrades to console-only rather than failing the
run, because losing a log file is never a reason to abandon a scrape.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(log_dir: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(getattr(logging, level, logging.INFO))
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, "pap.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=7,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError as exc:
        root.warning("Could not open log file in %s: %s", log_dir, exc)

    # These libraries log every request at INFO, which drowns out our own output.
    for noisy in ("urllib3", "googleapiclient.discovery_cache", "google_auth_httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
