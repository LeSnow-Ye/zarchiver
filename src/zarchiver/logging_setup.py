"""Logging setup for zarchiver.

zarchiver uses the standard :mod:`logging` library throughout. Modules get a
logger via ``logging.getLogger(__name__)`` and never configure handlers
themselves; the CLI calls :func:`setup_logging` once to install a single
:class:`~rich.logging.RichHandler` on the package logger.

Verbosity is a single knob:

* default (``0``)   → INFO: one line per archived item plus high-level steps.
* ``-v`` (``1``)    → DEBUG: per-page fetches, parse results, image counts, etc.
* ``-vv`` (``2``)   → DEBUG for zarchiver *and* noisy third-party libraries.
* ``--quiet``       → WARNING and above only.
"""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

#: Root logger for the whole package. Child loggers (``zarchiver.*``) inherit
#: its handler and level, so configuring this one configures everything.
ROOT_LOGGER_NAME = "zarchiver"

_configured = False


def setup_logging(verbosity: int = 0, *, quiet: bool = False,
                  console: Console | None = None) -> logging.Logger:
    """Install a Rich log handler on the package logger and set its level.

    Idempotent: safe to call more than once (later calls just adjust the level).

    Args:
        verbosity: 0 = INFO, 1+ = DEBUG. 2+ also enables DEBUG for third-party
            libraries (Playwright, httpx, urllib3).
        quiet: If True, only WARNING and above are shown (overrides verbosity).
        console: Rich console to log through; a default is created if omitted.
    """
    global _configured

    if quiet:
        level = logging.WARNING
    elif verbosity >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if not _configured:
        handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            omit_repeated_times=False,
            rich_tracebacks=True,
            markup=False,
        )
        handler.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        _configured = True
    else:
        for h in logger.handlers:
            h.setLevel(level)

    # Third-party libraries are very chatty; only surface them at -vv.
    third_party_level = logging.DEBUG if verbosity >= 2 else logging.WARNING
    for name in ("playwright", "httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(third_party_level)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper around :func:`logging.getLogger`."""
    return logging.getLogger(name)
