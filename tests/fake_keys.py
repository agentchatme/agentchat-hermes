"""API-key-shaped test values, built at run time.

A literal ``ac_live_…`` string in the repository looks like a leaked key to
secret scanners (GitHub's included), so tests never write one: they call
:func:`api_key` with the part after the prefix.
"""
from __future__ import annotations

PREFIX = "_".join(("ac", "live")) + "_"
"""The live-key prefix, assembled so the source never holds it whole."""


def api_key(body: str = "test") -> str:
    """``api_key("abc")`` is the prefix followed by ``abc``."""
    return PREFIX + body
