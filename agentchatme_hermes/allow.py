"""The one switch: may this agent use the AgentChat tools on its own?

The tools are the hosted MCP server, ``mcp_servers.agentchat`` in the
profile's ``config.yaml`` (:mod:`.mcp_registration`). Hermes asks the person
before each write tool of a ``trust: untrusted`` server, and declines one in a
turn nobody can answer; ``trust: full`` lets them all run. The person decides
once, at setup, with one question::

    Allow @alice to use AgentChat tools on its own? [Y/n]

Enter or ``y`` is a yes; ``n`` is a no. Ctrl-C and the end of input decide
nothing (nobody answered). It is never asked per tool, per peer or in the
middle of a turn.

When the entry is written
─────────────────────────
Only once a person has decided, and the decision is recorded in the
profile's own data (:func:`record_path`):

* yes → the entry with ``trust: full``;
* no → the entry with ``trust: untrusted`` (the read-only tools work);
* nobody asked yet → no entry at all: an agent's own terminal, no terminal,
  Ctrl-C, a gateway or any other process activating with nobody present;
* the one exception: a profile set up before 0.3 (only the old
  ``AGENTCHATME_API_KEY`` name, no entry, never placed by this release line)
  gets ``trust: full``, once, recorded (:func:`first_placement`). The person
  set AgentChat up when its tools acted without asking.

A yes over an ``untrusted`` entry a running gateway already connected is
applied in two steps, because Hermes keeps a connected server's tier until
the server is connected again and its config watcher (about once a minute)
only adds and removes servers by name: the entry is removed (the watcher
drops the server), then written with ``trust: full`` once Hermes runs no
``agentchat`` server for this profile any more (:func:`server_runtime`: not
connected, connecting, parked or between sessions of a transport reconnect)
and at least one watcher tick plus margin (:data:`TICK_WAIT_S`) has passed
since the removal; the watcher then connects it fresh. The gateway does the
second step (:func:`start_completion`); the decision is recorded first, so a
crash between the steps completes at the next start. Two to three minutes.
When the gateway keeps the earlier connection past :data:`DROP_TIMEOUT_S`
nothing is written (the old tier would stay in use): the decision stays
pending, the record says so (``stalled``), ``doctor``, ``status`` and the
health record say "restart the gateway to apply", and this process stops
trying after :data:`MAX_APPLY_ATTEMPTS`. A pending ``trust: full`` that cannot
be written (a read-only or package-managed ``config.yaml``) is tried again,
but counted once per start of a Hermes process, and given up at the
:data:`MAX_WRITE_FAILURES`-th start that fails: a yes over an ``untrusted``
entry gets that entry back (the read-only tools keep working), and the switch
says why and names the operator's route — never "being applied" for ever.

Entries an earlier release wrote are left as they are (no ``trust`` key reads
as ``full``), apart from the client header :mod:`.mcp_registration` adds once.

Who answers
───────────
A person: at their own terminal (the CLI and wizard question), or from a chat
with the agent (``/agentchat allow``). The plugin never offers or answers the
question for the agent. The terminal question is not asked in a command an
agent's terminal ran (Hermes stamps ``HERMES_SESSION_ID`` on every command
its terminal tool runs, and a ``hermes agentchat …`` process starts no
session of its own). ``/agentchat allow`` asks through Hermes' own prompt
(``ctx.ask_choice``) on the surface the command came from — a messaging
chat's buttons, a local session's viewers, the classic CLI's picker — so it
is answered wherever that surface is; an agent that drives a Hermes CLI in
a terminal of its own could answer it there. This is best effort, not a
security boundary: an agent with a shell can edit ``config.yaml``. There is
no ``--yes`` flag.

While the switch is off
───────────────────────
:class:`.agent_invoker.AgentInvoker` does not start AgentChat work: no gate
call, no turn, nothing recorded; the socket still acknowledges delivery and
the cards stay on the tray. When the switch comes on, the gateway's minute
check wakes the receive loop once Hermes has the tools registered.
:func:`read_switch` is the one reading of the switch, for the pause,
``doctor``, ``status``, the wizard and the slash command.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Union

from ._version import __version__
from .mcp_registration import (
    Allowance,
    Decision,
    EntryState,
    TrustState,
    config_is_managed,
    inspect_allowance,
    manual_snippet,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .profile_scope import ProfileScope

logger = logging.getLogger(__name__)

COMMAND = "agentchat"
QUESTION = "Allow {who} to use AgentChat tools on its own?"
# Hermes' config watcher drops a removed server and connects an added one on
# its housekeeping tick (60 s, contract-tested): the second step of a yes over
# a connected untrusted server writes at least TICK_WAIT_S (90 s) after the
# removal, and the next tick connects it — 90 to 150 s.
APPLY_WORDS = "it takes effect within about three minutes"

# ─── the words ─────────────────────────────────────────────────────────────

ALLOWED_LINE = "AgentChat tools allowed"
ALREADY_ALLOWED_LINE = "AgentChat tools: allowed"
NOT_ALLOWED_LINE = (
    "AgentChat tools not allowed. {who} can read AgentChat but does not answer on its own. "
    "To allow later: send /agentchat allow in a chat with it."
)
NOT_ANSWERED_LINE = (
    "AgentChat tools: not decided (the question was not answered). To allow: send /agentchat allow "
    "in a chat with {who}, or run {cmd} allow in a terminal."
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
APPLYING_LINE = "AgentChat tools allowed; " + APPLY_WORDS
RESTART_TO_APPLY = "restart the gateway to apply: hermes gateway restart"
STALLED_LINE = (
    "AgentChat tools allowed, not applied yet: the running gateway kept the earlier AgentChat connection — "
    + RESTART_TO_APPLY
)
# trust: full written in place while a gateway the plugin does not run in holds
# the untrusted connection (plugins.isolation: host): only its restart applies it.
AWAITS_RESTART_WORDS = (
    "trust: full is written, but the running gateway keeps its earlier AgentChat connection, and each write tool "
    "still asks, until it restarts — " + RESTART_TO_APPLY
)


def operator_route(api_base: str | None = None) -> str:
    """For a deployment whose ``config.yaml`` is built, not kept between starts
    (a container image): the operator declares the entry there, which is how
    Hermes documents a person widening a server's trust."""
    from .config import DEFAULT_API_BASE

    return (
        "An operator who builds config.yaml (a container image, a config not kept between starts) "
        "declares the tools there instead:\n" + manual_snippet(api_base or DEFAULT_API_BASE, trust="full")
    )


