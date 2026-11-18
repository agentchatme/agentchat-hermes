"""``secrets.agentchat`` in the profile's ``config.yaml``.

The block turns on the key source (:mod:`.key_source`) and names
``AGENTCHAT_API_KEY`` as a credential, so Hermes keeps it out of every
command the agent runs::

    secrets:
      agentchat:
        enabled: true
        api_key_env: AGENTCHAT_API_KEY

It holds no key. Read, changed and saved the way :mod:`.mcp_registration`
does it: the file as written (``read_user_config_raw``), Hermes'
``save_config`` round trip (comments, key order and quotes kept), nothing
written on a managed install.

A ``secrets.agentchat`` that is not exactly ours is the user's: ``enabled:
false`` is left off (doctor says what that costs), a different
``api_key_env`` or extra keys are left alone. An ``enabled: true`` block
without ``api_key_env`` gets it added.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any, Literal

from .config import ENV_API_KEY
from .key_source import KEY_ENV_FIELD, SOURCE_NAME, secrets_section
from .mcp_registration import McpRegistrationError, _hermes_config_io

logger = logging.getLogger(__name__)

BlockState = Literal["missing", "ours", "incomplete", "disabled", "user_managed"]
EnsureOutcome = Literal["added", "completed", "present", "disabled", "user_managed"]


def manual_snippet() -> str:
    """The block as YAML, for docs and for a managed install."""
    return (
        "secrets:\n"
        f"  {SOURCE_NAME}:\n"
        "    enabled: true\n"
        f"    {KEY_ENV_FIELD}: {ENV_API_KEY}\n"
    )


def describe_block(config: Mapping[str, Any]) -> BlockState:
    secrets = config.get("secrets")
    if not isinstance(secrets, Mapping):
        return "missing"
    block = secrets.get(SOURCE_NAME)
    if block is None:
        return "missing"
    if not isinstance(block, Mapping):
        return "user_managed"
    if block.get("enabled") is False:
        return "disabled"
    if block.get("enabled") is not True:
        return "user_managed"
    key_env = block.get(KEY_ENV_FIELD)
    if key_env is None:
        return "incomplete"
    return "ours" if key_env == ENV_API_KEY else "user_managed"


def ensure_block(config: MutableMapping[str, Any]) -> EnsureOutcome:
    """Add the block when absent; add ``api_key_env`` to an enabled block without it."""
    state = describe_block(config)
    if state == "missing":
        secrets = config.get("secrets")
        if not isinstance(secrets, MutableMapping):
            secrets = {}
            config["secrets"] = secrets
        secrets[SOURCE_NAME] = secrets_section()
        return "added"
    if state == "incomplete":
        config["secrets"][SOURCE_NAME][KEY_ENV_FIELD] = ENV_API_KEY
        return "completed"
    if state == "ours":
        return "present"
    return state


def remove_block(config: MutableMapping[str, Any]) -> bool:
    """Remove the block when it is ours. ``True`` when removed."""
    if describe_block(config) not in ("ours", "incomplete"):
        return False
    block = config["secrets"][SOURCE_NAME]
    if set(block) - {"enabled", KEY_ENV_FIELD}:
        return False  # the user added keys of their own: theirs now
    secrets = config["secrets"]
    del secrets[SOURCE_NAME]
    if not secrets:
        del config["secrets"]
    return True


# ─── Hermes-backed operations ──────────────────────────────────────────────


def _config_io() -> tuple[Any, Any, Any]:
    """Hermes' raw reader, saver and managed check; the error names this block, not the tools'."""
    try:
        return _hermes_config_io()
    except McpRegistrationError as exc:
        raise McpRegistrationError(
            _by_hand("Hermes' config.yaml reader or saver is not available here")
        ) from exc


def install_block() -> EnsureOutcome:
    """Ensure the block in the active profile's ``config.yaml``; raise when the write did not land."""
    read, save_config, is_managed = _config_io()
    config = read()
    outcome = ensure_block(config)
    if outcome not in ("added", "completed"):
        return outcome
    if is_managed():
        raise McpRegistrationError(
            _by_hand("Hermes' configuration is managed by a package manager, so the plugin cannot "
                     "write config.yaml")
        )
    save_config(config)
    if describe_block(read()) != "ours":
        raise McpRegistrationError(_by_hand("Hermes did not save the change to config.yaml"))
    logger.info("secrets_registration: %s secrets.%s", outcome, SOURCE_NAME)
    return outcome


def uninstall_block() -> bool:
    """Remove our block from the active profile's ``config.yaml`` (anything else untouched)."""
    read, save_config, is_managed = _config_io()
    config = read()
    if describe_block(config) not in ("ours", "incomplete"):
        return False
    if is_managed():
        raise McpRegistrationError(
            "Hermes' configuration is managed by a package manager, so the plugin cannot remove "
            f"secrets.{SOURCE_NAME}; remove it there"
        )
    if not remove_block(config):
        return False
    save_config(config)
    logger.info("secrets_registration: removed secrets.%s", SOURCE_NAME)
    return True


def source_enabled(config: Mapping[str, Any]) -> bool:
    """Does ``config`` turn the key source on (whoever wrote the block)?"""
    secrets = config.get("secrets")
    block = secrets.get(SOURCE_NAME) if isinstance(secrets, Mapping) else None
    return isinstance(block, Mapping) and block.get("enabled") is True


def inspect_source_enabled() -> bool:
    read, _save, _managed = _config_io()
    return source_enabled(read())


def inspect_block() -> BlockState:
    read, _save, _managed = _config_io()
    return describe_block(read())


def _by_hand(reason: str) -> str:
    return f"{reason}. Add this to the profile's config.yaml by hand:\n{manual_snippet()}"
