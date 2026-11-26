"""Register the hosted AgentChat MCP server as this profile's tool source.

The tools an agent uses on AgentChat are defined once, in the hosted tool
layer at ``https://api.agentchat.me/mcp`` (Streamable HTTP, bearer key). The
integration does not define tools; it tells Hermes where they are.

What Hermes offers, verified in its source and docs (0.22.5 on 2026-11-01;
the headers and reload rules again on v0.23.1 and main, 2026-11-11):

* ``mcp_servers.<name>`` in the profile's ``config.yaml`` declares a remote
  MCP server (``url`` + ``headers``). ``${VAR}`` in ``headers`` resolves at
  connect time from the profile's secrets — its ``.env`` and its secret
  sources; this plugin's source supplies ``AGENTCHAT_API_KEY`` from the key
  file (:mod:`.key_source`) — (``hermes_cli/config.py``
  ``_env_ref_lookup``; docs *reference/mcp-config-reference*), and an unset
  variable fails the connect closed with the variable named in the server's
  status. Every configured header is sent on each request, unchanged
  (``tools/mcp_tool_transport.py`` ``_http_endpoint``). ``save_config``
  writes through a ruamel round trip that keeps the file's comments, key
  order, quotes and blank lines and reassigns only the nodes that changed
  (``utils.atomic_roundtrip_yaml_save``; v0.22.5, v0.23.1 and main). The gateway watches
  ``config.yaml`` and connects a new entry within about a minute; a session
  picks it up on ``/reload-mcp`` or on the next session. A changed entry
  (new headers) is used once its connection is rebuilt: ``/reload-mcp``, a
  gateway restart, or a reconnect. Tools appear to the model as
  ``mcp__<server>__<tool>``.
* The hosted tool layer knows the client only from request headers
  (``X-AgentChat-Client``), not from MCP ``clientInfo``. The entry carries
  the plugin's name (:func:`client_identity.hermes_mcp_headers`), so the
  server treats this agent as the Hermes socket integration and shows it no
  wake-webhook tools — the socket is its wake path. The entry is static (no
  version header): once written it must not change from release to release
  (only a person's yes changes its ``trust``).
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
profile's secrets first). ``${AGENTCHAT_API_KEY}`` stays a template on disk
and the key never lands in ``config.yaml``.

An entry with this URL and the ``${AGENTCHAT_API_KEY}`` bearer is the
plugin's (an earlier release's, or the documented snippet copied by hand;
they cannot be told apart), with any keys the user added (``trust``,
``timeout``, ``tools``) kept. When it has no client header,
:func:`ensure_entry` adds ``X-AgentChat-Client: hermes`` and changes
nothing else: at most one write per install, never again (apart from a
person's yes, below). An entry that
already says ``hermes`` is left as it is (a version header too, if one is
there); a client header someone set to another name is left alone.
Any other same-name entry is the user's and is never modified; ``doctor``
says how to add the headers to it.

The one switch (``trust``)
──────────────────────────
Hermes gates every tool of a ``trust: untrusted`` server that is not
annotated read-only: each call asks the person, and a turn nobody can answer
is declined (``tools/mcp_tool_handlers.py`` ``_trust_gate_check``). A missing
``trust`` key means ``full`` (``tools/mcp_tool_registration.py``
``_normalize_server_trust``). The entry is written only once a person has
decided (:data:`Decision`): a yes writes ``trust: full`` (and turns an
existing ``untrusted`` entry of ours to ``full``), a no writes ``trust:
untrusted``; with nobody asked yet nothing is added. The one exception is the
recorded carry-over for a profile set up before 0.3 (:mod:`.allow`). Every
entry the plugin writes names its ``trust``; an entry an earlier release
wrote without one is left as it is. Hermes records a server's tier when it
connects it: a new entry is connected with the tier it was written with
(the gateway's config reconcile, about once a minute), a changed tier on a
connected server applies after ``/reload-mcp`` or a restart.
:func:`tools_allowance` is Hermes' predicate for the reply tool, read from
the same file: the tier, ``approvals.mode: off``, ``HERMES_YOLO_MODE`` and a
remembered Always approval.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Callable, Literal

from .client_identity import CLIENT_HEADER, CLIENT_NAME, hermes_mcp_headers
from .config import DEFAULT_API_BASE, ENV_API_KEY
from .turn import MCP_SERVER_NAME

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
AUTH_HEADER_TEMPLATE = f"Bearer ${{{ENV_API_KEY}}}"

# Agent Plugins v1.0.0 schema identifier Hermes checks for a root mcp.json
# (``hermes_cli/agent_plugins.py`` MCP_SCHEMA_V1).
MCP_JSON_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

EntryState = Literal["missing", "ours", "user_managed", "disabled"]
# ``added``: written with the decision's trust. ``allowed``: an existing entry
# of ours turned to ``trust: full`` (a person's yes). ``upgraded``: the client
# header added. ``missing``: no entry, and nobody decided, so none is written.
EnsureOutcome = Literal["added", "allowed", "present", "upgraded", "missing", "user_managed", "disabled"]
# The tiers the plugin writes, and what an entry says (``unset``: no ``trust``
# key, which Hermes reads as ``full``).
Trust = Literal["full", "untrusted"]
TrustState = Literal["full", "untrusted", "unset"]
# Who decided the entry's tier: a person's ``yes`` / ``no``, or the one-time
# carry-over of a setup made before 0.3 (:mod:`.allow`).
Decision = Literal["yes", "no", "carried_over"]
TRUST_FOR: dict[str, Trust] = {"yes": "full", "no": "untrusted", "carried_over": "full"}
# The write tool a reply turn needs (Hermes keys a remembered approval by the
# raw MCP tool name: ``mcp:<server>/<tool>``).
REPLY_TOOL = "agentchat_send_message"
# How an entry's headers name the client: ``current`` (hermes), ``missing`` (no
# client header: placed before 0.5.1, or written by hand), ``other`` (a name
# someone chose).
IdentityState = Literal["current", "missing", "other"]


class McpRegistrationError(RuntimeError):
    """The entry could not be read or written through Hermes."""


def hosted_mcp_url(api_base: str = DEFAULT_API_BASE) -> str:
    return api_base.rstrip("/") + MCP_PATH


def server_entry(api_base: str = DEFAULT_API_BASE, *, trust: Trust = "untrusted") -> dict[str, Any]:
    """The ``mcp_servers.agentchat`` value this plugin writes, with its tier."""
    return {
        "url": hosted_mcp_url(api_base),
        "headers": {"Authorization": AUTH_HEADER_TEMPLATE, **hermes_mcp_headers()},
        "trust": trust,
    }


def manual_snippet(api_base: str = DEFAULT_API_BASE, *, trust: Trust = "full") -> str:
    """The same entry as YAML, for the README and for operators placing it by
    hand (a person writing it has decided: ``full`` by default)."""
    return (
        "mcp_servers:\n"
        f"  {MCP_SERVER_NAME}:\n"
        f'    url: "{hosted_mcp_url(api_base)}"\n'
        "    headers:\n"
        f'      Authorization: "{AUTH_HEADER_TEMPLATE}"\n'
        + client_header_lines("      ")
        + f"    trust: {trust}  # untrusted: each AgentChat write tool asks first\n"
    )


def client_header_lines(indent: str = "") -> str:
    """The client header as a YAML line, for a hand-written entry."""
    return "".join(f'{indent}{name}: "{value}"\n' for name, value in hermes_mcp_headers().items())


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
                "headers": {"Authorization": AUTH_HEADER_TEMPLATE, **hermes_mcp_headers()},
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
    return "current" if client.strip().lower() == CLIENT_NAME else "other"


def _set_client_header(entry: MutableMapping[str, Any]) -> None:
    """Set ``X-AgentChat-Client: hermes`` on our entry, replacing any casing
    of it; every other header and key stays as it is."""
    headers = dict(entry.get("headers") or {})
    name = CLIENT_HEADER.lower()
    kept = {k: v for k, v in headers.items() if not (isinstance(k, str) and k.lower() == name)}
    entry["headers"] = {**kept, **hermes_mcp_headers()}


def entry_trust(entry: Any) -> TrustState:
    """The tier an entry says, read as Hermes reads it: no ``trust`` key (or
    an empty one) is ``unset`` (Hermes: ``full``); ``full`` / ``untrusted`` in
    any case and spacing are themselves; anything else is ``untrusted``
    (Hermes fails closed). The contract suite checks it against Hermes'."""
    raw = entry.get("trust") if isinstance(entry, Mapping) else None
    if raw is None:
        return "unset"
    text = str(raw).strip().lower()
    if text == "full":
        return "full"
    return "untrusted"


