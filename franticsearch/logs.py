"""Logging for the server and the CLI: one handler, on stderr.

Under systemd, stderr goes to the journal (``journalctl --user -u franticsearch``),
which timestamps every line itself; in Docker, to ``docker logs``. The level comes
from ``FS_LOG_LEVEL`` (default INFO). Flask's error log (tracebacks of 500s) and
Werkzeug's request log go through the same handler.
"""

from __future__ import annotations

import logging
import os
import sys

DEFAULT_LEVEL = "INFO"


def configure(level: str | None = None) -> None:
    """Set up the root logger (safe to call again: it replaces the handler)."""
    name = (level or os.environ.get("FS_LOG_LEVEL") or DEFAULT_LEVEL).upper()
    numeric = logging.getLevelName(name)
    bad = not isinstance(numeric, int)
    # The journal adds its own timestamp to every line.
    fmt = "%(levelname)s %(name)s: %(message)s"
    if not os.environ.get("JOURNAL_STREAM"):
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(level=logging.INFO if bad else numeric, format=fmt, stream=sys.stderr, force=True)
    if bad:
        logging.getLogger(__name__).warning("unknown FS_LOG_LEVEL %r, using INFO", name)
