"""The one switch: may this agent use the AgentChat tools on its own?

The tools are the hosted MCP server, ``mcp_servers.agentchat`` in the
profile's ``config.yaml`` (:mod:`.mcp_registration`). Hermes asks the person
before each write tool of a ``trust: untrusted`` server, and declines one in a
turn nobody can answer; ``trust: full`` lets them all run. The person decides
once, at setup, with one question::

    Allow @alice to use AgentChat tools on its own? [Y/n]

Enter or ``y`` is a yes; ``n``, end of input and Ctrl-C are a no. It is never
asked per tool, per peer or in the middle of a turn.

When the entry is written
─────────────────────────
Only once a person has decided:

* yes → the entry with ``trust: full`` (an existing ``untrusted`` entry of
  ours is turned to ``full``: a connected server keeps its tier until
  ``/reload-mcp`` or a restart, so only then does the answer name it);
* no → the entry with ``trust: untrusted`` (the read-only tools work);
* nobody asked yet → no entry at all: an agent's own terminal, no terminal,
  a gateway or any other process activating with nobody present;
* the one exception: a profile set up before 0.3 (only the old
  ``AGENTCHATME_API_KEY`` name, no entry, never placed by this release line)
  gets ``trust: full``, once, recorded (:func:`first_placement`). The person
  set AgentChat up when its tools acted without asking.

Entries an earlier release wrote are left as they are (no ``trust`` key reads
as ``full``), apart from the client header :mod:`.mcp_registration` adds once.

Who can say yes
───────────────
A person: at their own terminal (the CLI and wizard question), or from any
chat with the agent (``/agentchat allow``, asked through Hermes' own
``ctx.ask_choice``: buttons on Telegram, Discord and Slack, the clarify panel
in the CLI and Desktop). The agent cannot type a slash command into its own
chat. A terminal is no proof of a person (Hermes gives agents PTYs), so a CLI
started inside an agent's terminal never asks: Hermes stamps
``HERMES_SESSION_ID`` on every command its terminal tool runs, and no
``hermes agentchat …`` process starts a session of its own. There is no
``--yes`` flag.

While the switch is off
───────────────────────
:class:`.agent_invoker.AgentInvoker` does not start AgentChat work: no gate
call, no turn, nothing recorded; the socket still acknowledges delivery and
the cards stay on the tray (the server reminds later). :func:`state_of` says
which case it is; ``doctor``, ``status``, the health record and one log line
per change say it too.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal

from ._version import __version__
from .mcp_registration import (
    Allowance,
    Decision,
    EntryState,
    McpRegistrationError,
    Trust,
    TrustState,
    config_is_managed,
    effective_trust,
    inspect_allowance,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

COMMAND = "agentchat"
QUESTION = "Allow {who} to use AgentChat tools on its own?"

# ─── the words ─────────────────────────────────────────────────────────────

ALLOWED_LINE = "AgentChat tools allowed"
ALREADY_ALLOWED_LINE = "AgentChat tools: allowed"
NOT_ALLOWED_LINE = (
    "AgentChat tools not allowed. {who} can read AgentChat but does not answer on its own. "
    "To allow later: send /agentchat allow in a chat with it."
)
AGENT_TERMINAL_LINE = (
    "AgentChat tools: not allowed yet. This is an agent's terminal, so the plugin does not ask "
    "here. The person who runs {who} allows them once by sending /agentchat allow in a chat with it."
)
NO_TERMINAL_LINE = (
    "AgentChat tools: not allowed yet (no terminal to ask in). To allow: send /agentchat allow "
    "in a chat with {who}, or run {cmd} allow in a terminal."
)
HAND_MANAGED_LINE = (
    "AgentChat tools: mcp_servers.agentchat is written by hand in config.yaml, so the plugin "
    "leaves it; set trust: full there to allow."
)
DISABLED_LINE = "AgentChat tools: mcp_servers.agentchat is disabled in config.yaml — left alone."
MANAGED_LINE = (
    "AgentChat tools: this Hermes install's configuration is managed by a package manager; "
    "declare mcp_servers.agentchat with trust: full there to allow."
)

State = Literal["allowed", "not_allowed_yet", "not_allowed", "restart_to_apply", "disabled", "unknown"]
# The states in which no AgentChat work starts.
PAUSED_STATES: frozenset[str] = frozenset({"not_allowed_yet", "not_allowed", "restart_to_apply", "disabled"})

_REASONS: dict[str, str] = {
    "not_allowed_yet": "AgentChat tools are not allowed yet (the person allows them with /agentchat allow)",
    "not_allowed": "AgentChat tools are not allowed (the person allows them with /agentchat allow)",
    "restart_to_apply": (
        "AgentChat tools are allowed but not applied yet (/reload-mcp in a chat the gateway serves, "
        "or hermes gateway restart)"
    ),
    "disabled": "mcp_servers.agentchat is disabled in config.yaml",
}


def who(handle: str | None) -> str:
    """``@alice``, or ``this agent`` when the handle is not known."""
    name = (handle or "").strip().lstrip("@")
    return f"@{name}" if name else "this agent"


def reason(state: State) -> str:
    """The short reason a cycle is paused (the receive loop's ``failure``)."""
    return _REASONS.get(state, state)


def state_of(allowance: Allowance | None, live: Trust | None = None) -> State:
    """Which case the switch is in. ``live`` is the tier Hermes holds for the
    connected hosted tools in this process (:class:`LiveTier`), ``None`` when
    not known; it matters only when ``config.yaml`` allows the tools through
    their tier (approvals off, ``--yolo`` and an Always approval are read by
    Hermes at each call)."""
    if allowance is None:
        return "unknown"
    if allowance.entry == "missing":
        return "not_allowed_yet"
    if allowance.entry == "disabled":
        return "disabled"
    if not allowance.allowed:
        return "not_allowed"
    if not allowance.bypass and live == "untrusted":
        return "restart_to_apply"
    return "allowed"


def describe(
    state: State,
    allowance: Allowance | None,
    handle: str | None,
    *,
    carried: bool = False,
    cmd: str = "hermes agentchat",
) -> str:
    """``doctor`` / ``status`` wording for the switch (no level prefix);
    ``cmd``: ``hermes agentchat`` spelled for the profile."""
    agent = who(handle)
    if state == "allowed" and allowance is not None:
        if allowance.via == "trust":
            if carried:
                return "allowed (carried over from your setup under an earlier release)"
            return "allowed (trust: full)"
        if allowance.via == "no_trust_key":
            return "allowed (the entry has no trust key, which Hermes reads as full)"
        if allowance.via == "approvals_off":
            return "allowed by approvals.mode: off (mcp_servers.agentchat says trust: untrusted)"
        if allowance.via == "yolo":
            return "allowed by --yolo (HERMES_YOLO_MODE; mcp_servers.agentchat says trust: untrusted)"
        if allowance.via == "always_grant":
            return (
                "agentchat_send_message is allowed by an Always approval; the other AgentChat write tools "
                "still ask — /agentchat allow covers them all"
            )
    if state == "not_allowed_yet":
        return (
            f"not allowed yet — {agent} has no AgentChat tools until the person allows them: send "
            f"/agentchat allow in a chat with it, or run {cmd} allow"
        )
    if state == "not_allowed":
        return (
            f"not allowed — {agent} can read AgentChat but does not answer on its own; send /agentchat "
            f"allow in a chat with it, or run {cmd} allow"
        )
    if state == "restart_to_apply":
        return (
            "allowed in config.yaml, but the running gateway connected them before that — send "
            "/reload-mcp in a chat it serves, or run hermes gateway restart"
        )
    if state == "disabled":
        return "mcp_servers.agentchat is disabled in config.yaml — the agent has no AgentChat tools"
    return "not known (this Hermes' config.yaml could not be read)"


# ─── who is at the terminal ────────────────────────────────────────────────


def in_agent_terminal(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process was started inside a Hermes agent's process tree:
    Hermes puts ``HERMES_SESSION_ID`` in the environment of every command its
    terminal tool runs (``tools/environments/local.py``
    ``_inject_session_context_env``; ``gateway/session_context.py``
    ``set_current_session_id``), and a ``hermes agentchat …`` process starts no
    session of its own. The person's own terminal pane in Hermes Desktop is
    marked ``HERMES_DESKTOP_TERMINAL=1`` and is not an agent's."""
    env = os.environ if environ is None else environ
    if env.get("HERMES_DESKTOP_TERMINAL") == "1":
        return False
    return bool((env.get("HERMES_SESSION_ID") or "").strip())


def stdin_is_tty() -> bool:
    """Is stdin an interactive terminal? ``False`` for pipes, files, a
    closed or replaced stdin — anywhere a prompt would never be answered."""
    try:
        return sys.stdin is not None and bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def ask_in_terminal(question: str) -> bool:
    """``question [Y/n]`` on the terminal: Enter, ``y`` or ``yes`` is a yes;
    anything else, end of input and Ctrl-C are a no. Read with Hermes' own
    line reader (``hermes_cli.cli_output.line_input``), which keeps the
    end of input and Ctrl-C apart from an empty line (its ``prompt`` turns
    both into ``""``)."""
    text = f"  {question} [Y/n]: "
    reader: Callable[[str], str] = input
    try:
        from hermes_cli.cli_output import Colors, color, line_input

        text = color(text, Colors.YELLOW)
        reader = line_input
    except Exception:
        logger.debug("allow: Hermes' line reader not importable; plain input()")
    try:
        answer = reader(text)
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    return str(answer).strip().lower() in ("", "y", "yes")


@dataclass(frozen=True)
class SetupDecision:
    """What the person decided at setup: ``decision`` ``yes`` / ``no``, or
    ``None`` when nobody was asked (and nothing changes); ``line`` says which."""

    decision: Decision | None
    line: str


def decide(
    handle: str | None,
    *,
    api_base: str,
    tty: bool,
    ask: Callable[[str], bool] | None = None,
    environ: Mapping[str, str] | None = None,
    cmd: str = "hermes agentchat",
) -> SetupDecision:
    """Ask the one question when it is the person's to answer here, before
    the key is saved (the entry is then written once, with the answer).
    Not asked when the tools are already allowed, when the entry is written by
    hand or disabled, when the config is package-managed, in an agent's
    terminal, or with no terminal."""
    agent = who(handle)
    try:
        allowance: Allowance | None = inspect_allowance(api_base=api_base)
    except McpRegistrationError:
        allowance = None
    if allowance is not None:
        if allowance.allowed:
            return SetupDecision(None, ALREADY_ALLOWED_LINE)
        if allowance.entry == "user_managed":
            return SetupDecision(None, HAND_MANAGED_LINE)
        if allowance.entry == "disabled":
            return SetupDecision(None, DISABLED_LINE)
    if config_is_managed():
        return SetupDecision(None, MANAGED_LINE)
    if in_agent_terminal(environ):
        return SetupDecision(None, AGENT_TERMINAL_LINE.format(who=agent))
    if not tty:
        return SetupDecision(None, NO_TERMINAL_LINE.format(who=agent, cmd=cmd))
    yes = (ask or ask_in_terminal)(QUESTION.format(who=agent))
    if yes:
        return SetupDecision("yes", ALLOWED_LINE)
    return SetupDecision("no", NOT_ALLOWED_LINE.format(who=agent))


# ─── the one-time carry-over and its record ────────────────────────────────

RECORD_FILENAME = "mcp-entry.json"
RECORD_VERSION = 1
How = Literal["asked", "carried_over", "existing"]


def record_path(home: Path) -> Path:
    """``<profile home>/agentchat/mcp-entry.json``: the plugin's own data
    (beside the key file; logout removes only the key file, so this stays)."""
    from .key_store import store_dir

    return store_dir(home) / RECORD_FILENAME


def read_record(home: Path) -> dict[str, Any] | None:
    """The record, or ``None`` when there is none (or it is unreadable)."""
    from .state import read_json

    record = read_json(record_path(home))
    return record if isinstance(record, dict) else None


def write_record(home: Path, *, how: How, trust: TrustState | None) -> None:
    """Record how this profile's entry was decided: ``asked`` (a person),
    ``carried_over`` (the one-time carry-over), ``existing`` (an entry an
    earlier release placed was there when this release line first looked).
    Never raises: a record that cannot be written is logged."""
    from .state import write_json_atomic

    record = {
        "version": RECORD_VERSION,
        "how": how,
        "trust": trust,
        "release": __version__,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        write_json_atomic(record_path(home), record)
    except OSError as exc:
        logger.warning("agentchat plugin: could not write %s (%s)", record_path(home), exc)


def carried_over(home: Path) -> bool:
    """Whether this profile's entry was the carry-over (doctor's wording)."""
    record = read_record(home)
    return record is not None and record.get("how") == "carried_over"


def legacy_key_set() -> bool:
    """Whether the pre-0.3 name ``AGENTCHATME_API_KEY`` is set for this profile
    (read through Hermes' profile-scoped settings, never from ``.env``)."""
    from .config import ENV_API_KEY_LEGACY
    from .profile_scope import resolve_setting

    try:
        return bool((resolve_setting(ENV_API_KEY_LEGACY) or "").strip())
    except Exception:
        logger.debug("allow: the legacy key name could not be read", exc_info=True)
        return False


def first_placement(home: Path, *, entry: EntryState, legacy_key_set: bool) -> Decision | None:
    """The one decision made for a profile with nobody present, at most once:
    ``carried_over`` when the profile was set up before 0.3 (the legacy key
    name is set, there is no entry, and this release line never placed one);
    else ``None``. An entry already there is recorded as ``existing``, so a
    later removal never brings the carry-over back. The carry-over itself is
    recorded when its entry is written (:func:`.placement.place_mcp`)."""
    if read_record(home) is not None:
        return None
    if entry != "missing":
        write_record(home, how="existing", trust=None)
        return None
    return "carried_over" if legacy_key_set else None


# ─── the tier Hermes holds for the connected tools ─────────────────────────


class LiveTier:
    """The tier Hermes recorded when it registered the hosted tools in this
    process. Hermes records it when it connects the server and keeps it until
    the tools are registered again (``/reload-mcp``, a restart); a transport
    reconnect keeps the registered tools and the tier. Registered tools are
    told apart by the registry entry's identity (``tools.registry``:
    ``ToolEntry`` compares by ``is``). A registration seen for the first time
    takes the tier ``config.yaml`` says then (the reconcile and ``/reload-mcp``
    read the file when they connect); in-process writers observe just before
    they change the file (``/agentchat allow``), so a following
    ``/reload-mcp`` is told apart from the change itself."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entry: object | None = None
        self._live: Trust | None = None

    def observe(self, entry: object | None, config_trust: TrustState | None) -> Trust | None:
        with self._lock:
            if entry is None:
                self._entry, self._live = None, None
                return None
            if entry is not self._entry:
                self._entry, self._live = entry, effective_trust(config_trust)
            return self._live


_trackers: dict[str, LiveTier] = {}
_trackers_lock = threading.Lock()


def live_tier(home: Path) -> LiveTier:
    """This process's :class:`LiveTier` for ``home``'s profile."""
    key = str(home)
    with _trackers_lock:
        tracker = _trackers.get(key)
        if tracker is None:
            tracker = _trackers[key] = LiveTier()
        return tracker


def observe_live(home: Path, allowance: Allowance | None) -> Trust | None:
    """Feed :func:`live_tier` with the hosted tools registered now (the
    current profile scope) and what ``config.yaml`` says."""
    from .hosted_tools import hosted_tool_entry

    entry = hosted_tool_entry()
    return live_tier(home).observe(entry, allowance.trust if allowance is not None else None)


def read_switch(home: Path, *, api_base: str) -> tuple[State, Allowance | None]:
    """The switch for ``home`` as this process sees it: ``config.yaml`` plus
    the tier of the tools registered here. Run inside the profile's scope.
    Never raises (``unknown`` when the config cannot be read)."""
    try:
        allowance: Allowance | None = inspect_allowance(api_base=api_base)
    except Exception:
        logger.debug("allow: config.yaml not read", exc_info=True)
        return "unknown", None
    try:
        live = observe_live(home, allowance)
    except Exception:
        logger.debug("allow: the registered tools could not be read", exc_info=True)
        live = None
    return state_of(allowance, live), allowance


# ─── /agentchat allow ──────────────────────────────────────────────────────

_CHOICES = [{"value": "allow", "label": "Allow"}, {"value": "no", "label": "Don't allow"}]


def register_allow_command(ctx: Any, home: Path) -> None:
    """Register ``/agentchat`` (``/agentchat allow``) for this profile. A Hermes
    without ``ctx.register_command`` gets none (``hermes agentchat allow``
    still works). Never raises."""
    register = getattr(ctx, "register_command", None)
    if not callable(register):
        logger.debug("agentchat plugin: this Hermes has no ctx.register_command")
        return

    def agentchat(raw_args: str = "") -> str:
        return run_command(raw_args, home=home, ask_choice=getattr(ctx, "ask_choice", None))

    try:
        register(
            COMMAND,
            agentchat,
            description="AgentChat: allow this agent to use the AgentChat tools on its own",
            args_hint="allow",
        )
    except Exception:
        logger.warning("agentchat plugin: /agentchat could not be registered", exc_info=True)


def run_command(raw_args: str, *, home: Path, ask_choice: Any) -> str:
    """``/agentchat [allow]``: its reply. ``allow`` asks the person who sent it
    (Hermes' own prompt, ``ctx.ask_choice``); nothing else changes anything."""
    from .profile_scope import profile_context

    try:
        with profile_context(home):
            return _run_command(raw_args, home=home, ask_choice=ask_choice)
    except Exception as exc:
        logger.warning("agentchat plugin: /agentchat failed", exc_info=True)
        return f"AgentChat: the command failed ({exc}). hermes agentchat doctor says more."


def _run_command(raw_args: str, *, home: Path, ask_choice: Any) -> str:
    from .cli import _api_base  # the CLI's resolver, until there is one for every caller
    from .config import cli_prefix, resolve_key
    from .placement import place_mcp, profile_identity

    api_base = _api_base()
    cmd = cli_prefix(home)
    resolution = resolve_key(home)
    if resolution.refusal is not None:
        return f"AgentChat: {resolution.refusal}"
    if not (resolution.api_key or resolution.held_key):
        return (
            "AgentChat is not set up in this profile yet: run hermes agentchat in a terminal, or ask "
            "your agent to sign up."
        )
    handle = profile_identity(home, api_base=api_base).handle
    agent = who(handle)
    state, allowance = read_switch(home, api_base=api_base)
    words = raw_args.strip().lower().split()
    if not words or words[0] != "allow":
        described = describe(state, allowance, handle, carried=carried_over(home), cmd=cmd)
        return f"AgentChat tools for {agent}: {described}."
    if state == "allowed":
        return f"AgentChat tools are already allowed for {agent}."
    if state == "restart_to_apply":
        return f"AgentChat tools for {agent}: {describe(state, allowance, handle, cmd=cmd)}."
    if allowance is not None and allowance.entry == "user_managed":
        return HAND_MANAGED_LINE
    if state == "disabled":
        return DISABLED_LINE
    if config_is_managed():
        return MANAGED_LINE
    if not callable(ask_choice):
        return f"This Hermes cannot ask here. Run {cmd} allow in a terminal to allow {agent}."
    picked = ask_choice(QUESTION.format(who=agent), list(_CHOICES))
    if picked not in ("allow", "no"):
        return "Nothing changed: the question was not answered."
    decision: Decision = "yes" if picked == "allow" else "no"
    if decision == "yes":
        # Before the change: the tools registered now keep the tier they have.
        observe_live(home, allowance)
    step = place_mcp(api_base, decision=decision)
    if step.failed:
        return f"AgentChat tools: nothing changed — {step.detail}"
    if decision == "no":
        return NOT_ALLOWED_LINE.format(who=agent)
    if step.outcome == "allowed":
        from .leader_lock import is_hermes_gateway_process

        if is_hermes_gateway_process():
            return f"AgentChat tools allowed for {agent}. Send /reload-mcp to apply it now."
        return (
            f"AgentChat tools allowed for {agent}. To apply: /reload-mcp in a chat the gateway serves, "
            "or hermes gateway restart."
        )
    return f"AgentChat tools allowed for {agent}. The gateway connects them within about a minute."