def effective_trust(state: TrustState | None) -> Trust | None:
    """The tier Hermes applies for an entry's :func:`entry_trust` (``None``: no entry)."""
    if state is None:
        return None
    return "untrusted" if state == "untrusted" else "full"


def ensure_entry(
    config: MutableMapping[str, Any],
    *,
    api_base: str = DEFAULT_API_BASE,
    decision: Decision | None = None,
) -> EnsureOutcome:
    """Bring the entry to what ``decision`` asks; add the client header to our
    entry when it has none (once per install).

    * no entry: written with the decision's tier (``yes`` and
      ``carried_over``: ``full``; ``no``: ``untrusted``); with no decision
      nothing is added (``missing``);
    * our entry: a ``yes`` turns an ``untrusted`` one to ``full``
      (``allowed``); nothing else changes its ``trust``;
    * a user-managed or disabled entry is never touched, nor an entry that
      already names a client (``hermes`` or another), nor a version header.
    """
    state = describe_entry(config, api_base=api_base)
    if state == "missing":
        if decision is None:
            return "missing"
        servers = config.get("mcp_servers")
        if not isinstance(servers, MutableMapping):
            servers = {}
            config["mcp_servers"] = servers
        servers[MCP_SERVER_NAME] = server_entry(api_base, trust=TRUST_FOR[decision])
        return "added"
    if state == "ours":
        entry = config["mcp_servers"][MCP_SERVER_NAME]
        if not isinstance(entry, MutableMapping):
            return "present"
        outcome: EnsureOutcome = "present"
        if entry_identity(entry) == "missing":
            _set_client_header(entry)
            outcome = "upgraded"
        if decision == "yes" and entry_trust(entry) == "untrusted":
            entry["trust"] = "full"
            outcome = "allowed"
        return outcome
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
    ``${AGENTCHAT_API_KEY}`` from the profile's secrets, which every running
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
            "be registered. Add this to the profile's config.yaml by hand:\n" + manual_snippet(trust="untrusted")
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
            "could not be registered. Add this to the profile's config.yaml by hand:\n" + manual_snippet(trust="untrusted")
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


