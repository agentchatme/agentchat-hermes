"""The key file, handed to Hermes for the hosted tools: a Hermes secret source.

Hermes' MCP client connects the hosted AgentChat tools itself and sends
``Authorization: Bearer ${AGENTCHAT_API_KEY}`` (the ``mcp_servers.agentchat``
entry, :mod:`.mcp_registration`). Hermes resolves ``${VAR}`` in an MCP header
from a profile's secrets only — its ``.env`` and its *secret sources*
(``hermes_cli/config.py`` ``_env_ref_lookup``; ``agent/secret_scope.py``
``build_profile_secret_scope``) — so the key file reaches the tools the way
Hermes documents for a credential that lives outside ``.env``: a secret-source
plugin (``website/docs/developer-guide/secret-source-plugin.md``;
``agent.secret_sources.base`` and ``ctx.register_secret_source`` are on
Hermes' public plugin API).

* :class:`AgentChatKeySource` (``secrets.agentchat`` in ``config.yaml``)
  supplies ``AGENTCHAT_API_KEY`` from *this profile's* key file
  (:mod:`.key_store`), never another profile's: ``fetch`` is called per
  profile home, and under a multiplexed gateway Hermes keeps each profile's
  values in that profile's own scope, out of the shared process environment.
  ``override_existing`` is on, so a stale ``.env`` line or an exported
  variable never decides which agent the tools act as. A profile whose key
  is a copy of another profile's (``config.copy_owner``: a cloned profile)
  gets the variable *empty*, over the copy in its ``.env``, so its hosted
  tools fail closed instead of acting as the other agent.
* The same ``secrets.agentchat`` block carries ``api_key_env:
  AGENTCHAT_API_KEY``. Hermes reads a ``*_key_env`` field anywhere in a
  profile's ``config.yaml`` (outside ``mcp_servers``) as naming a credential,
  and strips that variable from every child it spawns for the agent —
  terminal commands, background processes, ``execute_code``, its terminal env
  snapshots — and refuses it as an ``env_passthrough`` name
  (``tools/environments/local_env_policy.py`` ``_home_declared_key_env``;
  Hermes v0.23.2 and later). Without it the key, wherever it came from, is in
  the environment of every command the agent runs.

Timing (Hermes' own, documented): sources resolve at process start, before
plugins register; right after plugin discovery Hermes re-pulls every enabled
plugin source, so the discovering process — gateway, CLI session, cron —
gets the key. Hermes then keeps the value for the process's life (a fetched
home is not fetched again; a ``.env`` reload re-applies it), so a key saved
or replaced after that (``hermes agentchat login`` / ``recover`` while a
gateway or a session runs) reaches the hosted tools when that process
restarts.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import key_store
from .config import ENV_API_KEY, cli_prefix, copy_owner
from .profile_scope import profile_label, resolve_hermes_home

logger = logging.getLogger(__name__)

SOURCE_NAME = "agentchat"
SOURCE_LABEL = "AgentChat key file"
# The field Hermes reads as "this variable is a credential" (any ``*_key_env``).
KEY_ENV_FIELD = "api_key_env"


def secrets_section() -> dict[str, Any]:
    """The ``secrets.agentchat`` value this plugin writes."""
    return {"enabled": True, KEY_ENV_FIELD: ENV_API_KEY}


def source_supported() -> bool:
    """Is Hermes' secret-source contract importable in this process?"""
    try:
        from agent.secret_sources.base import SECRET_SOURCE_API_VERSION, SecretSource
    except Exception:
        return False
    return isinstance(SECRET_SOURCE_API_VERSION, int) and isinstance(SecretSource, type)


