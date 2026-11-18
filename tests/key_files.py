"""A profile's AgentChat key file, set up for a test.

Every test runs with ``HERMES_HOME`` pointing at its own temporary directory
(the home guard in ``conftest.py``); these helpers write and read the key
file there through the real :mod:`agentchatme_hermes.key_store`.
"""
from __future__ import annotations

import os
from pathlib import Path

from agentchatme_hermes import key_store

DEFAULT_BASE = "https://api.agentchat.me"


def profile_home() -> Path:
    """The test's profile home (``HERMES_HOME``)."""
    return Path(os.environ["HERMES_HOME"])


def save_key(
    api_key: str, *, handle: str = "alice", home: Path | None = None, api_base: str = DEFAULT_BASE
) -> Path:
    """Save ``api_key`` in the key file of ``home`` (the test's profile by default)."""
    return key_store.write_store(
        home if home is not None else profile_home(), api_key=api_key, handle=handle, api_base=api_base
    )


def saved(home: Path | None = None) -> key_store.StoredKey | None:
    """What the key file of ``home`` holds, or ``None``."""
    read = key_store.read_store(home if home is not None else profile_home())
    return read.key if read.usable else None