@dataclass(frozen=True)
class EntryPlacement:
    """What :func:`install_entry` did: the outcome, the entry's ``trust`` after
    it (``None`` when there is no entry of ours), and whether the client header
    was added on the way (an ``allowed`` entry can gain it too)."""

    outcome: EnsureOutcome
    trust: TrustState | None = None
    header_added: bool = False


def _our_entry(config: Mapping[str, Any]) -> Any:
    servers = config.get("mcp_servers")
    return servers.get(MCP_SERVER_NAME) if isinstance(servers, Mapping) else None


def install_entry(*, api_base: str = DEFAULT_API_BASE, decision: Decision | None = None) -> EntryPlacement:
    """Bring the entry in the active profile's ``config.yaml`` to what
    ``decision`` asks (:func:`ensure_entry`), naming this client. One read,
    one write only when something changes, then a read back: raises
    :class:`McpRegistrationError` when the change did not land."""
    read, save_config, is_managed = _hermes_config_io()
    config = read()
    before = _our_entry(config)
    header_missing = describe_entry(config, api_base=api_base) == "ours" and entry_identity(before) == "missing"
    outcome = ensure_entry(config, api_base=api_base, decision=decision)
    state = describe_entry(config, api_base=api_base)
    trust = entry_trust(_our_entry(config)) if state == "ours" else None
    if outcome not in ("added", "allowed", "upgraded"):
        return EntryPlacement(outcome, trust)
    snippet = manual_snippet(api_base, trust=effective_trust(trust) or "untrusted")
    if is_managed():
        raise McpRegistrationError(
            "Hermes' configuration is managed by a package manager, so the plugin cannot write "
            "config.yaml. Declare the hosted tools there:\n" + snippet
        )
    save_config(config)
    after = read()
    landed = _our_entry(after)
    if (
        describe_entry(after, api_base=api_base) != "ours"
        or entry_identity(landed) == "missing"
        or entry_trust(landed) != trust
    ):
        raise McpRegistrationError("Hermes did not save the change to config.yaml. Add this by hand:\n" + snippet)
    if outcome == "added":
        logger.info(
            "mcp_registration: added mcp_servers.%s → %s (trust: %s)",
            MCP_SERVER_NAME,
            hosted_mcp_url(api_base),
            trust,
        )
    elif outcome == "allowed":
        logger.info("mcp_registration: mcp_servers.%s now has trust: full", MCP_SERVER_NAME)
    else:
        logger.info("mcp_registration: added the client header to mcp_servers.%s", MCP_SERVER_NAME)
    return EntryPlacement(outcome, trust, header_added=header_missing)


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


