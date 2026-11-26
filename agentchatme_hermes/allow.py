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
drops the server), then written with ``trust: full`` once the gateway holds
no AgentChat tools any more (the watcher connects it fresh). The gateway does
the second step (:func:`start_completion`); the decision is recorded first,
so a crash between the steps completes at the next start. About two minutes.

Entries an earlier release wrote are left as they are (no ``trust`` key reads
as ``full``), apart from the client header :mod:`.mcp_registration` adds once.

Who answers
───────────
A person: at their own terminal (the CLI and wizard question), or from a chat
with the agent (``/agentchat allow``, asked through Hermes' own
``ctx.ask_choice``). The plugin never offers or answers the question for the
agent: it does not ask in a command an agent's terminal ran (Hermes stamps
``HERMES_SESSION_ID`` on every command its terminal tool runs, and a
``hermes agentchat …`` process starts no session of its own), and refuses
``/agentchat allow`` in a classic chat process that an agent's terminal
started. This is best effort, not a security boundary: an agent with a shell
can edit ``config.yaml``. There is no ``--yes`` flag.

While the switch is off
───────────────────────
:class:`.agent_invoker.AgentInvoker` does not start AgentChat work: no gate
call, no turn, nothing recorded; the socket still acknowledges delivery and
the cards stay on the tray. :func:`read_switch` is the one reading of the
switch, for the pause, ``doctor``, ``status``, the wizard and the slash
command.
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
# its housekeeping tick (60 s): the two steps of a yes over a connected
# untrusted server take two ticks (contract-tested).
APPLY_WORDS = "it takes effect within about two minutes"

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


def who(handle: str | None) -> str:
    """``@alice``, or ``this agent`` when the handle is not known."""
    name = (handle or "").strip().lstrip("@")
    return f"@{name}" if name else "this agent"


def reason(state: State) -> str:
    """The short reason a cycle is paused (the receive loop's ``failure``)."""
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
    return Switch(state_of(allowance, record), allowance, record)


