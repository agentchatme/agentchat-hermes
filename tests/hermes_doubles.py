"""Stand-ins for the Hermes modules the key source is built on.

Hermes is not installed in the unit suite. ``agent.secret_sources.base`` is
stood in by the same shape Hermes ships (``ErrorKind``, ``FetchResult``,
``SecretSource``, ``SECRET_SOURCE_API_VERSION``); the real one is exercised in
``test_hermes_contract.py``.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest


class ErrorKind(str, Enum):
    NOT_CONFIGURED = "not_configured"
    REF_INVALID = "ref_invalid"
    INTERNAL = "internal"


@dataclass
class FetchResult:
    secrets: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    error_kind: ErrorKind | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class SecretSource:
    api_version = 1
    name = ""
    label = ""
    shape = "mapped"
    override_existing_default = False

    def is_enabled(self, cfg: dict[str, Any]) -> bool:
        return bool(isinstance(cfg, dict) and cfg.get("enabled"))

    def override_existing(self, cfg: dict[str, Any]) -> bool:
        return bool(cfg.get("override_existing", self.override_existing_default))


def install_secret_source_base(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    base = types.ModuleType("agent.secret_sources.base")
    base.ErrorKind = ErrorKind  # type: ignore[attr-defined]
    base.FetchResult = FetchResult  # type: ignore[attr-defined]
    base.SecretSource = SecretSource  # type: ignore[attr-defined]
    base.SECRET_SOURCE_API_VERSION = 1  # type: ignore[attr-defined]
    for name in ("agent", "agent.secret_sources"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "agent.secret_sources.base", base)
    return base
