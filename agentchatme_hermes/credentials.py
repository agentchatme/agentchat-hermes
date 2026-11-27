"""Saving, moving and clearing this profile's key: one path for the wizard and the CLI.

* :func:`save_credentials` — register / login / recover: the key file first
  (:mod:`.key_store`), read back; only then are the key settings removed from
  the profile ``.env``, so there is never a moment with no saved copy. If the
  key file cannot be written — or Hermes could not read it for the hosted
  tools (:func:`key_bridge`) — a key the server just issued is not dropped: it
  is saved in the profile ``.env`` with Hermes' own writer and the caller says so.
* :func:`move_env_key` — ``doctor --fix``: a key found in this profile's
  settings moves into the key file, and its ``.env`` line is removed.
* :func:`adopt_legacy_key` — activation: a key found only under the pre-v3
  name ``AGENTCHATME_API_KEY`` is copied into the key file once the server
  has accepted it (the hosted tools read ``AGENTCHAT_API_KEY``); the ``.env``
  line stays for ``doctor --fix``.
* :func:`clear_credentials` — logout: the key file and the key settings.
* :func:`key_in_use_line` — after a save: whether a restart is needed for the
  key to be used (only when it replaced another), said to the person.

This module never opens a ``.env`` file. The key is read from the profile's
settings as Hermes resolves them (:func:`.config.settings_key`); lines are
removed and written only with Hermes' ``remove_env_value`` /
``save_env_value``, and only for the profile the process runs for
(``get_env_path()`` in this profile's home). Hermes loads the profile ``.env``
over the shell's exports, so a key setting that ``remove_env_value`` finds in
no ``.env`` line came from the environment (a shell export, a service's
``Environment=``).

Moving a key is a check (no other profile's key file holds it) and a write;
both happen under the per-account ``adopt`` lock (:func:`.leader_lock.held_lock`
on ``<hermes root>/agentchat-locks/adopt-<fingerprint>.lock``), so two
profiles holding the same key — a ``.env`` copied by ``hermes profile
create`` — cannot both take it, even at one multiplexed gateway's boot: the
second finds the first's key file and is refused as its copy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from . import key_store, profile_scope
from .config import ENV_API_KEY, ENV_API_KEY_LEGACY, ENV_HANDLE

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from .leader_lock import HeldLock

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


RESTART_HINT = (
    "Restart the gateway (`hermes gateway restart`) and any open Hermes session so they use the "
    "key: a running Hermes keeps the key it started with for the AgentChat tools."
)
# In an agent's own terminal the restart is the person's: a gateway restart
# stops the live turns of every agent that gateway serves.
RESTART_HINT_AGENT = (
    "A running gateway and any open Hermes session keep the earlier key for the AgentChat tools until they "
    "restart. That restart is for the person who runs them, not for an agent: a gateway restart stops every "
    "agent the gateway serves."
)
NO_RESTART_LINE = (
    "A running gateway that serves this profile starts AgentChat for it by itself within about a minute: "
    "no restart needed."
)


def key_held(home: Path | None = None) -> str | None:
    """The key this profile holds before a save, refused or not (``""`` for a
    refused copy whose key is not known), or ``None`` when it holds none."""
    from .config import resolve_key

    resolution = resolve_key(home)
    held = resolution.api_key or resolution.held_key
    if held is None and resolution.refusal is not None:
        return ""
    return held


def key_in_use_line(before: str | None, api_key: str) -> str:
    """How a key just saved comes into use (``before``: :func:`key_held` before
    the save). A profile's first key, or the same key again, needs no restart:
    a running gateway activates it by itself (its activation re-checks every
    30 s) and connects the hosted tools with it (Hermes fetches the key source
    for a profile it had nothing for). A key that replaces another needs one: a
    running Hermes keeps the key it started with. In an agent's own terminal
    (:func:`.allow.in_agent_terminal`) that restart is said to be the
    person's, never the agent's."""
    from .allow import in_agent_terminal

    if before is None or before == api_key:
        return NO_RESTART_LINE
    return RESTART_HINT_AGENT if in_agent_terminal() else RESTART_HINT


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
    except Exception as exc:  # Hermes' reader raised: an unreadable or invalid config.yaml
        return False, f"Hermes could not read this profile's config.yaml ({type(exc).__name__}: {exc})"
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
      the key settings are removed from the profile ``.env``.
    * Otherwise, or when the key file cannot be written: the profile ``.env``
      with Hermes' writer, after Hermes' write lock allows it (a managed
      install refuses ``.env`` writes without raising), and the old key file
      — another key now — is removed so the two cannot disagree.
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
            return SaveOutcome(path, remove_key_settings(home), None)
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
        remove_key_settings(home),
        f"the profile .env could not be written, so the key is in the key file, but {reason}",
        tools_need_block=True,
    )


def _save_env_fallback(api_key: str, home: Path) -> bool:
    """Write the key to this profile's ``.env`` with Hermes' writer; ``True`` when it is there.

    Only for the profile the process runs for, and only when Hermes'
    ``.env`` write lock allows the name (``require_env_writable``: a managed
    install refuses, and ``save_env_value`` would refuse without raising).
    """
    try:
        from hermes_cli.config import require_env_writable, save_env_value
    except ImportError:
        return False
    if not _edits_this_profile(home):
        return False
    try:
        require_env_writable(ENV_API_KEY, "set")
        save_env_value(ENV_API_KEY, api_key)
    except Exception:
        logger.warning("credentials: Hermes refused the .env write", exc_info=True)
        return False
    return True


@dataclass(frozen=True)
class MoveOutcome:
    moved: bool
    path: Path | None = None
    env_lines_removed: tuple[str, ...] = ()
    detail: str = ""


def move_env_key(
    *, handle: str, api_base: str, home: Path | None = None, lock_dir: Path | None = None
) -> MoveOutcome:
    """Move the key in this profile's settings into the key file (``doctor --fix``).

    Moves nothing when it is not certain which key is this profile's: the
    two names hold different keys, or another profile's key file holds the
    same key (moving it would make this profile that agent). When no
    ``.env`` line held an ``AGENTCHAT_API_KEY`` key it came from the
    environment: the key-file write is undone and that is said (a key the
    shell or the service sets keeps working from there). A key found only
    under the pre-v3 name stays in the key file either way: the hosted tools
    read ``AGENTCHAT_API_KEY``, which only the key file supplies.
    """
    from .config import cli_prefix

    home = home if home is not None else profile_scope.resolve_hermes_home()
    cmd = cli_prefix(home)
    canonical = (profile_scope.resolve_setting(ENV_API_KEY) or "").strip()
    legacy = (profile_scope.resolve_setting(ENV_API_KEY_LEGACY) or "").strip()
    if canonical and legacy and canonical != legacy:
        return MoveOutcome(
            False,
            detail=(
                f"this profile's settings hold two different AgentChat keys ({ENV_API_KEY} and "
                f"{ENV_API_KEY_LEGACY}): remove the wrong one first"
            ),
        )
    api_key = canonical or legacy
    if not api_key:
        return MoveOutcome(False, detail="no key in this profile's settings")
    remover = _hermes_remover(home)
    if remover is None:
        return MoveOutcome(
            False,
            detail=f"Hermes cannot edit this profile's .env from this process — run `{cmd} doctor --fix`",
        )
    with _key_lock(home, api_base=api_base, api_key=api_key, lock_dir=lock_dir) as lock:
        if lock == "busy":
            return MoveOutcome(False, detail="another profile is saving the same key right now — run this again")
        refused = _claimed_elsewhere(home, api_key)
        if refused is not None:
            return MoveOutcome(False, detail=refused)
        try:
            path = key_store.write_store(home, api_key=api_key, handle=handle, api_base=api_base)
        except key_store.KeyStoreError as exc:
            return MoveOutcome(False, detail=f"the key stays where it is: {exc}")
    names = tuple(name for name, value in ((ENV_API_KEY, canonical), (ENV_API_KEY_LEGACY, legacy)) if value)
    removed = remove_key_settings(home, names, remover=remover)
    left = tuple(name for name in names if name not in removed)
    if canonical and ENV_API_KEY in left and not removed:
        # Not in the profile .env: the shell or the service sets it. Undo.
        try:
            key_store.delete_store(home)
        except key_store.KeyStoreError as exc:
            logger.warning("credentials: the key file written for the move was not removed: %s", exc)
        return MoveOutcome(
            False,
            detail=(
                f"{ENV_API_KEY} is set in this process's environment (the shell or the service), "
                f"not in the profile .env — nothing moved: `{cmd} login --api-key-file <file>` saves "
                "it in the key file, then unset it there"
            ),
        )
    detail = (
        f"{' and '.join(left)} {'is' if len(left) == 1 else 'are'} set in the environment (the shell "
        "or the service), not in the profile .env: unset it there"
        if left
        else ""
    )
    return MoveOutcome(True, path, removed, detail)


class ConfigUnreadable(RuntimeError):
    """Hermes could not read this profile's ``config.yaml``: whether the key source is on is not known."""


