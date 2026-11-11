"""API-key-shaped test values, built at run time.

A real key is ``ac_`` followed by 32 random bytes as unpadded base64url
(43 characters). Written out in the repository, one looks like a leaked key
to secret scanners (gitleaks' AgentChat rule in ``.gitleaks.toml``, GitHub's
own), so tests never hold one as a literal: :func:`api_key` builds a short
stand-in from a readable body, :func:`real_shaped_key` a full-shape key from
fresh random bytes.
"""
from __future__ import annotations

import base64
import secrets

PREFIX = "ac" + "_"
"""The key prefix, assembled so the source never holds a key-shaped run."""


def api_key(body: str = "test") -> str:
    """``api_key("abc")`` is the prefix followed by ``abc``."""
    return PREFIX + body


def real_shaped_key() -> str:
    """A key of the real shape: the prefix and 43 base64url characters."""
    return PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
