"""Say a thing once per process, then quietly.

A gateway that lost the leader election retries every 15 s; a lock or a
private directory that cannot be had is the same problem each time. The
first occurrence is logged at its level, every later one at DEBUG, keyed by
what the caller names (a path, a path and an error).
"""
from __future__ import annotations

import logging
import threading

_seen: set[str] = set()
_lock = threading.Lock()


def log_once(logger: logging.Logger, key: str, level: int, message: str, *args: object) -> None:
    """``logger.log(level, message, *args)`` the first time for ``key``, DEBUG after."""
    with _lock:
        first = key not in _seen
        _seen.add(key)
    logger.log(level if first else logging.DEBUG, message, *args)