def build_key_source() -> Any | None:
    """A new :class:`AgentChatKeySource`, or ``None`` outside Hermes.

    The class is built on Hermes' own ``SecretSource`` (its ABC), so it is
    defined only when Hermes is importable.
    """
    try:
        from agent.secret_sources.base import ErrorKind, FetchResult, SecretSource
    except Exception:
        return None

    class AgentChatKeySource(SecretSource):  # type: ignore[misc]
        """``AGENTCHAT_API_KEY`` from this profile's AgentChat key file."""

        name = SOURCE_NAME
        label = SOURCE_LABEL
        shape = "mapped"
        # The profile's own key file decides which agent the hosted tools are;
        # an exported variable or an old .env line never does.
        override_existing_default = True

        def is_enabled(self, cfg: Any) -> bool:
            # Enabled by ``secrets.agentchat.enabled: true`` and only while this
            # profile has something to say: a key file it may use, or a key
            # that is a copy of another profile's (masked, see ``fetch``). With
            # neither, Hermes does not count a fetch for the profile, so a key
            # saved later is picked up by the next reload.
            if not (isinstance(cfg, dict) and cfg.get("enabled") is True):
                return False
            try:
                home = resolve_hermes_home()
                return key_store.read_store(home).usable or copy_owner(home) is not None
            except Exception:
                return False

        def fetch(self, cfg: Any, home_path: Path) -> Any:
            result = FetchResult()
            try:
                home = Path(home_path)
                read = key_store.read_store(home)
                if read.usable and read.key is not None:
                    result.secrets = {ENV_API_KEY: read.key.api_key}
                    return result
                owner = copy_owner(home)
                if owner is not None:
                    # This profile holds a copy of another profile's key (a
                    # cloned profile). Its hosted tools must not act as that
                    # agent: the variable is supplied empty, over the copy in
                    # its .env, so the tools fail closed until the copy is
                    # logged out. The plugin itself refuses to run there.
                    result.secrets = {ENV_API_KEY: ""}
                    result.warnings.append(
                        f"profile {profile_label(home)} holds a copy of profile "
                        f"{profile_label(owner)}'s AgentChat key; its AgentChat tools get no key"
                    )
                    return result
                if read.status == "missing":
                    result.error = (
                        f"no AgentChat key saved for profile {profile_label(home)} "
                        f"({read.path}); run `{cli_prefix(home)} login`"
                    )
                    result.error_kind = ErrorKind.NOT_CONFIGURED
                elif read.status == "copied":
                    result.error = (
                        f"the AgentChat key file of profile {profile_label(home)} is a copy of "
                        f"profile {profile_label(read.origin_home or home)}'s; not used"
                    )
                    result.error_kind = ErrorKind.REF_INVALID
                else:
                    result.error = read.detail or f"{read.path} cannot be used"
                    result.error_kind = ErrorKind.REF_INVALID
            except Exception as exc:  # the contract: fetch never raises
                result.error = f"reading the AgentChat key file failed ({type(exc).__name__})"
                result.error_kind = ErrorKind.INTERNAL
            return result

        def config_schema(self) -> dict[str, Any]:
            return {
                "enabled": {
                    "description": "Supply AGENTCHAT_API_KEY from this profile's AgentChat key file "
                    "(<profile>/agentchat/credentials.json) to Hermes' MCP client.",
                    "default": True,
                },
                KEY_ENV_FIELD: {
                    "description": "The variable the key is supplied as; Hermes keeps it out of "
                    "every command the agent runs. Always AGENTCHAT_API_KEY.",
                    "default": ENV_API_KEY,
                },
            }

        def remediation(self, kind: Any, cfg: Any) -> str:
            return "Run `hermes agentchat doctor` in this profile."

    return AgentChatKeySource()


def register_key_source(ctx: Any) -> str:
    """Register the source with Hermes. Returns ``registered``, ``unsupported`` or ``failed``."""
    register = getattr(ctx, "register_secret_source", None)
    if not callable(register):
        logger.warning(
            "agentchat plugin: this Hermes has no ctx.register_secret_source — the hosted tools "
            "cannot read the key from the AgentChat key file; keep the key in the profile .env "
            "or upgrade Hermes"
        )
        return "unsupported"
    source = build_key_source()
    if source is None:
        logger.warning(
            "agentchat plugin: Hermes' secret-source contract (agent.secret_sources.base) is not "
            "importable — the hosted tools cannot read the key from the AgentChat key file"
        )
        return "unsupported"
    try:
        accepted = register(source)
    except Exception:
        logger.warning("agentchat plugin: registering the key source failed", exc_info=True)
        return "failed"
    # Hermes returns the registration's handle, or ``None`` when it refused
    # the source (and logged why).
    if accepted is None or accepted is False:
        logger.warning("agentchat plugin: Hermes refused the key source (see the line above)")
        return "failed"
    return "registered"


def supplied_by_us(name: str = ENV_API_KEY) -> bool:
    """Was the process environment's value of ``name`` supplied by this source?

    Hermes records which source supplied a variable (``agent.secret_scope.
    secret_source_label``, public). ``False`` when it cannot tell.
    """
    try:
        from agent.secret_scope import secret_source_label
    except Exception:
        return False
    try:
        suffix = str(secret_source_label(name) or "")
    except Exception:
        return False
    # The label when Hermes finds the registration, the bare name when it does
    # not (a profile-scoped registration looked up without its scope).
    return f"(from {SOURCE_LABEL})" in suffix or f"(from {SOURCE_NAME})" in suffix