# ─── what Hermes' approval gate does with the reply tool ───────────────────

Via = Literal["trust", "no_trust_key", "approvals_off", "yolo", "always_grant"]

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class Allowance:
    """Whether Hermes lets the reply tool (:data:`REPLY_TOOL`) of the server
    named ``agentchat`` run without asking, read from this profile's
    ``config.yaml`` as written. ``entry``: :func:`describe_entry` (a
    hand-written entry is gated the same way); ``trust``: what the entry says
    (``None``: no entry); ``via``: what lets the writes through (``None``:
    nothing, so each write would ask and an unattended turn is declined)."""

    entry: EntryState
    trust: TrustState | None
    via: Via | None

    @property
    def allowed(self) -> bool:
        return self.entry not in ("missing", "disabled") and self.via is not None

    @property
    def bypass(self) -> bool:
        """Allowed by something Hermes reads at each call (not the tier it
        recorded when it connected the server)."""
        return self.via in ("approvals_off", "yolo", "always_grant")


def _approval_mode(raw: Any) -> str:
    """``approvals.mode`` as Hermes normalises it: YAML reads a bare ``off`` as
    ``False``; unknown values are ``manual`` (``tools/approval_context.py``
    ``_normalize_approval_mode``; the contract suite checks it)."""
    if isinstance(raw, bool):
        return "off" if raw is False else "manual"
    if isinstance(raw, str) and raw.strip().lower() in ("manual", "smart", "off"):
        return raw.strip().lower()
    return "manual"


def grant_key(tool: str = REPLY_TOOL) -> str:
    """The key Hermes keeps an Always answer to its trust prompt under
    (``tools/mcp_tool_handlers.py`` ``_trust_approval_key``)."""
    return f"mcp:{MCP_SERVER_NAME}/{tool}"


def tools_allowance(
    config: Mapping[str, Any],
    *,
    api_base: str = DEFAULT_API_BASE,
    environ: Mapping[str, str] | None = None,
) -> Allowance:
    """Hermes' approval gate for :data:`REPLY_TOOL`, from ``config.yaml``: the
    entry's tier, else ``approvals.mode: off``, the process ``--yolo``
    (``HERMES_YOLO_MODE``), or an Always approval in ``command_allowlist``.
    Read only; the plugin never writes any of those keys."""
    env = os.environ if environ is None else environ
    state = describe_entry(config, api_base=api_base)
    if state == "missing":
        return Allowance("missing", None, None)
    entry = _our_entry(config)
    trust = entry_trust(entry)
    if state == "disabled":
        return Allowance("disabled", trust, None)
    if trust != "untrusted":
        return Allowance(state, trust, "trust" if trust == "full" else "no_trust_key")
    approvals = config.get("approvals")
    if isinstance(approvals, Mapping) and _approval_mode(approvals.get("mode", "manual")) == "off":
        return Allowance(state, trust, "approvals_off")
    if str(env.get("HERMES_YOLO_MODE", "")).strip().lower() in _TRUTHY:
        return Allowance(state, trust, "yolo")
    allowlist = config.get("command_allowlist")
    if isinstance(allowlist, list) and grant_key() in allowlist:
        return Allowance(state, trust, "always_grant")
    return Allowance(state, trust, None)


def inspect_allowance(*, api_base: str = DEFAULT_API_BASE) -> Allowance:
    """:func:`tools_allowance` for the active profile's raw ``config.yaml``."""
    return tools_allowance(_hermes_raw_config(), api_base=api_base)


def config_is_managed() -> bool:
    """Whether Hermes' configuration is managed by a package manager (the
    plugin cannot write it). ``False`` when that cannot be told."""
    try:
        _read, _save, is_managed = _hermes_config_io()
    except McpRegistrationError:
        return False
    return is_managed()


def inspect_entry_identity(*, api_base: str = DEFAULT_API_BASE) -> tuple[EntryState, IdentityState | None]:
    """The entry's state and, when there is one, how it names the client (for ``doctor``)."""
    config = _hermes_raw_config()
    state = describe_entry(config, api_base=api_base)
    if state == "missing":
        return state, None
    servers = config.get("mcp_servers")
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, Mapping) else None
    return state, entry_identity(entry)