State = Literal["allowed", "applying", "not_allowed_yet", "not_allowed", "disabled", "unknown"]
# The states in which no AgentChat work starts.
PAUSED_STATES: frozenset[str] = frozenset({"applying", "not_allowed_yet", "not_allowed", "disabled"})

_REASONS: dict[str, str] = {
    "not_allowed_yet": "AgentChat tools are not allowed yet (the person allows them with /agentchat allow)",
    "not_allowed": "AgentChat tools are not allowed (the person allows them with /agentchat allow)",
    "applying": f"AgentChat tools are allowed and being applied ({APPLY_WORDS})",
    "disabled": "mcp_servers.agentchat is disabled in config.yaml",
}
_STALLED_REASON = (
    "AgentChat tools are allowed but not applied (the running gateway kept the earlier connection) — "
    + RESTART_TO_APPLY
)


def who(handle: str | None) -> str:
    """``@alice``, or ``this agent`` when the handle is not known."""
    name = (handle or "").strip().lstrip("@")
    return f"@{name}" if name else "this agent"


def reason(state: State, *, stalled: bool = False) -> str:
    """The short reason a cycle is paused (the receive loop's ``failure``);
    ``stalled``: a yes being applied that the running gateway did not take up
    (:attr:`Switch.stalled`)."""
    if state == "applying" and stalled:
        return _STALLED_REASON
    return _REASONS.get(state, state)


# ─── the one reading of the switch ─────────────────────────────────────────


@dataclass(frozen=True)
class Switch:
    """The switch for one profile as ``config.yaml`` and the plugin's record say.

    ``allowance``: :func:`.mcp_registration.tools_allowance` (``None`` when the
    config could not be read); ``record``: :func:`read_record`."""

    state: State
    allowance: Allowance | None = None
    record: dict[str, Any] | None = None
    # The pid of the running gateway that still holds the connection made
    # before a yes was written in place (the record's ``restart_gateway``, not
    # shown to be gone: :func:`gateway_verdict`), else ``None``: the yes is in
    # config.yaml, not in effect until that gateway restarts.
    awaits_restart: int | None = None
    # ``awaits_restart`` because whether that gateway still runs cannot be
    # told from here (Hermes' liveness not readable, a probe error), not
    # because it was seen running.
    restart_unverified: bool = False

    @property
    def in_effect_state(self) -> State:
        """The state the receive loop acts on: a yes written but not in effect
        (:attr:`awaits_restart`) pauses like a yes being applied."""
        return "applying" if self.awaits_restart is not None else self.state

    @property
    def paused(self) -> bool:
        return self.state in PAUSED_STATES

    @property
    def by_tier(self) -> bool:
        """The entry's own tier allows the tools: the person's switch is on."""
        return self.allowance is not None and self.allowance.by_tier

    @property
    def carried(self) -> bool:
        return self.record is not None and self.record.get("how") == "carried_over"

    @property
    def hand_edited(self) -> bool:
        """The entry's trust is not what the plugin last wrote (changed by hand):
        a running gateway uses it once it connects the tools again."""
        if self.record is None or self.allowance is None or self.allowance.trust is None:
            return False
        written = self.record.get("trust")
        if written not in ("full", "untrusted"):
            return False
        now = "untrusted" if self.allowance.trust == "untrusted" else "full"
        return bool(written != now)

    @property
    def stalled(self) -> bool:
        """A yes being applied that the running gateway did not take up: it kept
        the earlier connection past :data:`DROP_TIMEOUT_S`, so ``trust: full``
        is not written yet; a gateway restart applies it."""
        return self.state == "applying" and self.record is not None and self.record.get("stalled") is True

    @property
    def gave_up(self) -> str | None:
        """Why a decided ``trust: full`` was given up (it could not be written
        to ``config.yaml``, :data:`MAX_WRITE_FAILURES` times), or ``None``."""
        if self.record is None or self.record.get("pending") is True:
            return None
        why = self.record.get("gave_up")
        return why if isinstance(why, str) and why else None


def state_of(allowance: Allowance | None, record: dict[str, Any] | None = None) -> State:
    """Which case the switch is in."""
    if allowance is None:
        return "unknown"
    if allowance.entry == "missing":
        return "applying" if record is not None and record.get("pending") is True else "not_allowed_yet"
    if allowance.entry == "disabled":
        return "disabled"
    return "allowed" if allowance.allowed else "not_allowed"


def read_switch(home: Path, *, api_base: str) -> Switch:
    """The switch for ``home`` (run inside the profile's scope): one config
    read and the record. Never raises (``unknown`` when the config cannot be
    read)."""
    try:
        allowance: Allowance | None = inspect_allowance(api_base=api_base)
    except Exception:
        logger.debug("allow: config.yaml not read", exc_info=True)
        return Switch("unknown")
    record = read_record(home)
    state = state_of(allowance, record)
    held = _restart_gateway(record)
    if held is None or state != "allowed":
        return Switch(state, allowance, record)
    verdict = gateway_verdict(held, gateway_seen(home))
    if verdict == "gone":
        # Positive evidence only (Hermes says no gateway runs, or another pid,
        # or another start time, both readable): the yes is in effect, and the
        # record forgets that gateway, once.
        assert record is not None
        _rewrite(home, record, restart_gateway=None)
        return Switch(state, allowance, read_record(home))
    return Switch(state, allowance, record, held[0], restart_unverified=verdict == "unknown")


GatewayId = tuple[int, Optional[int]]
"""A gateway process: its pid and its start-time fingerprint (Hermes'
``gateway.status.get_process_start_time``; ``None`` where it cannot be read)."""


def _restart_gateway(record: dict[str, Any] | None) -> GatewayId | None:
    """The gateway the record says still held the connection when a yes was
    written in place, or ``None``."""
    held = record.get("restart_gateway") if record is not None else None
    if not isinstance(held, dict):
        return None
    pid, start = held.get("pid"), held.get("start_time")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    return pid, start if isinstance(start, int) and not isinstance(start, bool) else None


Seen = Union[GatewayId, Literal["stopped", "unknown"]]
"""What Hermes says of the gateway serving a profile (:func:`gateway_seen`):
the gateway running, ``stopped`` (no gateway, and Hermes' probe reported no
error), or ``unknown`` (not readable, a probe error, running with no pid)."""


