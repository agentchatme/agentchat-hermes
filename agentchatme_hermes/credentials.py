"""Saving, moving and clearing this profile's key: one path for the wizard and the CLI.

* :func:`save_credentials` — register / login / recover: the key file first
  (:mod:`.key_store`), read back; only then is the profile ``.env``'s key
  line removed, so there is never a moment with no saved copy. If the key
  file cannot be written — or Hermes could not read it for the hosted tools
  (:func:`key_bridge`) — a key the server just issued is not dropped: it is
  saved in the profile ``.env`` as before and the caller says so.
* :func:`move_env_key` — ``doctor --fix``: the ``.env`` key moves into the
  key file; the ``.env`` line is removed only when it holds exactly the key
  that was moved.
* :func:`clear_credentials` — logout: the key file and the ``.env`` key lines.

Lines are removed with Hermes' own ``remove_env_value`` (its write lock and
cache) when Hermes is importable and the file is the active profile's; else
by :func:`remove_env_lines`, which drops only the named lines and leaves every
other byte of the file as it was.
"""
from __future__ import annotations

import contextlib
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import key_store, profile_scope
from .config import ENV_API_KEY, ENV_API_KEY_LEGACY, ENV_HANDLE
from .profile_scope import dotenv_value

logger = logging.getLogger(__name__)

ENV_KEY_NAMES = (ENV_API_KEY, ENV_API_KEY_LEGACY)


@dataclass(frozen=True)
class SaveOutcome:
    # The key file written; ``None`` when it could not be and the key went to ``.env``.
    path: Path | None
    # ``.env`` lines removed after the key file was written.
    env_lines_removed: tuple[str, ...] = ()
    # Why the key file could not be written (the key is then in ``.env``).
    fallback_reason: str | None = None


def key_bridge() -> tuple[bool, str]:
    """Can Hermes' MCP client read this profile's key file? ``(ready, why not)``.

    The hosted tools authenticate with ``${AGENTCHAT_API_KEY}``, which Hermes
    resolves from the profile's ``.env`` or a secret source. The key may
    leave ``.env`` only when the plugin's source can stand in: Hermes has
    secret sources, and ``secrets.agentchat`` turns it on in ``config.yaml``
    (placed here when missing). Otherwise moving the key would leave the
    tools unauthenticated.
    """
    from .key_source import source_supported
    from .mcp_registration import McpRegistrationError
    from .placement import place_secrets
    from .secrets_registration import inspect_source_enabled

    if not source_supported():
        return False, (
            "this Hermes has no secret sources (agent.secret_sources), so its MCP client could "
            "not read the key file — upgrade Hermes"
        )
    step = place_secrets()
    try:
        enabled = inspect_source_enabled()
    except McpRegistrationError as exc:
        return False, str(exc)
    if enabled:
        return True, ""
    return False, step.detail


def save_credentials(
    *,
    api_key: str,
    handle: str,
    api_base: str,
    home: Path | None = None,
    use_key_file: bool = True,
    not_ready: str = "",
) -> SaveOutcome:
    """Save a key the server accepted (or just issued) for this profile.

    ``use_key_file=False`` (from :func:`key_bridge`): Hermes could not read
    the key file for the hosted tools, so the key goes to the profile
    ``.env`` as before, and ``not_ready`` says why.

    Raises:
        key_store.KeyStoreError: neither the key file nor the ``.env``
            fallback could be written (outside Hermes only).
    """
    home = home if home is not None else profile_scope.resolve_hermes_home()
    if not use_key_file:
        _save_env_fallback(api_key)
        return SaveOutcome(None, (), not_ready or "the hosted tools could not read the key file")
    try:
        path = key_store.write_store(home, api_key=api_key, handle=handle, api_base=api_base)
    except key_store.KeyStoreError as exc:
        _save_env_fallback(api_key)
        logger.warning("credentials: key file not written (%s); key saved in the profile .env", exc)
        return SaveOutcome(None, (), str(exc))
    removed = remove_env_key_lines(home)
    return SaveOutcome(path, removed, None)


def _save_env_fallback(api_key: str) -> None:
    try:
        from hermes_cli.config import save_env_value
    except ImportError as exc:
        raise key_store.KeyStoreError(
            "the key file could not be written and Hermes' .env helper is not importable"
        ) from exc
    save_env_value(ENV_API_KEY, api_key)