@dataclass(frozen=True)
class AdoptOutcome:
    """What :func:`adopt_legacy_key` did. ``copy``: another profile's key file
    holds the key (``owner``): this profile is that agent's copy."""

    status: Literal["adopted", "copy", "busy", "unsupported", "failed"]
    path: Path | None = None
    owner: Path | None = None
    detail: str = ""


def adopt_legacy_key(
    *, home: Path, api_key: str, api_base: str, handle: str = "", lock_dir: Path | None = None
) -> AdoptOutcome:
    """Copy a key found only under the pre-v3 name into this profile's key file.

    The hosted tools authenticate with ``AGENTCHAT_API_KEY``, which Hermes
    gets from the key file through the plugin's secret source; a profile set
    up by a release before v3 has only ``AGENTCHATME_API_KEY``. Done at
    activation, once the server has accepted the key (``handle`` is the one it
    returned) and before the runtime opens a socket, in the default profile
    and in a named profile set up as an agent, under the per-account lock: only when no other profile's key file holds the key, so
    of two profiles holding the same key the first to adopt it is the agent
    (at a multiplexed boot, the launch profile) and the second is refused as
    its copy. The ``.env`` line is left for ``doctor --fix`` (never edited
    from a gateway).
    """
    from .key_source import source_supported

    if not source_supported():
        return AdoptOutcome(
            "unsupported",
            detail="this Hermes has no secret sources, so the hosted tools cannot read a key file",
        )
    with _key_lock(home, api_base=api_base, api_key=api_key, lock_dir=lock_dir) as lock:
        if lock == "busy":
            return AdoptOutcome("busy", detail="another profile is saving the same key right now")
        holders = key_store.other_holders(home, api_key, root=profile_scope.hermes_root_of(home))
        if holders:
            return AdoptOutcome("copy", owner=holders[0].home)
        try:
            path = key_store.write_store(home, api_key=api_key, handle=handle, api_base=api_base)
        except key_store.KeyStoreError as exc:
            return AdoptOutcome("failed", detail=str(exc))
    return AdoptOutcome("adopted", path=path)


