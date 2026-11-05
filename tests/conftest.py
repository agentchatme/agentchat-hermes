"""Shared test fixtures."""
from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def assert_private_file() -> Callable[[Path], None]:
    """Assert a file the plugin wrote is owner-only (``0600``).

    POSIX only: Windows reports ``0o666`` for every file whatever ``chmod``
    asked for, so the mode bits say nothing there. The production code
    still opens and ``chmod``s with ``0o600`` on every platform; only the
    assertion is conditional (the pattern ``test_ws_health`` uses inline).
    """

    def check(path: Path) -> None:
        assert path.exists(), f"{path} was not written"
        if os.name == "nt":
            return
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"{path}: expected 0600, got {oct(mode)}"

    return check
