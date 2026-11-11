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

The entry is read, changed and saved as written: ``read_user_config_raw()``
→ change ``mcp_servers`` → ``save_config()``, Hermes' own write-back round
trip (``load_config()`` would expand ``${AGENTCHAT_API_KEY}`` from the
``.env`` first). ``${AGENTCHAT_API_KEY}`` stays a template on disk and the
key never lands in ``config.yaml``.

An entry with this URL and the ``${AGENTCHAT_API_KEY}`` bearer is the
plugin's (an earlier release's, or the documented snippet copied by hand;
they cannot be told apart), with any keys the user added (``trust``,
``timeout``, ``tools``) kept. When its client headers are missing or name
an older version, :func:`ensure_entry` sets them in place and changes
nothing else (a newer version is left as it is, so two installs sharing a
profile never rewrite it back and forth); a client header someone set to
another name is left alone.
Any other same-name entry is the user's and is never modified; ``doctor``
says how to add the headers to it.
"""
from __future__ import annotations

import logging
import re
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
# How an entry's headers name the client: ``current`` (hermes, this version or
# a newer one), ``stale`` (hermes, an older or unreadable version, or none),
# ``missing`` (no client header: placed before 0.5.1, or written by hand),
# ``other`` (a name someone chose).
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
    ours = hermes_client_headers()[CLIENT_VERSION_HEADER]
    theirs = _header(headers, CLIENT_VERSION_HEADER)
    if theirs == ours:
        return "current"
    ours_key, theirs_key = _version_key(ours), _version_key(theirs)
    if ours_key is not None and theirs_key is not None and theirs_key > ours_key:
        return "current"  # a newer install shares this profile; never downgrade it
    return "stale"


def _version_key(version: Any) -> tuple[int, int, int, int, str] | None:
    """A total order on version strings, else ``None``: ``x.y.z`` numerically,
    then pre-releases (``rc1``) below the release and post or local builds
    (``.post1``, ``+local``) above it, then the suffix as text. Total, so two
    installs never both see the other as older."""
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)(.*)$", version) if isinstance(version, str) else None
    if match is None:
        return None
    suffix = match[4].strip()
    rank = 1 if not suffix else 2 if suffix.startswith(("+", ".post", "post", "-post")) else 0
    return (int(match[1]), int(match[2]), int(match[3]), rank, suffix)


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


def _hermes_config_io() -> tuple[ConfigLoader, ConfigSaver, Callable[[], bool]]:
    """Hermes' reader for ``config.yaml`` *as written*, its saver, and its
    "is this install managed" check.

    Ownership is decided on the file as written. ``load_config()`` expands
    ``${AGENTCHAT_API_KEY}`` from the profile's ``.env``, which every running
    Hermes has loaded, so read through it our own entry carries a literal
    key and looks hand-written. ``read_user_config_raw()`` is Hermes' reader
    for exactly this write-back round trip, and ``save_config()`` keeps the
    templates.
    """
    try:
        from hermes_cli import config as hermes_config
    except ImportError as exc:
        raise McpRegistrationError(
            "Hermes' config helpers are not importable here, so the tool source could not "
            "be registered. Add this to the profile's config.yaml by hand:\n" + manual_snippet()
        ) from exc
    reader = next(
        (
            fn
            for fn in (getattr(hermes_config, name, None) for name in ("read_user_config_raw", "read_raw_config"))
            if callable(fn)
        ),
        None,
    )
    saver = getattr(hermes_config, "save_config", None)
    if reader is None or not callable(saver):
        raise McpRegistrationError(
            "this Hermes has no raw config reader or saver the plugin knows, so the tool source "
            "could not be registered. Add this to the profile's config.yaml by hand:\n" + manual_snippet()
        )
    managed = getattr(hermes_config, "is_managed", None)

    def is_managed() -> bool:
        return bool(managed()) if callable(managed) else False

    def read() -> dict[str, Any]:
        data = reader()
        return data if isinstance(data, dict) else {}

    return read, saver, is_managed


def _hermes_raw_config() -> dict[str, Any]:
    """The profile's ``config.yaml`` with ``${VAR}`` templates intact."""
    read, _save, _is_managed = _hermes_config_io()
    return read()


def install_entry(*, api_base: str = DEFAULT_API_BASE) -> EnsureOutcome:
    """Ensure the entry exists, with current client headers, in the active
    profile's ``config.yaml``. Writes only when something changes, and
    raises :class:`McpRegistrationError` when the change did not land."""
    read, save_config, is_managed = _hermes_config_io()
    config = read()
    outcome = ensure_entry(config, api_base=api_base)
    if outcome not in ("added", "upgraded"):
        return outcome
    if is_managed():
        raise McpRegistrationError(
            "Hermes' configuration is managed by a package manager, so the plugin cannot write "
            "config.yaml. Declare the hosted tools there:\n" + manual_snippet(api_base)
        )
    save_config(config)
    if ensure_entry(read(), api_base=api_base) != "present":
        raise McpRegistrationError(
            "Hermes did not save the change to config.yaml. Add this by hand:\n" + manual_snippet(api_base)
        )
    if outcome == "added":
        logger.info("mcp_registration: added mcp_servers.%s → %s", MCP_SERVER_NAME, hosted_mcp_url(api_base))
    else:
        logger.info("mcp_registration: set the client headers on mcp_servers.%s", MCP_SERVER_NAME)
    return outcome


def uninstall_entry(*, api_base: str = DEFAULT_API_BASE) -> bool:
    """Remove our entry from the active profile's ``config.yaml`` (user entries untouched)."""
    read, save_config, is_managed = _hermes_config_io()
    config = read()
    if describe_entry(config, api_base=api_base) != "ours":
        return False
    if is_managed():
        raise McpRegistrationError(
            "Hermes' configuration is managed by a package manager, so the plugin cannot remove "
            f"mcp_servers.{MCP_SERVER_NAME}; remove it there"
        )
    remove_entry(config, api_base=api_base)
    save_config(config)
    logger.info("mcp_registration: removed mcp_servers.%s", MCP_SERVER_NAME)
    return True


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