# How long a move waits for the per-account lock. It runs inside ``register()``,
# which Hermes abandons at 10 s by default; a busy lock is retried anyway.
KEY_LOCK_WAIT_S = 2.0


def _key_lock(
    home: Path, *, api_base: str, api_key: str, lock_dir: Path | None
) -> AbstractContextManager[HeldLock]:
    """The per-account ``adopt`` lock for ``api_key`` (beside the socket's leader lock)."""
    from .leader_lock import held_lock, lock_path_for

    return held_lock(
        lock_path_for(
            hermes_home=home,
            account_fingerprint=profile_scope.account_fingerprint(api_base, api_key),
            lock_dir=lock_dir,
            purpose="adopt",
        ),
        timeout_s=KEY_LOCK_WAIT_S,
    )


def source_carries_key() -> bool:
    """Does Hermes' MCP client get ``AGENTCHAT_API_KEY`` from the key source in this profile?

    The source is on when Hermes has secret sources and ``secrets.agentchat``
    turns it on (whoever wrote the block). Reads ``config.yaml`` only.
    """
    from .key_source import source_supported
    from .mcp_registration import McpRegistrationError
    from .secrets_registration import inspect_source_enabled

    if not source_supported():
        return False
    try:
        return inspect_source_enabled()
    except McpRegistrationError:
        return False
    except Exception as exc:  # Hermes' reader raised: an unreadable or invalid config.yaml
        raise ConfigUnreadable(f"{type(exc).__name__}: {exc}") from exc