def gateway_verdict(recorded: GatewayId, seen: Seen) -> Literal["same", "gone", "unknown"]:
    """Whether the gateway ``recorded`` is the one Hermes sees now. ``gone``
    only on positive evidence: Hermes says none runs (no probe error), or the
    one running has another pid, or another start time where both are
    readable. Where only one side's start time is readable (a pid in another
    namespace, a process this one may not inspect) the pid alone decides; an
    unreadable liveness is ``unknown``, never ``gone``."""
    if seen == "unknown":
        return "unknown"
    if seen == "stopped":
        return "gone"
    return "same" if same_gateway(recorded, seen) else "gone"


def same_gateway(recorded: GatewayId, current: GatewayId) -> bool:
    """One process, not a pid reused: the same pid, and start-time
    fingerprints that agree where both are readable (Hermes'
    ``start_time_fingerprints_match``, its 2 s drift tolerance); where only one
    side's, or neither, is readable, the pid alone."""
    if recorded[0] != current[0]:
        return False
    before, now = recorded[1], current[1]
    if before is None or now is None:
        return True
    try:
        from gateway.status import start_time_fingerprints_match  # type: ignore[import-not-found]

        return bool(start_time_fingerprints_match(before, now))
    except Exception:
        return abs(before - now) <= 200


def describe(switch: Switch, handle: str | None, *, cmd: str = "hermes agentchat", cli: bool = True) -> str:
    """``doctor`` / ``status`` wording for the switch (no level prefix);
    ``cmd``: ``hermes agentchat`` spelled for the profile; ``cli`` False where
    the ``hermes agentchat`` commands do not exist (``plugins.isolation:
    host``), so none is named."""
    agent = who(handle)
    allowance = switch.allowance
    run_allow = f", or run {cmd} allow" if cli else ""
    if switch.awaits_restart is not None and switch.restart_unverified:
        return (
            f"allowed in config.yaml, not in effect yet as far as can be told — the gateway that kept the earlier "
            f"AgentChat connection (pid {switch.awaits_restart}) cannot be checked from here, so whether it "
            f"restarted cannot be told; {RESTART_TO_APPLY}"
        )
    if switch.awaits_restart is not None:
        return f"allowed in config.yaml, not in effect yet — {AWAITS_RESTART_WORDS} (gateway pid {switch.awaits_restart})"
    if switch.state == "allowed" and allowance is not None:
        if switch.hand_edited:
            return (
                "allowed in config.yaml (trust changed by hand there): a running gateway uses it once it "
                "connects the tools again — hermes gateway restart, or /reload-mcp in a messaging chat it serves "
                "(Telegram, Discord, …; not Desktop or a terminal chat, whose /reload-mcp keeps a connected server "
                "as it is)"
            )
        if allowance.via == "trust":
            if switch.carried:
                return "allowed (carried over from your setup under an earlier release)"
            return "allowed (trust: full)"
        if allowance.via == "no_trust_key":
            return "allowed (the entry has no trust key, which Hermes reads as full)"
        if allowance.via == "approvals_off":
            return (
                "allowed by approvals.mode: off (mcp_servers.agentchat says trust: untrusted; /agentchat allow "
                "turns the tools themselves on)"
            )
        if allowance.via == "yolo":
            return "allowed by --yolo (HERMES_YOLO_MODE; mcp_servers.agentchat says trust: untrusted)"
    if switch.state == "applying":
        if switch.stalled:
            return (
                "allowed, not applied yet — the running gateway kept the earlier AgentChat connection, so "
                f"trust: full is not written; {RESTART_TO_APPLY}"
            )
        return (
            f"allowed, being applied ({APPLY_WORDS}: the running gateway drops the old connection, then "
            "connects the tools again) — if it does not, hermes gateway restart"
        )
    if switch.state == "not_allowed_yet":
        if switch.gave_up:
            return (
                f"not allowed yet — trust: full was decided but could not be written to config.yaml "
                f"({switch.gave_up}); once it is writable send /agentchat allow in a chat with {agent}, or "
                "an operator who builds config.yaml declares mcp_servers.agentchat with trust: full there"
            )
        return (
            f"not allowed yet — {agent} has no AgentChat tools until the person allows them: send "
            f"/agentchat allow in a chat with it{run_allow}"
        )
    if switch.state == "not_allowed" and switch.gave_up:
        return (
            f"not allowed — trust: full was decided but could not be written to config.yaml ({switch.gave_up}), "
            f"so the entry is back with trust: untrusted: {agent} can read AgentChat but does not answer on its "
            f"own; once config.yaml is writable send /agentchat allow in a chat with it, or an operator who builds "
            "config.yaml declares mcp_servers.agentchat with trust: full there"
        )
    if switch.state == "not_allowed":
        hand = " (trust changed by hand in config.yaml: a running gateway uses it once it reconnects)" if switch.hand_edited else ""
        return (
            f"not allowed — {agent} can read AgentChat but does not answer on its own{hand}; send /agentchat "
            f"allow in a chat with it{run_allow}"
        )
    if switch.state == "disabled":
        return "mcp_servers.agentchat is disabled in config.yaml — the agent has no AgentChat tools"
    return "not known (this Hermes' config.yaml could not be read)"


# ─── who is at the terminal (best effort) ──────────────────────────────────