def describe(switch: Switch, handle: str | None, *, cmd: str = "hermes agentchat") -> str:
    """``doctor`` / ``status`` wording for the switch (no level prefix);
    ``cmd``: ``hermes agentchat`` spelled for the profile."""
    agent = who(handle)
    allowance = switch.allowance
    if switch.state == "allowed" and allowance is not None:
        if switch.hand_edited:
            return (
                "allowed in config.yaml (trust changed by hand there): a running gateway uses it once it "
                "connects the tools again — /reload-mcp in a chat it serves, or hermes gateway restart"
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
        return (
            f"allowed, being applied ({APPLY_WORDS}: the running gateway drops the old connection, then "
            "connects the tools again) — if it does not, hermes gateway restart"
        )
    if switch.state == "not_allowed_yet":
        return (
            f"not allowed yet — {agent} has no AgentChat tools until the person allows them: send "
            f"/agentchat allow in a chat with it, or run {cmd} allow"
        )
    if switch.state == "not_allowed":
        hand = " (trust changed by hand in config.yaml: a running gateway uses it once it reconnects)" if switch.hand_edited else ""
        return (
            f"not allowed — {agent} can read AgentChat but does not answer on its own{hand}; send /agentchat "
            f"allow in a chat with it, or run {cmd} allow"
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


# ``HERMES_SESSION_ID`` as this process found it when the plugin was registered
# (before a chat session of this process sets its own), ``""`` when unset.
_origin_session: str | None = None


def note_process_origin(environ: Mapping[str, str] | None = None) -> None:
    """Take the agent-terminal reading once, at ``register(ctx)``."""
    global _origin_session
    if _origin_session is None:
        env = os.environ if environ is None else environ
        _origin_session = (env.get("HERMES_SESSION_ID") or "").strip()


def started_in_agent_terminal(current_session_id: str = "") -> bool:
    """Whether this (non-gateway) process was started inside an agent's
    terminal: a session id was there before this process's own chat session.
    When the plugin was registered after this process's session began, the
    reading is that session's own id, which says nothing: not an agent's."""
    origin = _origin_session or ""
    return bool(origin) and origin != (current_session_id or "").strip()


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
    if switch.by_tier:
        return SetupDecision(None, ALREADY_ALLOWED_LINE)
    if switch.state == "applying":
        return SetupDecision(None, APPLYING_LINE)
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
) -> bool:
    """Record how this profile's entry was decided: ``asked`` (a person:
    ``decision`` yes / no), ``carried_over`` (the one-time carry-over),
    ``existing`` (an entry an earlier release placed was there when this
    release line first looked). ``pending``: ``trust: full`` is decided but
    not written yet (a yes being applied, a carry-over not placed yet).
    ``False`` (logged) when the record could not be written."""
    from .state import write_json_atomic

    record = {
        "version": RECORD_VERSION,
        "how": how,
        "decision": decision,
        "trust": trust,
        "pending": pending,
        "release": __version__,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        write_json_atomic(record_path(home), record)
    except OSError as exc:
        logger.warning("agentchat plugin: could not write %s (%s)", record_path(home), exc)
        return False
    return True


def clear_pending(home: Path) -> None:
    """Drop a pending ``trust: full`` (logout removes the entry on purpose)."""
    record = read_record(home)
    if record is not None and record.get("pending") is True:
        write_record(home, how=record.get("how") or "asked", trust=record.get("trust"), decision=record.get("decision"))


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
    if not write_record(home, how="carried_over", trust="full", pending=True):
        return None  # not recorded: not carried over (it would not be once only)
    return "carried_over"


# ─── a pending yes: the second step, in the gateway ────────────────────────

DROP_TIMEOUT_S = 180.0
CONNECT_TIMEOUT_S = 150.0
POLL_S = 2.0

_completions: dict[str, threading.Thread] = {}
_completions_lock = threading.Lock()
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


def complete_pending(home: Path, *, api_base: str, wait: bool, stop: threading.Event | None = None) -> bool:
    """Write the pending ``trust: full`` for ``home`` (the current profile
    scope). With ``wait``, first until this process holds no hosted AgentChat
    tools (Hermes' watcher dropped the removed server), at most
    :data:`DROP_TIMEOUT_S`, and afterwards until the tools are registered
    again, at most :data:`CONNECT_TIMEOUT_S`. ``True`` when the entry was
    written."""
    from .hosted_tools import hosted_tool_names
    from .placement import place_mcp

    record = read_record(home)
    if record is None or record.get("pending") is not True:
        return False
    stop = stop or threading.Event()
    if wait:
        deadline = time.monotonic() + DROP_TIMEOUT_S
        held = hosted_tool_names() or []
        while held and time.monotonic() < deadline and not stop.is_set():
            stop.wait(POLL_S)
            held = hosted_tool_names() or []
        if held:
            logger.warning(
                "agentchat plugin: Hermes still holds the earlier AgentChat tools after %.0fs; writing trust: "
                "full anyway — if they keep asking, hermes gateway restart",
                DROP_TIMEOUT_S,
            )
    decision: Decision = "carried_over" if record.get("how") == "carried_over" else "yes"
    step = place_mcp(api_base, decision=decision)
    if step.failed or step.outcome not in ("added", "present", "allowed"):
        logger.warning("agentchat plugin: the allowed AgentChat tools were not written — %s", step.detail)
        return False
    logger.info("agentchat plugin: AgentChat tools allowed: %s", step.detail)
    if wait:
        # Wake the receive loop once the tools are back, so its first cycle finds them.
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
    """In the gateway: finish a pending yes on a daemon thread, once
    (:func:`complete_pending` with ``wait``, in the profile's scope). ``None``
    when one already runs."""
    home = scope.home
    key = str(home)
    with _completions_lock:
        running = _completions.get(key)
        if running is not None and running.is_alive():
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

    def agentchat(raw_args: str = "", *, session_id: str = "") -> str:
        return run_command(
            raw_args, scope=scope, ask_choice=getattr(ctx, "ask_choice", None), session_id=session_id
        )

    try:
        register(
            COMMAND,
            agentchat,
            description="AgentChat: allow this agent to use the AgentChat tools on its own",
            args_hint="allow",
        )
    except Exception:
        logger.warning("agentchat plugin: /agentchat could not be registered", exc_info=True)


def run_command(raw_args: str, *, scope: ProfileScope, ask_choice: Any, session_id: str = "") -> str:
    """``/agentchat [allow]``: its reply, worked out in the profile's scope.
    ``allow`` asks the person who sent it (Hermes' own prompt,
    ``ctx.ask_choice``); nothing else changes anything."""
    from .profile_scope import ProfileScopeError

    try:
        return scope.run(_run_command, raw_args, scope=scope, ask_choice=ask_choice, session_id=session_id)
    except ProfileScopeError as exc:
        return f"AgentChat: this profile's scope could not be entered ({exc}). hermes agentchat doctor says more."
    except Exception as exc:
        logger.warning("agentchat plugin: /agentchat failed", exc_info=True)
        return f"AgentChat: the command failed ({exc}). hermes agentchat doctor says more."


def _run_command(raw_args: str, *, scope: ProfileScope, ask_choice: Any, session_id: str) -> str:
    from .config import cli_prefix, resolve_key
    from .leader_lock import is_hermes_gateway_process
    from .placement import place_mcp, profile_identity

    home = scope.home
    api_base = _api_base(home)
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
    switch = read_switch(home, api_base=api_base)
    words = raw_args.strip().lower().split()
    if not words or words[0] != "allow":
        return f"AgentChat tools for {agent}: {describe(switch, handle, cmd=cmd)}."
    in_gateway = is_hermes_gateway_process()
    if not in_gateway and started_in_agent_terminal(session_id):
        return (
            "This chat runs in a process an agent's terminal started, so the plugin does not ask here. "
            f"Send /agentchat allow in your own chat with {agent} (Telegram, Desktop, or a terminal of your own)."
        )
    if switch.by_tier:
        return f"AgentChat tools are already allowed for {agent}."
    if switch.state == "applying":
        if in_gateway:
            start_completion(scope, api_base=api_base)
        return f"AgentChat tools for {agent}: {describe(switch, handle, cmd=cmd)}."
    if switch.allowance is not None and switch.allowance.entry == "user_managed":
        return HAND_MANAGED_LINE
    if switch.state == "disabled":
        return DISABLED_LINE
    if config_is_managed():
        return MANAGED_LINE
    if not callable(ask_choice):
        return f"This Hermes cannot ask here. Run {cmd} allow in a terminal to allow {agent}."
    picked = ask_choice(QUESTION.format(who=agent), list(_CHOICES))
    if picked not in ("allow", "no"):
        return "Nothing changed: the question was not answered."
    decision: Decision = "yes" if picked == "allow" else "no"
    step = place_mcp(api_base, decision=decision)
    if step.failed:
        return f"AgentChat tools: nothing changed — {step.detail}"
    if decision == "no":
        return NOT_ALLOWED_LINE.format(who=agent)
    if step.outcome == "applying":
        if in_gateway:
            start_completion(scope, api_base=api_base)
        return f"AgentChat tools allowed for {agent}; {APPLY_WORDS}."
    if step.outcome == "allowed":
        return f"AgentChat tools allowed for {agent}. This chat uses them after /reload-mcp."
    return f"AgentChat tools allowed for {agent}. The gateway connects them within about a minute."