def tools_key_conflict(home: Path, api_key: str) -> str | None:
    """The sentence when Hermes would give the hosted tools another key than the key file's.

    ``None`` while the key source carries the key file (it then supplies its key
    over any other), or when the profile's settings hold no other key.
    """
    from .config import cli_prefix

    try:
        if source_carries_key():
            return None
    except ConfigUnreadable as exc:
        return (
            f"Hermes could not read this profile's config.yaml ({exc}), so which key the hosted "
            "tools would get is not known — fix the file; the gateway activates by itself once it "
            f"reads, and `{cli_prefix(home)} doctor` checks it"
        )
    try:
        theirs = (profile_scope.resolve_setting(ENV_API_KEY) or "").strip()
    except profile_scope.ProfileScopeError:
        return None
    if not theirs or theirs == api_key:
        return None
    return (
        f"Hermes gives the hosted tools {ENV_API_KEY} from the profile .env or the environment — a "
        f"different key than {key_store.store_path(home)}: they would act as another agent. "
        f"`{cli_prefix(home)} doctor --fix` places secrets.agentchat; then restart the gateway"
    )


def _claimed_elsewhere(home: Path, api_key: str) -> str | None:
    """Why ``api_key`` must not be saved for ``home``: another profile's key file holds it."""
    from .config import cli_prefix
    from .profile_scope import hermes_root_of, profile_label

    holders = key_store.other_holders(home, api_key, root=hermes_root_of(home))
    if not holders:
        return None
    others = ", ".join(f"profile {profile_label(h.home)}" for h in holders)
    return (
        f"another profile's key file holds the same key ({others}): one key is one agent, and "
        "saving it here would make this profile that agent. Run `"
        + "` / `".join(f"{cli_prefix(h.home)} logout" for h in holders)
        + "` in the profile that should not be it, then run this again"
    )


def clear_credentials(home: Path | None = None) -> tuple[bool, tuple[str, ...]]:
    """Logout: remove the key file and the key and handle settings from the profile ``.env``.

    Returns ``(key file removed, .env lines removed)``.
    """
    from .config import forget_copy

    home = home if home is not None else profile_scope.resolve_hermes_home()
    removed_file = key_store.delete_store(home)
    removed_lines = remove_key_settings(home, (*ENV_KEY_NAMES, ENV_HANDLE))
    forget_copy(home)
    return removed_file, removed_lines


def remove_key_settings(
    home: Path,
    names: tuple[str, ...] = ENV_KEY_NAMES,
    *,
    remover: Callable[[str], object] | None = None,
) -> tuple[str, ...]:
    """Remove ``names`` from this profile's ``.env`` with Hermes' ``remove_env_value``.

    Returns the names a line was removed for. Hermes removes every line of a
    name, and clears it from this process's environment. Nothing is removed
    for another profile's home, or outside Hermes. Never raises: a line that
    could not be removed is logged and the next doctor run shows it again.
    """
    remove = remover if remover is not None else _hermes_remover(home)
    if remove is None:
        return ()
    removed: list[str] = []
    for name in names:
        try:
            found = remove(name)
        except Exception:
            logger.warning("credentials: could not remove %s from the profile .env", name, exc_info=True)
            continue
        if found is True:
            removed.append(name)
    return tuple(removed)


def _edits_this_profile(home: Path) -> bool:
    """Do Hermes' ``.env`` writers edit ``home``'s file in this process?"""
    try:
        from hermes_cli.config import get_env_path
    except Exception:
        return False
    try:
        return key_store.same_home(Path(get_env_path()).parent, home)
    except Exception:
        return False


def _hermes_remover(home: Path) -> Callable[[str], object] | None:
    """Hermes' ``remove_env_value`` when it edits ``home``'s ``.env``, else ``None``."""
    try:
        from hermes_cli.config import remove_env_value
    except Exception:
        return None
    if not _edits_this_profile(home):
        return None
    return remove_env_value if callable(remove_env_value) else None