def in_agent_terminal(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process was started inside a Hermes agent's process tree,
    as far as can be told: Hermes puts ``HERMES_SESSION_ID`` in the
    environment of every command its terminal tool runs
    (``tools/environments/local.py`` ``_inject_session_context_env``;
    ``gateway/session_context.py`` ``set_current_session_id``), and a
    ``hermes agentchat …`` process starts no session of its own. A person's
    shell that a Hermes process started (Desktop's terminal pane, a shell
    escape) carries it too: there the person allows in a chat. Best effort:
    an agent can unset the variable."""
    env = os.environ if environ is None else environ
    return bool((env.get("HERMES_SESSION_ID") or "").strip())


def stdin_is_tty() -> bool:
    """Is stdin an interactive terminal? ``False`` for pipes, files, a
    closed or replaced stdin — anywhere a prompt would never be answered."""
    try:
        return sys.stdin is not None and bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


Answer = Literal["yes", "no", "cancelled", "end_of_input"]


def ask_in_terminal(question: str) -> Answer:
    """``question [Y/n]`` on the terminal: Enter, ``y`` or ``yes`` is a yes;
    anything else typed is a no; Ctrl-C (``cancelled``) and the end of input
    decide nothing. Read with Hermes' own line reader
    (``hermes_cli.cli_output.line_input``), which keeps those apart from an
    empty line (its ``prompt`` turns all three into ``""``)."""
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
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return "cancelled"
    except EOFError:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return "end_of_input"
    return "yes" if str(answer).strip().lower() in ("", "y", "yes") else "no"


@dataclass(frozen=True)
class SetupDecision:
    """What the person decided at setup: ``decision`` ``yes`` / ``no``, or
    ``None`` when nobody decided (and nothing changes); ``line`` says which."""

    decision: Decision | None
    line: str


def decide(
    handle: str | None,
    *,
    api_base: str,
    tty: bool,
    home: Path,
    ask: Callable[[str], Answer] | None = None,
    environ: Mapping[str, str] | None = None,
    cmd: str = "hermes agentchat",
) -> SetupDecision:
    """Ask the one question when it is the person's to answer here, before
    the key is saved (the entry is then written once, with the answer).
    Not asked when the entry's tier already allows the tools, when the entry
    is written by hand or disabled, when the config is package-managed, in an
    agent's terminal, or with no terminal."""
    agent = who(handle)
    switch = read_switch(home, api_base=api_base)
    if switch.awaits_restart is not None:
        return SetupDecision(None, f"AgentChat tools: {describe(switch, handle, cmd=cmd)}")
    if switch.by_tier:
        return SetupDecision(None, ALREADY_ALLOWED_LINE)
    if switch.state == "applying":
        return SetupDecision(None, STALLED_LINE if switch.stalled else APPLYING_LINE)
    if switch.allowance is not None and switch.allowance.entry == "user_managed":
        return SetupDecision(None, HAND_MANAGED_LINE)
    if switch.state == "disabled":
        return SetupDecision(None, DISABLED_LINE)
    if config_is_managed():
        return SetupDecision(None, MANAGED_LINE)
    if in_agent_terminal(environ):
        return SetupDecision(None, AGENT_TERMINAL_LINE.format(who=agent))
    if not tty:
        return SetupDecision(None, NO_TERMINAL_LINE.format(who=agent, cmd=cmd) + "\n" + operator_route(api_base))
    answer = (ask or ask_in_terminal)(QUESTION.format(who=agent))
    if answer == "yes":
        return SetupDecision("yes", ALLOWED_LINE)
    if answer == "no":
        return SetupDecision("no", NOT_ALLOWED_LINE.format(who=agent))
    if answer == "end_of_input":
        return SetupDecision(None, NO_TERMINAL_LINE.format(who=agent, cmd=cmd))
    return SetupDecision(None, NOT_ANSWERED_LINE.format(who=agent, cmd=cmd))


def line_after(tools: SetupDecision, mcp_outcome: str | None) -> str | None:
    """The answer's line once the entry is written (``mcp_outcome``: what
    :func:`.placement.place_mcp` did), or ``None`` when the placement's own
    warning says it all: a yes written in place that the running gateway takes
    up only when it restarts (``restart``) is never said to be "allowed"."""
    return None if tools.decision == "yes" and mcp_outcome == "restart" else tools.line


def decide_for(handle: str | None) -> SetupDecision:
    """:func:`decide` for this process: the profile, its API base, the
    profile's command and whether stdin is a terminal. The CLI, the wizard and
    ``doctor --fix`` ask through this."""
    from .config import cli_prefix
    from .profile_scope import resolve_hermes_home

    home = resolve_hermes_home()
    return decide(handle, api_base=_api_base(home), tty=stdin_is_tty(), home=home, cmd=cli_prefix(home))


def _api_base(home: Path) -> str:
    """This profile's API base (:func:`.config.resolve_api_base`, the one resolver)."""
    from .config import resolve_api_base, stored_api_base

    return resolve_api_base(stored_api_base(home), strict=False)


# ─── the record, the one-time carry-over, a pending yes ────────────────────

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


def write_record(
    home: Path,
    *,
    how: How,
    trust: TrustState | None,
    decision: Decision | None = None,
    pending: bool = False,
    stalled: bool = False,
    write_failures: int = 0,
    gave_up: str | None = None,
    decided_at: float | None = None,
    restart_gateway: GatewayId | None = None,
) -> bool:
    """Record how this profile's entry was decided: ``asked`` (a person:
    ``decision`` yes / no), ``carried_over`` (the one-time carry-over),
    ``existing`` (an entry an earlier release placed was there when this
    release line first looked). ``pending``: ``trust: full`` is decided but
    not written yet (a yes being applied, a carry-over not placed yet);
    ``stalled``: the running gateway kept the earlier connection, so it was not
    written (a restart applies it); ``write_failures``: starts of a Hermes
    process at which it could not be written; ``gave_up``: why it was given up
    after :data:`MAX_WRITE_FAILURES` of them; ``decided_at``: when a pending
    ``trust: full`` was decided (epoch seconds; for a yes over a connected
    entry, when the entry was removed); ``restart_gateway``: the running
    gateway (pid, start-time fingerprint) that kept the earlier connection when
    ``trust: full`` was written in place (it applies at that gateway's restart,
    :attr:`Switch.awaits_restart`).
    ``False`` (logged) when the record could not be written."""
    from .state import write_json_atomic

    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "how": how,
        "decision": decision,
        "trust": trust,
        "pending": pending,
        "release": __version__,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if stalled:
        record["stalled"] = True
    if write_failures:
        record["write_failures"] = write_failures
    if gave_up:
        record["gave_up"] = gave_up
    if decided_at is not None:
        record["decided_at"] = decided_at
    if restart_gateway is not None:
        record["restart_gateway"] = {"pid": restart_gateway[0], "start_time": restart_gateway[1]}
    try:
        write_json_atomic(record_path(home), record)
    except OSError as exc:
        logger.warning("agentchat plugin: could not write %s (%s)", record_path(home), exc)
        return False
    return True


def _pending(record: dict[str, Any] | None) -> bool:
    return record is not None and record.get("pending") is True


def _failures(record: dict[str, Any]) -> int:
    count = record.get("write_failures")
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def _rewrite(home: Path, record: dict[str, Any], **changes: Any) -> bool:
    """``record`` written again with ``changes`` (the rest kept)."""
    fields: dict[str, Any] = {
        "how": record.get("how") or "asked",
        "trust": record.get("trust"),
        "decision": record.get("decision"),
        "pending": _pending(record),
        "stalled": record.get("stalled") is True,
        "write_failures": _failures(record),
        "gave_up": record.get("gave_up") if isinstance(record.get("gave_up"), str) else None,
        "decided_at": _decided_at(record),
        "restart_gateway": _restart_gateway(record),
    }
    fields.update(changes)
    return write_record(home, **fields)


def _decided_at(record: dict[str, Any] | None) -> float | None:
    when = record.get("decided_at") if record is not None else None
    return float(when) if isinstance(when, (int, float)) and not isinstance(when, bool) else None


def clear_pending(home: Path) -> None:
    """Drop a pending ``trust: full`` (logout removes the entry on purpose)."""
    record = read_record(home)
    if _pending(record):
        assert record is not None
        _rewrite(home, record, pending=False, stalled=False, write_failures=0)


# Pending decisions (home, when decided) whose failed write was counted by
# this process already: a failure counts once per start of a Hermes process.
_failures_counted: set[tuple[str, float | None]] = set()


def note_write_failure(home: Path, detail: str, *, api_base: str | None = None) -> bool:
    """A pending ``trust: full`` that could not be written (a read-only or
    package-managed ``config.yaml``). Tried again while this process runs (a
    transient failure clears itself), but counted once per start of a Hermes
    process, so a person's yes is never given up within minutes; at the
    :data:`MAX_WRITE_FAILURES`-th start that fails it is given up: no longer
    pending, the reason kept. A yes over an ``untrusted`` entry (removed in its
    first step) gets that entry back, so the read-only tools keep working; the
    switch says why and names the operator's route. ``True`` when given up
    now."""
    record = read_record(home)
    if not _pending(record):
        return False
    assert record is not None
    counted = (str(home), _decided_at(record))
    if counted in _failures_counted:
        logger.debug("allow: the allowed AgentChat tools could not be written (%s); counted once this start", detail)
        return False
    _failures_counted.add(counted)
    failures = _failures(record) + 1
    if failures < MAX_WRITE_FAILURES:
        _rewrite(home, record, write_failures=failures)
        logger.warning(
            "agentchat plugin: the allowed AgentChat tools could not be written (%s) — tried again while Hermes "
            "runs and at its next start; given up if it still fails at %d starts (this start: %d)",
            detail,
            MAX_WRITE_FAILURES,
            failures,
        )
        return False
    trust: TrustState | None = None
    restored = ""
    if record.get("how") == "asked":
        # The first step removed our untrusted entry: put it back.
        from .mcp_registration import install_entry

        try:
            if install_entry(api_base=api_base or _api_base(home), decision="no").outcome in ("added", "present"):
                trust = "untrusted"
                restored = " mcp_servers.agentchat is back with trust: untrusted (the read-only tools work)."
        except Exception as exc:
            restored = f" The untrusted entry could not be written back either ({exc})."
    _rewrite(home, record, pending=False, stalled=False, trust=trust, write_failures=failures, gave_up=detail)
    logger.warning(
        "agentchat plugin: the allowed AgentChat tools could not be written at %d starts (%s) — given up: the "
        "AgentChat tools are not allowed for this profile.%s %s",
        failures,
        detail,
        restored,
        operator_route(api_base),
    )
    return True


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
    name is set, there is no entry, and this release line never placed one) —
    recorded, as pending, before anything is written, so a placement that
    does not land completes later and the old key line is never the only
    trace of it; else ``None``. An entry already there is recorded as
    ``existing``, so a later removal never brings the carry-over back."""
    if read_record(home) is not None:
        return None
    if entry != "missing":
        write_record(home, how="existing", trust=None)
        return None
    if not legacy_key_set:
        return None
    if not write_record(home, how="carried_over", trust="full", pending=True, decided_at=time.time()):
        return None  # not recorded: not carried over (it would not be once only)
    return "carried_over"


# ─── a pending yes: the second step, in the gateway ────────────────────────

# How long the gateway waits for Hermes to drop the earlier connection; the
# least time between the removal and the write: one config-watcher tick (60 s,
# contract-tested) plus a margin, so the watcher has run whatever the status
# says; how long it then waits for the tools to be registered again.
DROP_TIMEOUT_S = 180.0
TICK_WAIT_S = 90.0
CONNECT_TIMEOUT_S = 150.0
POLL_S = 2.0
# Timed-out waits after which this process stops trying (a restart applies
# it); starts of a Hermes process that could not write the entry before it is
# given up.
MAX_APPLY_ATTEMPTS = 3
MAX_WRITE_FAILURES = 3

_completions: dict[str, threading.Thread] = {}
_completions_lock = threading.Lock()
_timed_out: dict[str, int] = {}
# What to call in this process once a profile's tools are allowed again (the
# runtime's invoker: re-read the switch, wake the receive loop).
_on_applied: dict[str, Callable[[], object]] = {}


def on_applied(home: Path, callback: Callable[[], object] | None) -> None:
    """Have ``callback`` called when a pending yes for ``home`` is written."""
    if callback is None:
        _on_applied.pop(str(home), None)
    else:
        _on_applied[str(home)] = callback


def gateway_lives(home: Path, *, api_base: str) -> bool:
    """Whether a gateway runs for this profile's account (its health record
    fresh, its pid alive, the leader lock held). Never raises."""
    from .config import resolve_key
    from .ws_health import live_health

    try:
        resolution = resolve_key(home)
        stored = resolution.store.key
        api_key = resolution.held_key or (stored.api_key if stored is not None else None)
        if not api_key:
            return False
        return live_health(api_base, api_key, home) is not None
    except Exception:
        logger.debug("allow: gateway liveness not read", exc_info=True)
        return False


def gateway_seen(home: Path) -> Seen:
    """What Hermes says of the gateway serving ``home``'s profile:
    ``gateway.status.resolve_gateway_liveness``, Hermes' one answer to "is the
    gateway up?" (the profile's pid file and lock, its runtime status, a
    multiplexed gateway serving the profile), read-only and uncached, with
    the process start-time fingerprint (``gateway.status
    .get_process_start_time``) of the one running. ``stopped`` only when
    Hermes says none runs and its probe reported no error (``probe_error``);
    ``unknown`` when it cannot be read. Unlike :func:`gateway_lives` it does
    not need the plugin to run in that gateway: under ``plugins.isolation:
    host`` the gateway holds the hosted tools' connection while no AgentChat
    runtime runs in it. Never raises."""
    try:
        from gateway.status import resolve_gateway_liveness

        liveness = resolve_gateway_liveness(profile_dir=home, use_cache=False)
    except Exception:
        logger.debug("allow: Hermes' gateway liveness not read", exc_info=True)
        return "unknown"
    running = getattr(liveness, "running", None)
    pid = getattr(liveness, "pid", None)
    if running is True:
        if not isinstance(pid, int) or isinstance(pid, bool):
            return "unknown"
        return pid, process_start_time(pid)
    if running is False and getattr(liveness, "probe_error", True) is False:
        return "stopped"
    return "unknown"


def hermes_gateway(home: Path) -> GatewayId | None:
    """The gateway Hermes says serves ``home``'s profile (:func:`gateway_seen`),
    or ``None`` when none is seen running (stopped, or not readable)."""
    seen = gateway_seen(home)
    return None if isinstance(seen, str) else seen


def process_start_time(pid: int) -> int | None:
    """Hermes' start-time fingerprint of a process (``gateway.status
    .get_process_start_time``: Linux ``/proc`` ticks, else psutil), the
    pid-reuse guard; ``None`` when it cannot be read. Never raises."""
    try:
        from gateway.status import get_process_start_time

        start = get_process_start_time(pid)
    except Exception:
        return None
    return start if isinstance(start, int) and not isinstance(start, bool) else None


def held_by_this_process(switch: Switch) -> bool:
    """The gateway that kept the earlier connection is this very process (an
    in-process gateway whose AgentChat runtime was down when the yes was
    written in place): it can apply the yes itself, in two steps."""
    held = _restart_gateway(switch.record)
    if switch.awaits_restart is None or held is None:
        return False
    me = os.getpid()
    return same_gateway(held, (me, process_start_time(me)))


def reapply_in_two_steps(scope: ProfileScope, *, api_base: str) -> bool:
    """In the gateway that holds the earlier connection, once its AgentChat
    runtime runs: the yes written in place is applied as any yes over a
    connected entry — recorded as pending, the entry removed (Hermes' watcher
    drops the connection), and written back by :func:`start_completion` once
    the connection is gone. ``True`` when the first step was taken."""
    from .placement import remove_for_two_steps

    before = read_record(scope.home)
    step = remove_for_two_steps(api_base, scope.home)
    if step.failed:
        if before is not None:
            _rewrite(scope.home, before)  # still written in place, still not in effect
        logger.warning("agentchat plugin: the allowed AgentChat tools could not be applied here (%s)", step.detail)
        return False
    logger.info("agentchat plugin: applying the AgentChat tools allowed while this gateway's runtime was down: %s", step.detail)
    start_completion(scope, api_base=api_base)
    return True


def plugin_host_isolation() -> bool:
    """Whether Hermes runs this profile's plugins in its plugin host
    (``plugins.isolation: host`` in the profile's ``config.yaml``, read as
    Hermes reads it: case and ``-`` aside, anything else is in-process). Then
    this plugin's code runs in a process of its own beside the gateway, never
    in it: no AgentChat socket is held anywhere, so the agent is never woken;
    the ``hermes agentchat`` commands do not exist (Hermes skips
    ``register_cli_command`` there); and the gateway's MCP connection can be
    neither read nor dropped from here. Read from the file, because inside the
    host Hermes' own ``isolation_mode()`` answers ``in_process``. ``False``
    when it cannot be read. Never raises."""
    from .mcp_registration import _hermes_raw_config

    try:
        plugins = _hermes_raw_config().get("plugins")
    except Exception:
        return False
    raw = plugins.get("isolation") if isinstance(plugins, dict) else None
    return str(raw or "").strip().lower().replace("-", "_") == "host"


def host_isolation_note() -> str:
    """What ``plugins.isolation: host`` costs, and the trade-off, as a fact:
    the choice stays the person's. It never advises turning the isolation off
    (a guard Hermes offers for pooled deployments) and names no ``hermes
    agentchat`` command (none exists there)."""
    return (
        "AgentChat cannot wake this agent while its plugins run in Hermes' plugin host (plugins.isolation: "
        "host): no AgentChat connection is held for it, and messages to it wait on AgentChat, undelivered. "
        "The hosted AgentChat tools still work in chats. Being woken needs the plugin inside the gateway "
        "(plugins.isolation: in_process); that is the person's choice, and it is not advised for a pooled or "
        "multi-tenant deployment, which is what the plugin host is for"
    )


Runtime = Literal["held", "gone", "unknown"]


def server_runtime(api_base: str) -> Runtime:
    """Whether Hermes still runs an ``agentchat`` MCP server for this profile.

    Hermes' own status (``get_mcp_status``) read for our entry, so it answers
    after the entry left ``config.yaml`` too, and its reconnect state
    (``mcp_server_reconnecting``), both in the current registry scope — under a
    gateway serving several profiles each profile's own (its home's key), so
    one profile never waits on another's server; a single-profile process has
    one scope. ``held``: connected, connecting (a connect in flight uses the
    config it began with), lazily registered, parked (a parked server stays
    registered and probes itself: it would come back with its old tier), or
    between sessions of a transport reconnect (its task retries with the
    config it began with; the status shows it as ``configured``). ``gone``:
    Hermes holds nothing under the name and nothing is reconnecting.
    ``unknown``: not readable, or failed with a connect error — a first
    connect that failed leaves no server, one that parked on it keeps it, and
    the status does not tell them apart. Never raises."""
    from .hosted_tools import hosted_server_reconnecting, hosted_server_state
    from .mcp_registration import server_entry

    state = hosted_server_state(entry=server_entry(api_base))
    if state is None:
        return "unknown"
    if state.status in ("connected", "connecting", "lazy"):
        return "held"
    if state.status == "failed" and state.error is None:
        return "held"
    reconnecting = hosted_server_reconnecting()
    if reconnecting:
        return "held"
    if state.status == "configured" and reconnecting is False:
        return "gone"
    return "unknown"


def _wait_for_drop(home: Path, *, api_base: str, stop: threading.Event) -> bool | None:
    """Until Hermes runs no ``agentchat`` server for this profile
    (:func:`server_runtime` not ``held``) and at least :data:`TICK_WAIT_S` has
    passed since the entry was removed (the record's ``decided_at``; this wait's
    start when it has none): by then the config watcher has run at least once
    and dropped any server it ran, so neither a status that cannot tell nor a
    moment between a reconnect's attempts is taken for a drop. ``True``:
    dropped; ``False``: still held after :data:`DROP_TIMEOUT_S`; ``None``:
    stopped, or the decision is no longer pending (a logout, a no)."""
    started = time.monotonic()
    decided = _decided_at(read_record(home))
    before = max(0.0, time.time() - decided) if decided is not None else 0.0
    while True:
        runtime = server_runtime(api_base)
        waited = time.monotonic() - started
        if runtime != "held" and before + waited >= TICK_WAIT_S:
            return True
        if waited >= DROP_TIMEOUT_S:
            return False
        if stop.wait(POLL_S) or not _pending(read_record(home)):
            return None


def _note_stalled(home: Path) -> None:
    """The gateway kept the earlier connection past the wait: nothing written
    (it would keep its old tier), the decision stays pending and the record
    says so (``doctor``, ``status`` and the health record read it)."""
    from .profile_scope import profile_label

    key = str(home)
    with _completions_lock:
        attempts = _timed_out.get(key, 0) + 1
        _timed_out[key] = attempts
    record = read_record(home)
    if _pending(record) and record is not None and record.get("stalled") is not True:
        _rewrite(home, record, stalled=True)
    again = (
        "tried again shortly"
        if attempts < MAX_APPLY_ATTEMPTS
        else f"not tried again in this process after {attempts} attempts"
    )
    logger.warning(
        "agentchat plugin: Hermes still runs the earlier AgentChat connection for profile=%s after %.0fs, so "
        "trust: full is not written (that connection would keep its old tier) — %s (%s)",
        profile_label(home),
        DROP_TIMEOUT_S,
        RESTART_TO_APPLY,
        again,
    )


def complete_pending(home: Path, *, api_base: str, wait: bool, stop: threading.Event | None = None) -> bool:
    """Write the pending ``trust: full`` for ``home`` (the current profile
    scope). With ``wait`` (the gateway), first until Hermes runs no
    ``agentchat`` server for this profile any more (:func:`_wait_for_drop`) —
    never blindly: still held after :data:`DROP_TIMEOUT_S`, nothing is written
    and the record says "restart the gateway to apply" — and afterwards until
    the tools are registered again, at most :data:`CONNECT_TIMEOUT_S`. The
    decision and the entry are read again right before the write, so a logout,
    a no or a hand-written entry during the wait is never written over. A
    write that fails is counted (:func:`note_write_failure`). ``True`` when
    the entry was written."""
    from .hosted_tools import hosted_tool_names
    from .mcp_registration import McpRegistrationError, inspect_entry
    from .placement import place_mcp

    if not _pending(read_record(home)):
        return False
    stop = stop or threading.Event()
    if wait:
        dropped = _wait_for_drop(home, api_base=api_base, stop=stop)
        if dropped is None:
            return False
        if not dropped:
            _note_stalled(home)
            return False
    record = read_record(home)
    if not _pending(record):
        return False
    assert record is not None
    try:
        entry = inspect_entry(api_base=api_base)
    except McpRegistrationError:
        entry = "missing"  # not readable: the write below fails and says why
    if entry != "missing":
        logger.info(
            "agentchat plugin: mcp_servers.agentchat is in config.yaml again (%s) — the pending trust: full is "
            "not written over it",
            entry,
        )
        clear_pending(home)
        return False
    decision: Decision = "carried_over" if record.get("how") == "carried_over" else "yes"
    step = place_mcp(api_base, decision=decision)
    if step.failed or step.outcome not in ("added", "present", "allowed"):
        note_write_failure(home, step.detail, api_base=api_base)
        return False
    logger.info("agentchat plugin: AgentChat tools allowed: %s", step.detail)
    if wait:
        # The tools back before the callback, so the wake it gives finds them.
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        names = hosted_tool_names()
        while names == [] and time.monotonic() < deadline and not stop.wait(POLL_S):
            names = hosted_tool_names()
    callback = _on_applied.get(str(home))
    if callback is not None:
        try:
            callback()
        except Exception:
            logger.debug("allow: the applied callback raised", exc_info=True)
    return True


def start_completion(scope: ProfileScope, *, api_base: str) -> threading.Thread | None:
    """In the gateway: finish a pending yes on a daemon thread, once at a
    time (:func:`complete_pending` with ``wait``, in the profile's scope).
    ``None`` when one already runs, or when :data:`MAX_APPLY_ATTEMPTS` waits
    timed out in this process already (a restart applies it)."""
    home = scope.home
    key = str(home)
    with _completions_lock:
        running = _completions.get(key)
        if running is not None and running.is_alive():
            return None
        if _timed_out.get(key, 0) >= MAX_APPLY_ATTEMPTS:
            return None

        def run() -> None:
            try:
                scope.run(complete_pending, home, api_base=api_base, wait=True)
            except Exception:
                logger.exception("agentchat plugin: applying the allowed AgentChat tools raised")

        thread = scope.thread(run, name="agentchat-tools-apply")
        _completions[key] = thread
        thread.start()
        return thread


# ─── /agentchat allow ──────────────────────────────────────────────────────

_CHOICES = [{"value": "allow", "label": "Allow"}, {"value": "no", "label": "Don't allow"}]


def register_allow_command(ctx: Any, scope: ProfileScope) -> None:
    """Register ``/agentchat`` (``/agentchat allow``) for this profile (its
    scope, captured in ``register``). A Hermes without ``ctx.register_command``
    gets none (``hermes agentchat allow`` still works). Never raises."""
    register = getattr(ctx, "register_command", None)
    if not callable(register):
        logger.debug("agentchat plugin: this Hermes has no ctx.register_command")
        return

    def agentchat(raw_args: str = "") -> str:
        # Called in the command's own context, where the surface that
        # dispatched it bound Hermes' asker for ctx.ask_choice.
        return run_command(raw_args, scope=scope, ask_choice=getattr(ctx, "ask_choice", None))

    try:
        register(
            COMMAND,
            agentchat,
            description="AgentChat: allow this agent to use the AgentChat tools on its own",
            args_hint="allow",
        )
    except Exception:
        logger.warning("agentchat plugin: /agentchat could not be registered", exc_info=True)


@dataclass(frozen=True)
class _Question:
    """What :func:`_read_command` leaves to ask: the agent's name and the
    profile's command, for the replies."""

    agent: str
    cmd: str


def run_command(raw_args: str, *, scope: ProfileScope, ask_choice: Any) -> str:
    """``/agentchat [allow]``: its reply. The switch is read, and the answer
    written, in the profile's scope; the question is asked here, in the
    caller's own context. Hermes' ``ask_choice`` asks through the asker the
    dispatching surface bound in that context (``plugin_choices.bind_asker``,
    a context variable), which the profile's scope — a copy of the context
    ``register()`` ran in — does not have. Only ``allow`` changes anything."""
    from .profile_scope import ProfileScopeError

    try:
        read = scope.run(_read_command, raw_args, scope=scope)
        if isinstance(read, str):
            return read
        if not callable(ask_choice):
            return f"This Hermes cannot ask here. Run {read.cmd} allow in a terminal to allow {read.agent}."
        picked = ask_choice(QUESTION.format(who=read.agent), list(_CHOICES))
        if picked not in ("allow", "no"):
            return "Nothing changed: the question was not answered."
        return scope.run(_write_answer, "yes" if picked == "allow" else "no", scope=scope, agent=read.agent)
    except ProfileScopeError as exc:
        return f"AgentChat: this profile's scope could not be entered ({exc}). hermes agentchat doctor says more."
    except Exception as exc:
        logger.warning("agentchat plugin: /agentchat failed", exc_info=True)
        return f"AgentChat: the command failed ({exc}). hermes agentchat doctor says more."


def _read_command(raw_args: str, *, scope: ProfileScope) -> str | _Question:
    """In the profile's scope: the reply when there is nothing to ask, else
    what to ask."""
    from .config import cli_prefix, resolve_key
    from .leader_lock import is_hermes_gateway_process
    from .placement import profile_identity

    home = scope.home
    api_base = _api_base(home)
    cmd = cli_prefix(home)
    host = plugin_host_isolation()
    note = f" {host_isolation_note()}." if host else ""
    resolution = resolve_key(home)
    if resolution.refusal is not None:
        return f"AgentChat: {resolution.refusal}"
    if not (resolution.api_key or resolution.held_key):
        if host:
            return f"AgentChat is not set up in this profile yet.{note}"
        return (
            "AgentChat is not set up in this profile yet: run hermes agentchat in a terminal, or ask "
            "your agent to sign up."
        )
    handle = profile_identity(home, api_base=api_base).handle
    agent = who(handle)
    switch = read_switch(home, api_base=api_base)
    words = raw_args.strip().lower().split()
    status = f"AgentChat tools for {agent}: {describe(switch, handle, cmd=cmd, cli=not host)}.{note}"
    if not words or words[0] != "allow":
        return status
    if switch.awaits_restart is not None:
        return status
    if switch.by_tier:
        return f"AgentChat tools are already allowed for {agent}.{note}"
    if switch.state == "applying":
        if is_hermes_gateway_process():
            start_completion(scope, api_base=api_base)
        return status
    if switch.allowance is not None and switch.allowance.entry == "user_managed":
        return HAND_MANAGED_LINE
    if switch.state == "disabled":
        return DISABLED_LINE
    if config_is_managed():
        return MANAGED_LINE
    return _Question(agent=agent, cmd=cmd)


def _write_answer(decision: Decision, *, scope: ProfileScope, agent: str) -> str:
    """In the profile's scope: the person's answer written. After a yes, a
    gateway that cannot run AgentChat until it restarts (a first install
    into a running gateway, :func:`.placement.gateway_cannot_load`) is
    restarted by Hermes' own graceful restart (:func:`restart_after_yes`),
    and the reply says so instead of how the tools connect."""
    reply = _written_answer(decision, scope=scope, agent=agent)
    if decision != "yes" or not reply.startswith("AgentChat tools allowed for"):
        return reply
    restart = restart_after_yes(scope.home)
    return reply if restart is None else f"AgentChat tools allowed for {agent}. {restart}"


def restart_after_yes(home: Path) -> str | None:
    """After a person's yes: when the gateway cannot load the plugin until it restarts, start
    Hermes' graceful restart (:mod:`.gateway_restart`) and say so; where it cannot be started
    (Windows, no gateway found), say the restart is the person's. ``None`` when no restart is
    needed. The one place a yes leads to a restart: ``/agentchat allow``, ``hermes agentchat
    allow``, the wizard's Allow and setup at a person's own terminal."""
    from . import gateway_restart
    from .credentials import LOAD_RESTART_LINE, LOAD_RESTARTING_LINE
    from .placement import gateway_cannot_load

    if not gateway_cannot_load(home):
        return None
    if gateway_restart.request_load_restart(home):
        return LOAD_RESTARTING_LINE
    return LOAD_RESTART_LINE


def _written_answer(decision: Decision, *, scope: ProfileScope, agent: str) -> str:
    """The answer written, and how it comes into use."""
    from .leader_lock import is_hermes_gateway_process
    from .placement import place_mcp

    api_base = _api_base(scope.home)
    if decision == "yes" and read_switch(scope.home, api_base=api_base).by_tier:
        return f"AgentChat tools are already allowed for {agent}."  # allowed meanwhile
    step = place_mcp(api_base, decision=decision)
    if step.failed:
        return f"AgentChat tools: nothing changed — {step.detail}"
    note = f" {host_isolation_note()}." if plugin_host_isolation() else ""
    if decision == "no":
        return NOT_ALLOWED_LINE.format(who=agent) + note
    if step.outcome == "applying":
        if is_hermes_gateway_process():
            start_completion(scope, api_base=api_base)
        return f"AgentChat tools allowed for {agent}; {APPLY_WORDS}."
    if step.outcome == "restart":
        return f"AgentChat tools for {agent}: not in effect yet — {AWAITS_RESTART_WORDS}.{note}"
    if step.outcome == "allowed":
        return f"AgentChat tools allowed for {agent}. This chat uses them after /reload-mcp.{note}"
    return f"AgentChat tools allowed for {agent}. The gateway connects them within about a minute.{note}"
