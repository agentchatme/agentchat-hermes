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
    warnings: list[str] = field(default_factory=list)
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


# ─── agent.secret_scope and hermes_cli.config's .env helpers ────────────────


class UnscopedSecretError(RuntimeError):
    """Hermes' fail-closed error: a secret read with no scope bound while multiplexing."""


class FakeSecretScope:
    """``agent.secret_scope`` as the plugin uses it.

    ``multiplex`` is the process flag. ``by_home`` is what Hermes would build
    for each profile home (``owner_secret_scope``). ``get_secret`` follows
    Hermes: a bound mapping first (a miss is ``default`` while multiplexing,
    else the process environment); unbound, the process environment, or
    :class:`UnscopedSecretError` while multiplexing.
    """

    def __init__(self, *, multiplex: bool = False, by_home: dict[str, dict[str, str]] | None = None) -> None:
        import contextvars

        self.multiplex = multiplex
        self.by_home = {self._key(h): dict(v) for h, v in (by_home or {}).items()}
        self.bound: contextvars.ContextVar[tuple[dict[str, str], str | None] | None] = contextvars.ContextVar(
            "fake_secret_scope", default=None
        )
        self.binds: list[str | None] = []
        self.refreshed: list[str] = []
        self.resets = 0
        self.fail_set: Exception | None = None
        self.refresh_fills = True

    @staticmethod
    def _key(home: Any) -> str:
        import os

        return os.path.realpath(str(home))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
        module = types.ModuleType("agent.secret_scope")
        module.UnscopedSecretError = UnscopedSecretError  # type: ignore[attr-defined]
        module.is_multiplex_active = lambda: self.multiplex  # type: ignore[attr-defined]
        module.get_secret = self.get_secret  # type: ignore[attr-defined]
        module.set_secret_scope = self.set_secret_scope  # type: ignore[attr-defined]
        module.reset_secret_scope = self.reset_secret_scope  # type: ignore[attr-defined]
        module.refresh_installed_secret_scope = self.refresh  # type: ignore[attr-defined]
        package = sys.modules.get("agent") or types.ModuleType("agent")
        monkeypatch.setitem(sys.modules, "agent", package)
        monkeypatch.setattr(package, "secret_scope", module, raising=False)
        monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
        return module

    def get_secret(self, name: str, default: str | None = None) -> str | None:
        import os

        bound = self.bound.get()
        if bound is not None:
            value = bound[0].get(name)
            if value is not None:
                return value
            return default if self.multiplex else os.environ.get(name, default)
        if self.multiplex:
            raise UnscopedSecretError(name)
        return os.environ.get(name, default)

    def set_secret_scope(self, mapping: dict[str, str], *, profile_home: str | None = None) -> Any:
        if self.fail_set is not None:
            raise self.fail_set
        self.binds.append(profile_home)
        return self.bound.set((mapping, profile_home))

    def reset_secret_scope(self, token: Any) -> None:
        self.resets += 1
        self.bound.reset(token)

    def refresh(self, home: Any) -> bool:
        self.refreshed.append(str(home))
        bound = self.bound.get()
        if bound is None or not self.refresh_fills:
            return False
        bound[0].update(self.by_home.get(self._key(home), {}))
        return True


class FakeHermesEnv:
    """``hermes_cli.config``'s ``.env`` helpers, on an in-memory ``.env`` per profile.

    As Hermes does at start, :meth:`load` puts the active profile's ``.env``
    (``HERMES_HOME``) into the process environment over what was exported;
    ``remove_env_value`` removes every line of a name from the active
    profile's file, clears the variable from the environment and says
    whether a line was there; ``save_env_value`` writes it to both. A
    ``managed`` profile refuses writes (``require_env_writable`` raises,
    ``save_env_value`` / ``remove_env_value`` do nothing). The plugin never
    opens a ``.env`` file, so none exists on disk.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, managed: bool = False) -> None:
        self.monkeypatch = monkeypatch
        self.files: dict[str, dict[str, str]] = {}
        self.managed = managed
        self.removed: list[str] = []
        self.saved: list[tuple[str, str]] = []

    @staticmethod
    def _active() -> Any:
        import os
        from pathlib import Path

        return Path(os.environ["HERMES_HOME"])

    def file(self, home: Any) -> dict[str, str]:
        import os

        return self.files.setdefault(os.path.realpath(str(home)), {})

    def write(self, home: Any, **values: str) -> None:
        """Lines in ``home``'s ``.env`` (as a user or an earlier release left them)."""
        self.file(home).update(values)

    def load(self) -> None:
        """Hermes' start-up load of the active profile's ``.env`` into the environment."""
        for name, value in self.file(self._active()).items():
            self.monkeypatch.setenv(name, value)

    def install(self) -> types.ModuleType:
        module = types.ModuleType("hermes_cli.config")
        module.get_env_path = lambda: self._active() / ".env"  # type: ignore[attr-defined]
        module.remove_env_value = self.remove_env_value  # type: ignore[attr-defined]
        module.save_env_value = self.save_env_value  # type: ignore[attr-defined]
        module.require_env_writable = self.require_env_writable  # type: ignore[attr-defined]
        package = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
        self.monkeypatch.setitem(sys.modules, "hermes_cli", package)
        self.monkeypatch.setattr(package, "config", module, raising=False)
        self.monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
        return module

    def require_env_writable(self, key: str, action: str) -> None:
        if self.managed:
            raise ValueError(f"{key} is managed: {action} refused")

    def remove_env_value(self, key: str) -> bool:
        self.removed.append(key)
        if self.managed:
            return False
        found = self.file(self._active()).pop(key, None) is not None
        self.monkeypatch.delenv(key, raising=False)
        return found

    def save_env_value(self, key: str, value: str) -> None:
        self.saved.append((key, value))
        if self.managed:
            return
        self.file(self._active())[key] = value
        self.monkeypatch.setenv(key, value)
