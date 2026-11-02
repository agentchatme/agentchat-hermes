"""Register the hosted AgentChat MCP server as this profile's tool source.

The tools an agent uses on AgentChat are defined once, in the hosted tool
layer at ``https://api.agentchat.me/mcp`` (Streamable HTTP, bearer key). The
doorbell does not define tools; it tells Hermes where they are.

What Hermes 0.22.5 offers, verified in its source and docs (2026-11-01):

* ``mcp_servers.<name>`` in the profile's ``config.yaml`` declares a remote
  MCP server (``url`` + ``headers``). ``${VAR}`` in ``headers`` resolves at
  connect time from the profile's ``.env`` (``hermes_cli/config.py``
  ``_env_ref_lookup``; docs *reference/mcp-config-reference*), and an unset
  variable fails the connect closed with the variable named in the server's
  status. The gateway watches ``config.yaml`` and connects a new entry
  within about a minute; a session picks it up on ``/reload-mcp`` or on the
  next session. Tools appear to the model as ``mcp__<server>__<tool>``.
* A portable *Agent Plugins v1* package may declare the same server in a
  root ``mcp.json``, but Hermes reads that file only for ``plugin.json``
  packages; a directory that also has ``plugin.yaml`` is loaded as a native
  plugin and its ``mcp.json`` is never consulted
  (``hermes_cli/plugins_discovery.py`` ``scan_directory``: the native
  manifest wins). This plugin is native (it needs ``register(ctx)`` for the
  socket, the lock and the CLI), so the live registration is the
  ``config.yaml`` entry below. The repository's ``mcp.json`` carries the
  identical declaration in the portable shape for any harness that reads
  it; :func:`mcp_json_document` is its single source of truth.

The entry is written the way Hermes' own ``hermes mcp install`` writes one
(``hermes_cli/mcp_catalog.py``: ``load_config()`` → mutate ``mcp_servers`` →
``save_config()``), so ``${AGENTCHAT_API_KEY}`` stays a template on disk and
the key never lands in ``config.yaml``. A same-name entry the user wrote
themselves always wins and is never modified.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any, Callable, Literal

from .config import DEFAULT_API_BASE, ENV_API_KEY
from .turn import MCP_SERVER_NAME

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
AUTH_HEADER_TEMPLATE = f"Bearer ${{{ENV_API_KEY}}}"

# Agent Plugins v1.0.0 schema identifier Hermes checks for a root mcp.json
# (``hermes_cli/agent_plugins.py`` MCP_SCHEMA_V1).
MCP_JSON_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

EntryState = Literal["missing", "ours", "user_managed", "disabled"]
EnsureOutcome = Literal["added", "present", "user_managed", "disabled"]


class McpRegistrationError(RuntimeError):
    """The entry could not be read or written through Hermes."""


def hosted_mcp_url(api_base: str = DEFAULT_API_BASE) -> str:
    return api_base.rstrip("/") + MCP_PATH


def server_entry(api_base: str = DEFAULT_API_BASE) -> dict[str, Any]:
    """The ``mcp_servers.agentchat`` value this plugin writes."""
    return {
        "url": hosted_mcp_url(api_base),
        "headers": {"Authorization": AUTH_HEADER_TEMPLATE},
    }


def manual_snippet(api_base: str = DEFAULT_API_BASE) -> str:
    """The same entry as YAML, for the README and for operators placing it by hand."""
    return (
        "mcp_servers:\n"
        f"  {MCP_SERVER_NAME}:\n"
        f'    url: "{hosted_mcp_url(api_base)}"\n'
        "    headers:\n"
        f'      Authorization: "{AUTH_HEADER_TEMPLATE}"\n'
    )


def mcp_json_document(api_base: str = DEFAULT_API_BASE) -> dict[str, Any]:
    """The portable (Agent Plugins v1) declaration of the same server.

    Exactly the top-level shape Hermes' loader accepts (``$schema`` and
    ``mcpServers`` only; ``type: streamable-http`` with ``url`` and
    ``headers``). The repository root ``mcp.json`` is this document; a
    test keeps the two in lockstep.
    """
    return {
        "$schema": MCP_JSON_SCHEMA,
        "mcpServers": {
            MCP_SERVER_NAME: {
                "type": "streamable-http",
                "url": hosted_mcp_url(api_base),
                "headers": {"Authorization": AUTH_HEADER_TEMPLATE},
            }
        },
    }


# ─── pure config-dict operations ───────────────────────────────────────────


def describe_entry(config: Mapping[str, Any], *, api_base: str = DEFAULT_API_BASE) -> EntryState:
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping):
        return "missing"
    entry = servers.get(MCP_SERVER_NAME)
    if entry is None:
        return "missing"
    if not isinstance(entry, Mapping):
        return "user_managed"
    if entry.get("enabled") is False:
        return "disabled"
    return "ours" if _points_at_us(entry, api_base) else "user_managed"


def _points_at_us(entry: Mapping[str, Any], api_base: str) -> bool:
    """URL is the hosted endpoint and the auth header references the key variable.

    Extra keys the user added (``trust``, ``timeout``, ``tools``) keep it
    ours — they narrow or tune, they do not redirect.
    """
    url = entry.get("url")
    if not isinstance(url, str) or url.rstrip("/") != hosted_mcp_url(api_base):
        return False
    headers = entry.get("headers")
    if not isinstance(headers, Mapping):
        return False
    auth = next(
        (v for k, v in headers.items() if isinstance(k, str) and k.lower() == "authorization"),
        None,
    )
    if not isinstance(auth, str):
        return False
    return f"${{{ENV_API_KEY}}}" in auth or f"${{env:{ENV_API_KEY}}}" in auth


def ensure_entry(
    config: MutableMapping[str, Any], *, api_base: str = DEFAULT_API_BASE
) -> EnsureOutcome:
    """Add the entry when absent. Never modifies an existing one."""
    state = describe_entry(config, api_base=api_base)
    if state == "missing":
        servers = config.get("mcp_servers")
        if not isinstance(servers, MutableMapping):
            servers = {}
            config["mcp_servers"] = servers
        servers[MCP_SERVER_NAME] = server_entry(api_base)
        return "added"
    if state == "ours":
        return "present"
    return state


def remove_entry(config: MutableMapping[str, Any], *, api_base: str = DEFAULT_API_BASE) -> bool:
    """Remove the entry only when it is ours. ``True`` when removed."""
    if describe_entry(config, api_base=api_base) != "ours":
        return False
    servers = config["mcp_servers"]
    del servers[MCP_SERVER_NAME]
    if not servers:
        del config["mcp_servers"]
    return True


# ─── Hermes-backed operations ──────────────────────────────────────────────

ConfigLoader = Callable[[], dict[str, Any]]
ConfigSaver = Callable[[dict[str, Any]], Any]


def _hermes_config_api() -> tuple[ConfigLoader, ConfigSaver]:
    try:
        from hermes_cli.config import load_config, save_config
    except ImportError as exc:
        raise McpRegistrationError(
            "Hermes' config helpers are not importable here, so the tool source could not "
            "be registered. Add this to the profile's config.yaml by hand:\n" + manual_snippet()
        ) from exc
    return load_config, save_config


def _hermes_raw_config() -> dict[str, Any]:
    """The profile's ``config.yaml`` with ``${VAR}`` templates intact."""
    try:
        from hermes_cli import config as hermes_config
    except ImportError as exc:
        raise McpRegistrationError("Hermes' config helpers are not importable here") from exc
    for name in ("read_user_config_raw", "read_raw_config", "load_config"):
        reader = getattr(hermes_config, name, None)
        if callable(reader):
            data = reader()
            return data if isinstance(data, dict) else {}
    raise McpRegistrationError("Hermes exposes no config reader this plugin knows")


def install_entry(*, api_base: str = DEFAULT_API_BASE) -> EnsureOutcome:
    """Ensure the entry exists in the active profile's ``config.yaml``."""
    load_config, save_config = _hermes_config_api()
    config = load_config()
    outcome = ensure_entry(config, api_base=api_base)
    if outcome == "added":
        save_config(config)
        logger.info("mcp_registration: added mcp_servers.%s → %s", MCP_SERVER_NAME, hosted_mcp_url(api_base))
    return outcome


def uninstall_entry(*, api_base: str = DEFAULT_API_BASE) -> bool:
    """Remove our entry from the active profile's ``config.yaml`` (user entries untouched)."""
    load_config, save_config = _hermes_config_api()
    config = load_config()
    removed = remove_entry(config, api_base=api_base)
    if removed:
        save_config(config)
        logger.info("mcp_registration: removed mcp_servers.%s", MCP_SERVER_NAME)
    return removed


def inspect_entry(*, api_base: str = DEFAULT_API_BASE) -> EntryState:
    """State of the entry in the active profile's raw ``config.yaml`` (for ``doctor``)."""
    return describe_entry(_hermes_raw_config(), api_base=api_base)
