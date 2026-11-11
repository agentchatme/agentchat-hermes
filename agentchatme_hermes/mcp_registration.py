"""Register the hosted AgentChat MCP server as this profile's tool source.

The tools an agent uses on AgentChat are defined once, in the hosted tool
layer at ``https://api.agentchat.me/mcp`` (Streamable HTTP, bearer key). The
integration does not define tools; it tells Hermes where they are.

What Hermes offers, verified in its source and docs (0.22.5 on 2026-11-01;
the headers and reload rules again on v0.23.1 and main, 2026-11-11):

* ``mcp_servers.<name>`` in the profile's ``config.yaml`` declares a remote
  MCP server (``url`` + ``headers``). ``${VAR}`` in ``headers`` resolves at
  connect time from the profile's ``.env`` (``hermes_cli/config.py``
  ``_env_ref_lookup``; docs *reference/mcp-config-reference*), and an unset
  variable fails the connect closed with the variable named in the server's
  status. Every configured header is sent on each request, unchanged
  (``tools/mcp_tool_transport.py`` ``_http_endpoint``). The gateway watches
  ``config.yaml`` and connects a new entry within about a minute; a session
  picks it up on ``/reload-mcp`` or on the next session. A changed entry
  (new headers) is used once its connection is rebuilt: ``/reload-mcp``, a
  gateway restart, or a reconnect. Tools appear to the model as
  ``mcp__<server>__<tool>``.
* The hosted tool layer knows the client only from request headers
  (``X-AgentChat-Client``), not from MCP ``clientInfo``. The entry carries
  the plugin's identity (:func:`client_identity.hermes_client_headers`), so
  the server treats this agent as the Hermes socket integration and shows
  it no wake-webhook tools — the socket is its wake path.
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
the key never lands in ``config.yaml``.

An entry with this URL and the ``${AGENTCHAT_API_KEY}`` bearer is the
plugin's (an earlier release's, or the documented snippet copied by hand;
they cannot be told apart), with any keys the user added (``trust``,
``timeout``, ``tools``) kept. When its client headers are missing or name
an older version, :func:`ensure_entry` sets them in place and changes
nothing else; a client header someone set to another name is left alone.
Any other same-name entry is the user's and is never modified; ``doctor``
says how to add the headers to it.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any, Callable, Literal

from .client_identity import (
    CLIENT_HEADER,
    CLIENT_NAME,
    CLIENT_VERSION_HEADER,
    hermes_client_headers,
)
from .config import DEFAULT_API_BASE, ENV_API_KEY
from .turn import MCP_SERVER_NAME

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
AUTH_HEADER_TEMPLATE = f"Bearer ${{{ENV_API_KEY}}}"

# Agent Plugins v1.0.0 schema identifier Hermes checks for a root mcp.json
# (``hermes_cli/agent_plugins.py`` MCP_SCHEMA_V1).
MCP_JSON_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

EntryState = Literal["missing", "ours", "user_managed", "disabled"]
EnsureOutcome = Literal["added", "present", "upgraded", "user_managed", "disabled"]
# How an entry's headers name the client: ``current`` (hermes, this version),
# ``stale`` (hermes, another version or none), ``missing`` (no client header:
# placed before 0.5.1, or written by hand), ``other`` (a name someone chose).
IdentityState = Literal["current", "stale", "missing", "other"]


class McpRegistrationError(RuntimeError):
    """The entry could not be read or written through Hermes."""


def hosted_mcp_url(api_base: str = DEFAULT_API_BASE) -> str:
    return api_base.rstrip("/") + MCP_PATH


def server_entry(api_base: str = DEFAULT_API_BASE) -> dict[str, Any]:
    """The ``mcp_servers.agentchat`` value this plugin writes."""
    return {
        "url": hosted_mcp_url(api_base),
        "headers": {"Authorization": AUTH_HEADER_TEMPLATE, **hermes_client_headers()},
    }


def manual_snippet(api_base: str = DEFAULT_API_BASE) -> str:
    """The same entry as YAML, for the README and for operators placing it by hand."""
    return (
        "mcp_servers:\n"
        f"  {MCP_SERVER_NAME}:\n"
        f'    url: "{hosted_mcp_url(api_base)}"\n'
        "    headers:\n"
        f'      Authorization: "{AUTH_HEADER_TEMPLATE}"\n'
        + client_header_lines("      ")
    )


def client_header_lines(indent: str = "") -> str:
    """The two client headers as YAML lines, for a hand-written entry."""
    return "".join(f'{indent}{name}: "{value}"\n' for name, value in hermes_client_headers().items())


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
                "headers": {"Authorization": AUTH_HEADER_TEMPLATE, **hermes_client_headers()},
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


def _header(headers: Mapping[str, Any], name: str) -> Any:
    """A header's value by case-insensitive name (``None`` when absent)."""
    lowered = name.lower()
    return next((v for k, v in headers.items() if isinstance(k, str) and k.lower() == lowered), None)


def entry_identity(entry: Any) -> IdentityState:
    """How ``entry``'s headers name the client (see :data:`IdentityState`)."""
    headers = entry.get("headers") if isinstance(entry, Mapping) else None
    if not isinstance(headers, Mapping):
        return "missing"
    client = _header(headers, CLIENT_HEADER)
    if not isinstance(client, str) or not client.strip():
        return "missing"
    if client.strip().lower() != CLIENT_NAME:
        return "other"
    wanted = hermes_client_headers()[CLIENT_VERSION_HEADER]
    return "current" if _header(headers, CLIENT_VERSION_HEADER) == wanted else "stale"


def _set_client_headers(entry: MutableMapping[str, Any]) -> None:
    """Set the two client headers on our entry, replacing any casing of
    them; every other header and key stays as it is."""
    headers = dict(entry.get("headers") or {})
    names = {CLIENT_HEADER.lower(), CLIENT_VERSION_HEADER.lower()}
    kept = {k: v for k, v in headers.items() if not (isinstance(k, str) and k.lower() in names)}
    entry["headers"] = {**kept, **hermes_client_headers()}


def ensure_entry(
    config: MutableMapping[str, Any], *, api_base: str = DEFAULT_API_BASE
) -> EnsureOutcome:
    """Add the entry when absent; bring our entry's client headers up to date.

    Never touches a user-managed or disabled entry, nor a client header
    someone set to another name.
    """
    state = describe_entry(config, api_base=api_base)
    if state == "missing":
        servers = config.get("mcp_servers")
        if not isinstance(servers, MutableMapping):
            servers = {}
            config["mcp_servers"] = servers
        servers[MCP_SERVER_NAME] = server_entry(api_base)
        return "added"
    if state == "ours":
        entry = config["mcp_servers"][MCP_SERVER_NAME]
        if entry_identity(entry) in ("missing", "stale") and isinstance(entry, MutableMapping):
            _set_client_headers(entry)
            return "upgraded"
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
    elif outcome == "upgraded":
        save_config(config)
        logger.info("mcp_registration: set the client headers on mcp_servers.%s", MCP_SERVER_NAME)
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


def inspect_entry_identity(*, api_base: str = DEFAULT_API_BASE) -> tuple[EntryState, IdentityState | None]:
    """The entry's state and, when there is one, how it names the client (for ``doctor``)."""
    config = _hermes_raw_config()
    state = describe_entry(config, api_base=api_base)
    if state == "missing":
        return state, None
    servers = config.get("mcp_servers")
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, Mapping) else None
    return state, entry_identity(entry)