@dataclass(frozen=True)
class MoveOutcome:
    moved: bool
    path: Path | None = None
    env_lines_removed: tuple[str, ...] = ()
    detail: str = ""


def env_file_key(home: Path) -> tuple[str, str] | None:
    """``(key, variable name)`` from the profile's own ``.env`` file, or ``None``."""
    env_file = Path(home) / ".env"
    for name in ENV_KEY_NAMES:
        value = (dotenv_value(env_file, name) or "").strip()
        if value:
            return value, name
    return None


def move_env_key(*, handle: str, api_base: str, home: Path | None = None) -> MoveOutcome:
    """Move the profile ``.env``'s key into the key file (``doctor --fix``)."""
    home = home if home is not None else profile_scope.resolve_hermes_home()
    found = env_file_key(home)
    if found is None:
        return MoveOutcome(False, detail="no key in the profile .env")
    api_key, _name = found
    try:
        path = key_store.write_store(home, api_key=api_key, handle=handle, api_base=api_base)
    except key_store.KeyStoreError as exc:
        return MoveOutcome(False, detail=f"the key stays in the profile .env: {exc}")
    removed = remove_env_key_lines(home, only_value=api_key)
    return MoveOutcome(True, path, removed)


def clear_credentials(home: Path | None = None) -> tuple[bool, tuple[str, ...]]:
    """Logout: remove the key file and the ``.env`` key and handle lines.

    Returns ``(key file removed, .env lines removed)``.
    """
    home = home if home is not None else profile_scope.resolve_hermes_home()
    removed_file = key_store.delete_store(home)
    removed_lines = remove_env_key_lines(home, names=(*ENV_KEY_NAMES, ENV_HANDLE))
    return removed_file, removed_lines


def remove_env_key_lines(
    home: Path,
    *,
    only_value: str | None = None,
    names: tuple[str, ...] = ENV_KEY_NAMES,
) -> tuple[str, ...]:
    """Remove ``names`` from the profile's ``.env``; with ``only_value``, only lines holding it.

    Returns the names removed. Never raises: a line that could not be
    removed is logged and the caller's doctor run shows it again.
    """
    env_file = Path(home) / ".env"
    targets = tuple(
        name
        for name in names
        if _defines(env_file, name)
        and (only_value is None or (dotenv_value(env_file, name) or "").strip() == only_value)
    )
    if not targets:
        return ()
    hermes_remove = _hermes_remover(home)
    removed: list[str] = []
    for name in targets:
        try:
            if hermes_remove is not None:
                hermes_remove(name)
            else:
                remove_env_lines(env_file, (name,))
        except Exception:
            logger.warning("credentials: could not remove %s from %s", name, env_file, exc_info=True)
            continue
        if not _defines(env_file, name):
            removed.append(name)
    return tuple(removed)


def _hermes_remover(home: Path) -> Callable[[str], object] | None:
    """Hermes' ``remove_env_value`` when it edits ``home``'s ``.env``, else ``None``."""
    try:
        from hermes_cli.config import get_env_path, remove_env_value
    except Exception:
        return None
    try:
        if not key_store.same_home(Path(get_env_path()).parent, home):
            return None
    except Exception:
        return None
    return remove_env_value if callable(remove_env_value) else None


def _defines(env_file: Path, name: str) -> bool:
    try:
        text = env_file.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    return any(_line_key(line) == name for line in text.splitlines())


def _line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    key, sep, _value = stripped.partition("=")
    return key.strip() if sep else None


def remove_env_lines(env_file: Path, names: tuple[str, ...]) -> bool:
    """Drop every line assigning one of ``names``; keep every other byte.

    Atomic (temporary file + rename) and keeps the file's mode. ``True`` when
    a line was removed.
    """
    try:
        raw = env_file.read_bytes()
        mode = stat.S_IMODE(os.stat(env_file).st_mode)
    except FileNotFoundError:
        return False
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if _line_key(line.lstrip("﻿")) not in names]
    if len(kept) == len(lines):
        return False
    tmp = env_file.with_name(f".{env_file.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
        try:
            os.write(fd, "".join(kept).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, env_file)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return True
