"""Structured logging for pyfreeswitch.

Provides a consistent logger factory so all library internals use the same
format and level. Library consumers can reconfigure by adjusting the
``pyfreeswitch`` logger in standard logging.
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_configured = False


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under the ``pyfreeswitch`` hierarchy.

    Args:
        name: Subcomponent name (e.g. ``"clients.esl"``). Prefixed with
            ``pyfreeswitch.``.
    """
    _ensure_configured()
    return logging.getLogger(f"pyfreeswitch.{name}")


def _ensure_configured() -> None:
    """Attach a default handler to the root ``pyfreeswitch`` logger (once)."""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("pyfreeswitch")
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
        root.setLevel(logging.WARNING)
