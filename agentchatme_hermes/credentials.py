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
    # The key file written; ``None`` when the key went to ``.env`` instead.
    path: Path | None
    # ``.env`` lines removed after the key file was written.
    env_lines_removed: tuple[str, ...] = ()
    # Why the key is not where it should be: in ``.env`` (``path`` is
    # ``None``), or in the key file although Hermes cannot read it for the
    # hosted tools yet (``tools_need_block``).
    fallback_reason: str | None = None
    # The key is in the key file, but ``secrets.agentchat`` must be added (by
    # hand: a managed config) before the hosted tools can read it.
    tools_need_block: bool = False


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
    """Save a key the server accepted (or just issued) for this profile. Never drops it.

    * The key file, when Hermes can read it for the hosted tools
      (``use_key_file``, from :func:`key_bridge`): written and read back, then
      any key line in the profile ``.env`` is removed.
    * Otherwise, or when the key file cannot be written: the profile ``.env``
      as before, checked by reading it back (Hermes refuses ``.env`` writes on
      a managed install without raising), and the old key file — another
      key now — is removed so the two cannot disagree.
    * When ``.env`` refuses the write too: the key file after all (a key in a
      file the tools cannot read yet beats a key saved nowhere), and the
      caller says ``secrets.agentchat`` must be added by hand.

    Raises:
        key_store.KeyStoreError: the key could be saved nowhere.
    """
    home = home if home is not None else profile_scope.resolve_hermes_home()
    reason = not_ready or "the hosted tools could not read the key file"
    if use_key_file:
        try:
            path = key_store.write_store(home, api_key=api_key, handle=handle, api_base=api_base)
        except key_store.KeyStoreError as exc:
            reason = str(exc)
        else:
            return SaveOutcome(path, remove_env_key_lines(home), None)
    if _save_env_fallback(api_key, home):
        try:
            key_store.delete_store(home)
        except key_store.KeyStoreError as exc:
            reason = f"{reason}; the old key file could not be removed ({exc}) — remove it by hand"
        logger.warning("credentials: key saved in the profile .env (%s)", reason)
        return SaveOutcome(None, (), reason)
    try:
        path = key_store.write_store(home, api_key=api_key, handle=handle, api_base=api_base)
    except key_store.KeyStoreError as exc:
        raise key_store.KeyStoreError(
            f"the key could be saved neither in the profile .env nor in the key file ({exc})"
        ) from exc
    return SaveOutcome(
        path,
        remove_env_key_lines(home),
        f"the profile .env could not be written, so the key is in the key file, but {reason}",
        tools_need_block=True,
    )


def _save_env_fallback(api_key: str, home: Path) -> bool:
    """Write the key to the profile ``.env`` with Hermes' writer; ``True`` when it is there."""
    try:
        from hermes_cli.config import save_env_value
    except ImportError:
        return False
    try:
        save_env_value(ENV_API_KEY, api_key)
    except Exception:
        logger.warning("credentials: Hermes refused the .env write", exc_info=True)
        return False
    return env_values(Path(home) / ".env", ENV_API_KEY) == [api_key]


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


def env_values(env_file: Path, name: str) -> list[str]:
    """Every value ``name`` is given in the dotenv file (a name may repeat), in order."""
    try:
        text = env_file.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    values: list[str] = []
    for line in text.splitlines():
        if _line_key(line) != name:
            continue
        values.append(_line_value(line))
    return values


def _line_value(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    value = stripped.partition("=")[2].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value.split(" #", 1)[0].strip()


def move_env_key(*, handle: str, api_base: str, home: Path | None = None) -> MoveOutcome:
    """Move the profile ``.env``'s key into the key file (``doctor --fix``).

    Moves nothing when it is not certain which key is this profile's: the
    ``.env`` gives it several values (both names, or one name twice), or
    another profile on this machine holds the same key — moving it would
    make this profile that agent and the other one a refused copy.
    """
    from .config import cli_prefix
    from .profile_scope import hermes_root_of, profile_label

    home = home if home is not None else profile_scope.resolve_hermes_home()
    found = env_file_key(home)
    if found is None:
        return MoveOutcome(False, detail="no key in the profile .env")
    api_key, _name = found
    env_file = Path(home) / ".env"
    values = {v.strip() for name in ENV_KEY_NAMES for v in env_values(env_file, name) if v.strip()}
    if len(values) > 1:
        return MoveOutcome(
            False,
            detail=(
                "the profile .env holds more than one AgentChat key (AGENTCHAT_API_KEY and "
                "AGENTCHATME_API_KEY, or a name given twice): remove the wrong lines first"
            ),
        )
    holders = key_store.other_holders(home, api_key, root=hermes_root_of(home), env_names=ENV_KEY_NAMES)
    if holders:
        others = ", ".join(f"profile {profile_label(h.home)}, in its {h.where}" for h in holders)
        return MoveOutcome(
            False,
            detail=(
                f"another profile holds the same key ({others}): one key is one agent, and moving "
                "it would make this profile that agent. Run `"
                + "` / `".join(f"{cli_prefix(h.home)} logout" for h in holders)
                + "` in the profile that should not be it, then run this again"
            ),
        )
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
    # With ``only_value``, a name goes only when every line of it holds that
    # value: Hermes' remover takes every line of the name at once.
    targets = tuple(
        name
        for name in names
        if _defines(env_file, name)
        and (only_value is None or all(v.strip() == only_value for v in env_values(env_file, name)))
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
    kept = [line for line in lines if _line_key(line.lstrip("\ufeff")) not in names]
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
